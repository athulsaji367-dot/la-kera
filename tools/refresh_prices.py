#!/usr/bin/env python3
"""Daily spice-price refresh for La-kera.

Pulls Kerala mandi modal prices from KisanDeals state pages
(https://www.kisandeals.com/mandiprices/<COMMODITY>/KERALA/ALL), which
republish the official Agmarknet / eNAM APMC data. Why not data.gov.in
directly? api.data.gov.in refuses connections from outside India
(connection refused from GitHub's runners — verified 2026-10-01), so the
official API cannot be used from GitHub Actions. KisanDeals serves the
same underlying mandi data and is reachable worldwide — but its bot
protection 403s bare datacenter clients, so fetching uses a
Chrome-impersonating HTTP client (curl_cffi) with a full-header
`requests` fallback.

For each spice the script parses the market-wise table, takes the rows
from the freshest reported date, and uses the MEDIAN modal price across
Kerala markets (₹/quintal -> ₹/kg). A price is written to Firestore only
when ALL of these hold:
  - the page fetched and parsed successfully,
  - the freshest data is at most 45 days old,
  - the value is inside the spice's sane ₹/kg bounds,
  - it does not jump more than 50% from the stored price (guards
    against grade mismatches and bad/cloned source data).
Otherwise the stored value is kept and the reason is logged (visible in
the GitHub Actions run log).

vanilla has no reliable free daily source (KisanDeals serves a generic
fallback table for it) — it always keeps its last verified value.
cinnamon mandi data is thin and grade-mismatched vs the app's stored
wholesale value, so in practice the jump guard keeps it manual too.

Env:
  FIREBASE_SERVICE_ACCOUNT  full service-account JSON (repo secret)
"""
import os
import re
import sys
import tempfile
from datetime import date, datetime
from html.parser import HTMLParser
from statistics import median

BASE = "https://www.kisandeals.com/mandiprices/{slug}/KERALA/ALL"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/",
}
MAX_AGE_DAYS = 45
MAX_JUMP = 0.50  # reject changes larger than +/-50% vs stored price

# Firestore doc id -> (KisanDeals commodity slug, sane ₹/kg bounds)
TARGETS = {
    "cardamom": ("CARDAMOM", (1000, 8000)),
    "pepper": ("BLACK-PEPPER", (300, 1100)),
    "cloves": ("CLOVES", (400, 2000)),
    "nutmeg": ("NUTMEG", (300, 2000)),
    "cinnamon": ("CINNAMON", (200, 1200)),
    "ginger": ("GINGER-DRY", (80, 500)),
    "turmeric": ("TURMERIC", (60, 400)),
    # "vanilla": no trustworthy daily feed — intentionally absent.
}


def get_html(url):
    """Fetch a page's HTML, impersonating Chrome; fall back to requests."""
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, headers=HEADERS, impersonate="chrome124",
                              timeout=60)
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code} via curl_cffi")
        return r.text
    except Exception as e:
        print(f"    (curl_cffi path failed: {e}; trying requests)")
    import requests
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


class TableParser(HTMLParser):
    """Collect table rows as lists of cell texts."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def fetch_latest_median_per_kg(slug):
    """Return (₹/kg median modal, freshest date, market count) or raise."""
    parser = TableParser()
    parser.feed(get_html(BASE.format(slug=slug)))
    by_date = {}
    for cells in parser.rows:
        # Market table: [desc, variety, market, district, state,
        #                 min, modal, max, change, date, trend]
        if len(cells) < 10:
            continue
        m_date = re.fullmatch(r"(\d{2}/\d{2}/\d{4})", cells[9] or "")
        m_modal = re.search(r"₹\s*([\d,]+)", cells[6] or "")
        if not m_date or not m_modal:
            continue
        d = datetime.strptime(m_date.group(1), "%d/%m/%Y").date()
        modal = int(m_modal.group(1).replace(",", ""))
        if modal > 0:
            by_date.setdefault(d, []).append(modal)
    if not by_date:
        raise ValueError("no market rows parsed")
    freshest = max(by_date)
    vals = by_date[freshest]
    return round(median(vals) / 100), freshest, len(vals)


def main():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not sa_json:
        sys.exit("ERROR: FIREBASE_SERVICE_ACCOUNT secret is not set.")

    print("Fetching Kerala mandi modal prices from KisanDeals...")
    fetched, fetch_errors = {}, {}
    for doc_id, (slug, _bounds) in TARGETS.items():
        try:
            price, freshest, n = fetch_latest_median_per_kg(slug)
            fetched[doc_id] = (price, freshest, n)
            print(f"  {doc_id}: ₹{price}/kg (median of {n} markets, {freshest})")
        except Exception as e:  # one bad page must not kill the others
            fetch_errors[doc_id] = str(e)
            print(f"  {doc_id}: FETCH FAILED ({e})")

    if not fetched:
        sys.exit("ERROR: every KisanDeals fetch failed; prices untouched.")

    import firebase_admin
    from firebase_admin import credentials, firestore

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(sa_json)
        sa_path = f.name
    firebase_admin.initialize_app(credentials.Certificate(sa_path))
    db = firestore.client()

    today = date.today()
    updated, kept = [], []
    for doc_id, (_slug, (lo, hi)) in TARGETS.items():
        ref = db.collection("prices").document(doc_id)
        snap = ref.get()
        current = snap.to_dict().get("pricePerKg") if snap.exists else None
        if doc_id in fetch_errors:
            kept.append(f"{doc_id}: fetch failed (kept {current})")
            continue
        new_price, freshest, _n = fetched[doc_id]
        age = (today - freshest).days
        if age > MAX_AGE_DAYS:
            kept.append(f"{doc_id}: freshest data {freshest} is {age} days old "
                        f"(kept {current})")
            continue
        if not (lo <= new_price <= hi):
            kept.append(f"{doc_id}: ₹{new_price}/kg outside sane bounds "
                        f"{lo}-{hi} (kept {current})")
            continue
        if current is not None and abs(new_price - current) / current > MAX_JUMP:
            kept.append(f"{doc_id}: ₹{new_price}/kg jumps >50% from stored "
                        f"₹{current} — needs a human look (kept {current})")
            continue
        if current is not None and new_price == current:
            kept.append(f"{doc_id}: unchanged at ₹{new_price}/kg")
            continue
        trend = 0
        if current:
            trend = round((new_price - current) / current * 100)
        ref.set({
            "pricePerKg": new_price,
            "trend": trend,
            "up": new_price >= (current or 0),
            "source": "KisanDeals Kerala modal (Agmarknet/eNAM), auto-refresh",
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        updated.append(f"{doc_id}: ₹{current} -> ₹{new_price}/kg (trend {trend:+d}%)")

    kept.append("vanilla: no reliable daily feed (kept last verified value)")

    print("\nUpdated:")
    print("\n".join("  " + u for u in updated) or "  (none)")
    print("Kept as-is:")
    print("\n".join("  " + k for k in kept) or "  (none)")


if __name__ == "__main__":
    main()
