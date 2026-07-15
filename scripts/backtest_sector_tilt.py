#!/usr/bin/env python3
"""P-1' Step 2: PIT backtest of the pre-registered sector-tilted mechanical rule.

Step 0b (analyze_curated_sectors.py, §8.23) found sector is the strongest axis
separating the curated 209 from the mechanical universe. This script runs the
pre-registered Step 1 rule to check whether restricting the mechanical PIT
universe to the curated-over-weighted sectors reproduces a durable economic edge.

Pre-registered rule (2026-07-15, locked before this run):
  - Sector set S = 33-sectors with curated-share / market-share >= 1.5:
    {サービス業, 食料品, 小売業, 建設業, 水産・農林業}
  - Universe = mechanical PIT universe ∩ S
  - Strategy/params = production C, unchanged (ma_cross, noTP/ATR3.0/TS5,
    ¥600k budget, 2 positions, price_max 3000). No parameter search.
  - Success = 4-year consecutive PF>=1.0 AND 2026H1 non-negative AND positive
    after DSR discount.

Caveat (documented): sectors are classified with the CURRENT master, so tickers
already delisted are dropped from the sector filter — an optimistic survivor
bias. A failing result is therefore robust a fortiori.

Read-only backtest (no production writes). Requires the PIT cache and the master
cache (.backtest_cache/equities_master_latest.pkl — analyze_curated_sectors.py
fetches it if absent). Result recorded in docs/backtest_report.md §8.23.

Usage:
    .venv/bin/python scripts/backtest_sector_tilt.py
"""
from __future__ import annotations

import pickle
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import run_one_backtest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".backtest_cache"
MASTER_PKL = CACHE / "equities_master_latest.pkl"
SECTORS = {"サービス業", "食料品", "小売業", "建設業", "水産・農林業"}

SPANS = [
    ("2022", date(2022, 1, 1), date(2022, 12, 31)),
    ("2023", date(2023, 1, 1), date(2023, 12, 31)),
    ("2024", date(2024, 1, 1), date(2024, 12, 31)),
    ("2025", date(2025, 1, 1), date(2025, 12, 31)),
    ("2026H1", date(2026, 1, 1), date(2026, 7, 10)),
    ("2022-2025_4yr", date(2022, 1, 1), date(2025, 12, 31)),
]


def code_to_ticker(code: str) -> str | None:
    if len(code) == 5 and code.endswith("0"):
        return f"{code[:-1]}.T"
    return None


def main() -> int:
    if not MASTER_PKL.exists():
        print("ERROR: equities master cache がありません。先に "
              "scripts/analyze_curated_sectors.py を実行してください。", file=sys.stderr)
        return 1
    with open(MASTER_PKL, "rb") as fh:
        master = pickle.load(fh)
    keep = {
        t for r in master
        if (t := code_to_ticker(r.get("Code", ""))) and r.get("S33Nm", "") in SECTORS
    }
    print(f"tickers in sector set S: {len(keep)}")

    pit_path = sorted(CACHE.glob("pit_data_*.pkl"))[-1]
    with open(pit_path, "rb") as fh:
        data = pickle.load(fh)
    print(f"PIT cache: {pit_path.name}")

    data_f = dict(data)
    data_f["universe_by_quarter"] = {
        q: [t for t in tickers if t in keep]
        for q, tickers in sorted(data["universe_by_quarter"].items())
    }

    print("\n=== sector-tilted mechanical rule (production C params) ===")
    print(f"  {'span':14s} {'trades':>6s} {'monthly%':>9s} {'PF':>6s} "
          f"{'winrate':>8s} {'maxDD%':>8s} {'total¥':>12s}")
    for name, s, e in SPANS:
        m = run_one_backtest(
            s, e, data_f, budget=600_000, max_positions=2,
            signal_strategy="ma_cross", target_profit=None, atr_mult=3.0,
            time_stop=5, price_max=3000.0, pit=True,
        )
        pf = m.get("profit_factor")
        pf_s = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)
        print(f"  {name:14s} {m.get('trades'):6d} "
              f"{m.get('monthly_pnl_pct'):+8.2f}% {pf_s:>6s} "
              f"{m.get('win_rate'):7.1%} {m.get('max_dd_pct'):+7.1f}% "
              f"¥{m.get('total_pnl'):+,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
