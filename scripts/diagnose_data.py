"""Diagnose J-Quants data quality: verify fins/summary field names and screener output.

Usage:
    python scripts/diagnose_data.py

Requires JQUANTS_API_KEY in environment (or .env file).
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
import pandas as pd

from squant.infrastructure.jquants_client import JQuantsClient
from squant.application.universe_loader import load_universe

BASE_URL = "https://api.jquants.com/v2"
PROBE_TICKERS = ["7203.T", "6758.T", "9984.T", "6098.T", "8306.T"]  # Toyota, Sony, SoftBank, Recruit, MUFG


def main() -> None:
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        print("ERROR: JQUANTS_API_KEY not set")
        sys.exit(1)

    client = JQuantsClient(api_key=api_key, requests_per_minute=30)

    # ── 1. Check fins/summary raw fields for known tickers ─────────────────────
    print("=" * 60)
    print("1. fins/summary raw fields (Toyota 7203.T)")
    print("=" * 60)
    r = httpx.get(
        f"{BASE_URL}/fins/summary",
        params={"code": "72030"},
        headers={"x-api-key": api_key},
        timeout=30,
    )
    print(f"Status: {r.status_code}")
    if r.is_success:
        data = r.json().get("data", [])
        if data:
            latest = max(data, key=lambda x: x.get("DisclosedDate", ""))
            print(f"Latest disclosure: {latest.get('DisclosedDate')}")
            print("All fields returned:")
            for k, v in latest.items():
                print(f"  {k}: {v}")
        else:
            print("  No data returned — may not be supported on this plan")
    else:
        print(f"  Error: {r.text[:300]}")

    # ── 2. Fetch fundamentals via client and show computed values ──────────────
    print()
    print("=" * 60)
    print("2. Computed fundamentals for probe tickers")
    print("=" * 60)

    today = date.today()
    start = today - timedelta(days=30)
    adj_close, volume = client.fetch_ohlcv(PROBE_TICKERS, start, today)
    fundamentals = client.fetch_fundamentals(PROBE_TICKERS)

    if fundamentals.empty:
        print("  WARNING: fundamentals DataFrame is empty — fins/summary fields may be wrong")
    else:
        print(fundamentals.to_string())

    # ── 3. Check OHLCV Va column (trading value for liquidity) ────────────────
    print()
    print("=" * 60)
    print("3. OHLCV Va (trading value) — sample for Toyota")
    print("=" * 60)
    cached = client._ohlcv_cache.get("7203.T")
    if cached is not None:
        print(f"Columns in cache: {list(cached.columns)}")
        if "Va" in cached.columns:
            print(f"Va (last 5 days):\n{cached['Va'].tail(5)}")
            print(f"avg_5d Va: {cached['Va'].tail(5).mean():,.0f} yen")
        else:
            print("  WARNING: 'Va' column not found — liquidity filter will always fail")
    else:
        print("  No cache for Toyota (OHLCV fetch may have failed)")

    # ── 4. Apply screener to universe and show per-filter counts ──────────────
    print()
    print("=" * 60)
    print("4. Screener simulation on full universe")
    print("=" * 60)
    universe = load_universe()
    print(f"Universe size: {len(universe)} tickers")

    adj_close_u, volume_u = client.fetch_ohlcv(universe, today - timedelta(days=170), today)
    print(f"OHLCV fetched: {len(adj_close_u.columns)} tickers")

    fundamentals_u = client.fetch_fundamentals(universe)
    print(f"Fundamentals fetched: {len(fundamentals_u)} tickers")

    from squant.domain import screener
    filtered = screener.apply_fundamental_filters(
        universe, adj_close_u, fundamentals_u, today, set()
    )
    fc = filtered.attrs.get("filter_counts", {})
    print(f"\nFilter counts (dropped):")
    for k, v in fc.items():
        print(f"  {k}: {v}")
    print(f"  PASSED: {len(filtered)}")

    if not filtered.empty:
        print(f"\nSample of tickers that passed fundamental filters:")
        print(filtered[["ticker", "close", "market_cap_jpy", "pbr", "equity_ratio"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
