#!/usr/bin/env python3
"""P-1' Step 0b: is the curated 209-ticker universe a sector-selected subset of
the mechanical point-in-time universe?

Step 0 (analyze_curated_vs_pit.py, §8.22) showed the four quality axes in the PIT
cache (market cap / PBR / equity ratio / turnover) separate curated from peer only
weakly (best single-axis lift ×1.2). The one remaining mechanical axis not held in
the PIT cache is *sector* (33-industry). This script joins the J-Quants equities
master (S33Nm) onto both the curated list and the mechanical universe and asks
whether a sector filter would concentrate toward the curated names any better.

Read-only, descriptive (no backtest, no DSR consumption) — same discipline as
Step 0. Sector is treated as a per-ticker property (unique-ticker frame), not
pooled ticker-quarters, since a ticker's S33 is essentially static.

Data:
  - data/universe.csv                      curated list (209 real tickers)
  - .backtest_cache/pit_data_*.pkl         mechanical PIT universe (universe_by_quarter)
  - .backtest_cache/equities_master_latest.pkl   J-Quants master (fetched if absent)

Usage:
    .venv/bin/python scripts/analyze_curated_sectors.py

Result recorded in docs/backtest_report.md §8.23.
"""
from __future__ import annotations

import collections
import os
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".backtest_cache"
MASTER_PKL = CACHE / "equities_master_latest.pkl"


def code_to_ticker(code: str) -> str | None:
    """J-Quants 5-char master code -> internal ticker ('13010' -> '1301.T')."""
    if len(code) == 5 and code.endswith("0"):
        return f"{code[:-1]}.T"
    return None


def load_curated() -> set[str]:
    out = set()
    for line in (ROOT / "data/universe.csv").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.endswith(".T"):
            out.add(line)
    return out


def load_master_rows() -> list[dict]:
    if MASTER_PKL.exists():
        with open(MASTER_PKL, "rb") as fh:
            return pickle.load(fh)
    # Fetch + cache once (network path — mirrors build_universe.py).
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(ROOT / ".env"), override=True)
    from squant.infrastructure.jquants_client import JQuantsClient
    client = JQuantsClient(
        api_key=os.environ.get("JQUANTS_API_KEY", ""),
        requests_per_minute=int(os.environ.get("JQUANTS_RPM", "50")),
    )
    rows = client.fetch_equities_master()
    if rows:
        MASTER_PKL.parent.mkdir(parents=True, exist_ok=True)
        with open(MASTER_PKL, "wb") as fh:
            pickle.dump(rows, fh)
    return rows


def _hist(tickers, sector_of) -> collections.Counter:
    return collections.Counter(
        sector_of[t] for t in tickers if t in sector_of
    )


def main() -> int:
    curated = load_curated()
    master = load_master_rows()
    if not master:
        print("ERROR: equities master unavailable (empty).", file=sys.stderr)
        return 1

    sector_of: dict[str, str] = {}
    scale_of: dict[str, str] = {}
    for r in master:
        t = code_to_ticker(r.get("Code", ""))
        if t:
            sector_of[t] = r.get("S33Nm", "(unknown)")
            scale_of[t] = r.get("ScaleCat", "(unknown)") or "(none)"

    print(f"curated tickers: {len(curated)} | master tickers with sector: {len(sector_of)}")
    cur_missing = sorted(curated - set(sector_of))
    print(f"curated with a sector: {len(curated & set(sector_of))}/{len(curated)}"
          + (f" | missing: {cur_missing}" if cur_missing else ""))

    # --- mechanical PIT universe: unique tickers across all quarters ----------
    pit_path = sorted(CACHE.glob("pit_data_*.pkl"))[0]
    with open(pit_path, "rb") as fh:
        d = pickle.load(fh)
    ubq = d["universe_by_quarter"]
    mech_union = set().union(*[set(v) for v in ubq.values()])
    # Restrict to currently-listed (have a sector) — curated 209 are all real/listed.
    mech = {t for t in mech_union if t in sector_of}
    print(f"\nmechanical PIT universe (union of quarters): {len(mech_union)} "
          f"| currently-listed w/ sector: {len(mech)}")

    cur_in_mech = curated & mech
    base = len(cur_in_mech) / len(mech) if mech else 0.0
    print(f"curated inside mechanical universe: {len(cur_in_mech)} "
          f"| base rate (curated / mech) = {base:.2%}")

    # --- curated 209 own sector histogram vs market -----------------------------
    cur_hist = _hist(curated, sector_of)
    mkt_hist = _hist(sector_of.keys(), sector_of)      # whole listed market
    mech_hist = _hist(mech, sector_of)                 # mechanical universe
    n_cur = sum(cur_hist.values())
    n_mkt = sum(mkt_hist.values())
    print(f"\n=== curated 209 sector distribution (all {n_cur}, vs whole market) ===")
    print(f"  {'sector(S33)':22s} {'curated':>14s}  {'market':>12s}   over/under")
    for sec, c in cur_hist.most_common():
        cur_pct = c / n_cur
        mkt_pct = mkt_hist.get(sec, 0) / n_mkt
        ratio = cur_pct / mkt_pct if mkt_pct else float("inf")
        print(f"  {sec:22s} {c:4d} ({cur_pct:5.1%})  {mkt_hist.get(sec,0):4d} "
              f"({mkt_pct:5.1%})   ×{ratio:.2f}")

    # --- separability: per-sector purity/lift within the mechanical universe ----
    print(f"\n=== per-sector separability inside mechanical universe "
          f"(base rate {base:.2%}) ===")
    print(f"  {'sector(S33)':22s} {'n_mech':>7s} {'n_cur':>6s} "
          f"{'purity':>8s} {'lift':>6s} {'recall':>7s}")
    rows_sep = []
    for sec in mech_hist:
        n_m = mech_hist[sec]
        n_c = sum(1 for t in cur_in_mech if sector_of[t] == sec)
        purity = n_c / n_m if n_m else 0.0
        lift = purity / base if base else 0.0
        recall = n_c / len(cur_in_mech) if cur_in_mech else 0.0
        rows_sep.append((sec, n_m, n_c, purity, lift, recall))
    for sec, n_m, n_c, purity, lift, recall in sorted(
        rows_sep, key=lambda x: x[4], reverse=True
    ):
        if n_c == 0:
            continue
        print(f"  {sec:22s} {n_m:7d} {n_c:6d} {purity:7.1%} "
              f"×{lift:4.2f} {recall:6.1%}")

    # --- best sector *subset* rule (greedy by purity), report cumulative --------
    ranked = sorted(
        [r for r in rows_sep if r[2] > 0], key=lambda x: x[3], reverse=True
    )
    print("\n=== greedy sector-subset rule (add sectors by purity desc) ===")
    cum_m = cum_c = 0
    print(f"  {'#sectors':>8s} {'cum_n_mech':>10s} {'cum_cur':>8s} "
          f"{'cum_purity':>10s} {'cum_lift':>8s} {'cum_recall':>10s}")
    for k, (_sec, n_m, n_c, _purity, _lift, _recall) in enumerate(ranked, 1):
        cum_m += n_m
        cum_c += n_c
        cpur = cum_c / cum_m
        clift = cpur / base if base else 0.0
        crec = cum_c / len(cur_in_mech)
        if k in (1, 2, 3, 5, 8, 10, len(ranked)) or crec >= 0.5:
            print(f"  {k:8d} {cum_m:10d} {cum_c:8d} {cpur:9.1%} "
                  f"×{clift:6.2f} {crec:9.1%}")
        if crec >= 0.5:
            break

    # --- bonus: TOPIX scale category (ScaleCat) as a coarse size axis -----------
    print("\n=== bonus: TOPIX scale category separability (ScaleCat) ===")
    scale_mech = collections.Counter(scale_of[t] for t in mech if t in scale_of)
    for sc, n_m in scale_mech.most_common():
        n_c = sum(1 for t in cur_in_mech if scale_of.get(t) == sc)
        purity = n_c / n_m if n_m else 0.0
        lift = purity / base if base else 0.0
        recall = n_c / len(cur_in_mech) if cur_in_mech else 0.0
        print(f"  {sc:18s} n_mech={n_m:5d} n_cur={n_c:4d} "
              f"purity={purity:5.1%} lift=×{lift:.2f} recall={recall:5.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
