#!/usr/bin/env python3
"""Daily spice-price refresh for La-kera.

Pulls Kerala modal prices from the official Agmarknet feed on data.gov.in
(Government of India Open Data), converts ₹/quintal -> ₹/kg, validates each
price against sane bounds, and updates the Firestore `prices` collection.
The app picks the new values up live via its Firestore listener.

Design rules (honesty first):
- A price is only written when it was actually fetched and passed the
  sanity bounds. On any fetch/parse/validation failure the existing
  Firestore value is left untouched and a warning is logged (visible in
  the GitHub Actions run log).
- cinnamon and vanilla have no reliable free structured daily feed; the
  script looks for them in the feed but in practice they keep their last
  verified value until a source reports.

Env:
  DATA_GOV_IN_API_KEY    free key from https://data.gov.in (My Account)
  FIREBASE_SERVICE_ACCOUNT  full service-account JSON (repo secret)
"""
import json
import os
import re
import sys
import tempfile
from statistics import mean

import requests

RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"  # Agmarknet daily mandi prices
API_URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"
UA = {"User-Agent": "la-kera-price-refresh/1.0 (+https://github.com/athulsaji367-dot/la-kera)"}

# Firestore doc id -> (commodity matcher, sane ₹/kg bounds)
# matcher receives the normalized commodity name (lowercase, no spaces/punct)
def is_dry_ginger(n):
    return "ginger" in n and "dry" in n


TARGETS = {
    "cardamom": (lambda n: "cardamom" in n, (1000, 8000)),
    "pepper": (lambda n: "blackpepper" in n or n == "pepper", (300, 1100)),
    "cloves": (lambda n: "cloves" in n, (400, 2000)),
    "nutmeg": (lambda n: "nutmeg" in n, (300, 2000)),
    "cinnamon": (lambda n: "cinnamon" in n, (200, 1200)),
    "ginger": (is_dry_ginger, (80, 500)),
    "turmeric": (lambda n: "turmeric" in n, (60, 400)),
    "vanilla": (lambda n: "vanilla" in n, (8000, 40000)),
}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fetch_kerala_rows(api_key: str):
    """Fetch all of today's Kerala rows from the Agmarknet snapshot."""
    rows, offset, limit = [], 0, 5000
    while True:
        params = {
            "api-key": api_key,
            "format": "json",
            "filters[state]": "Kerala",
            "limit": limit,
            "offset": offset,
        }
        r = requests.get(API_URL, params=params, headers=UA, timeout=60)
        r.raise_for_status()
        data = r.json()
        batch = data.get("records", [])
        rows.extend(batch)
        total = int(data.get("total", len(rows)))
        if len(rows) >= total or not batch:
            return rows
        offset += limit


def modal_prices_per_kg(rows):
    """Group modal prices (₹/quintal) by normalized commodity -> ₹/kg mean."""
    by_commodity = {}
    for row in rows:
        try:
            modal = float(row["modal_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if modal <= 0:
            continue
        by_commodity.setdefault(norm(row.get("commodity", "")), []).append(modal / 100.0)
    return {name: round(mean(vals)) for name, vals in by_commodity.items() if vals}


def main():
    api_key = os.environ.get("DATA_GOV_IN_API_KEY", "").strip()
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not api_key:
        sys.exit("ERROR: DATA_GOV_IN_API_KEY secret is not set. "
                 "Get a free key at https://data.gov.in (My Account -> API key) "
                 "and add it as a repo secret.")
    if not sa_json:
        sys.exit("ERROR: FIREBASE_SERVICE_ACCOUNT secret is not set.")

    print("Fetching today's Kerala mandi prices from Agmarknet (data.gov.in)...")
    rows = fetch_kerala_rows(api_key)
    print(f"  {len(rows)} Kerala records in today's snapshot.")
    prices = modal_prices_per_kg(rows)

    import firebase_admin
    from firebase_admin import credentials, firestore

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(sa_json)
        sa_path = f.name
    firebase_admin.initialize_app(credentials.Certificate(sa_path))
    db = firestore.client()

    updated, kept = [], []
    for doc_id, (matcher, (lo, hi)) in TARGETS.items():
        fresh = [p for name, p in prices.items() if matcher(name)]
        ref = db.collection("prices").document(doc_id)
        snap = ref.get()
        current = snap.to_dict().get("pricePerKg") if snap.exists else None
        if not fresh:
            kept.append(f"{doc_id}: no feed rows today (kept {current})")
            continue
        new_price = round(mean(fresh))
        if not (lo <= new_price <= hi):
            kept.append(f"{doc_id}: feed value ₹{new_price}/kg outside sane bounds "
                        f"{lo}-{hi} (kept {current})")
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
            "source": "Agmarknet Kerala modal via data.gov.in (auto-refresh)",
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        updated.append(f"{doc_id}: ₹{current} -> ₹{new_price}/kg (trend {trend:+d}%)")

    print("\nUpdated:")
    print("\n".join("  " + u for u in updated) or "  (none)")
    print("Kept as-is:")
    print("\n".join("  " + k for k in kept) or "  (none)")
    if not updated and len(kept) == len(TARGETS):
        print("\nWARNING: nothing was updated. Check the feed and this log.")


if __name__ == "__main__":
    main()
