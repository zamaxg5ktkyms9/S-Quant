#!/usr/bin/env python3
"""P-1' Step 0: is the curated 209-ticker universe a mechanically-describable
subset of the full point-in-time universe?

Read-only, descriptive analysis (no backtest, no DSR consumption). For each
quarterly PIT snapshot it splits the mechanical universe into curated
(present in data/universe.csv) vs non-curated and compares the four mechanical
axes available in the PIT cache: market cap, PBR, equity ratio, 5d turnover.
It also reports how much of the curated list falls *outside* the mechanical
universe entirely, attributing the exclusion (price band vs liquidity) against
the raw all-market bars.

Result recorded in docs/backtest_report.md §8.22.

Usage:
    .venv/bin/python scripts/analyze_curated_vs_pit.py

Requires the PIT cache (.backtest_cache/pit_data_*.pkl). The raw-bars
attribution additionally uses .backtest_cache/pit_bars_raw.pkl if present.
"""
from __future__ import annotations

import collections
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scipy.stats import ks_2samp, mannwhitneyu
    HAVE_SCIPY = True
except Exception:  # scipy is optional; effect sizes carry the conclusion
    HAVE_SCIPY = False

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".backtest_cache"
FEATS = ["market_cap_jpy", "pbr", "equity_ratio", "avg_5d_trading_value_jpy"]
PRICE_MAX = 3000.0
LIQ_MIN = 1e8


def load_curated() -> set[str]:
    out = set()
    for line in (ROOT / "data/universe.csv").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.endswith(".T"):
            out.add(line)
    return out


def main() -> int:
    curated = load_curated()
    print(f"curated tickers: {len(curated)}")

    pit_path = sorted(CACHE.glob("pit_data_*.pkl"))[0]
    with open(pit_path, "rb") as fh:
        d = pickle.load(fh)
    ubq, fbq = d["universe_by_quarter"], d["fundamentals_by_quarter"]
    qs = sorted(ubq.keys())

    # --- coverage: how much of curated ever appears in a mechanical universe ---
    ever_in: set[str] = set()
    print("\n=== curated membership per quarterly PIT universe ===")
    for q in qs:
        inter = curated & set(ubq[q])
        ever_in |= inter
        print(f"  {q}: PIT univ={len(ubq[q]):5d}  curated in univ={len(inter):3d}")
    never = sorted(curated - ever_in)
    print(f"\ncurated ever in universe: {len(ever_in)}/{len(curated)} | "
          f"never in universe: {len(never)}")

    # --- attribute the exclusion of the 'never' set against raw all-market bars ---
    raw_p = CACHE / "pit_bars_raw.pkl"
    if raw_p.exists() and never:
        print(f"\nloading raw bars for attribution ({raw_p.stat().st_size/1e9:.2f}GB)...",
              flush=True)
        with open(raw_p, "rb") as fh:
            raw = pickle.load(fh)
        max_close: dict[str, float] = collections.defaultdict(float)
        va: dict[str, list[float]] = collections.defaultdict(list)
        seen: set[str] = set()
        for rows in raw.values():
            for b in rows:
                code = b.get("Code", "")
                seen.add(code)
                cl = b.get("AdjC") or b.get("C")
                if cl is not None:
                    max_close[code] = max(max_close[code], float(cl))
                if b.get("Va") is not None:
                    va[code].append(float(b["Va"]))
        # J-Quants bars use 5-char codes: a 4-digit ticker "1301.T" -> "13010".
        def to_code(ticker: str) -> str:
            base = ticker[:-2]
            for cand in (base + "0", base):
                if cand in seen:
                    return cand
            return base + "0"

        traded = over = lowliq = absent = 0
        for t in never:
            code = to_code(t)
            if code not in seen:
                absent += 1
                continue
            traded += 1
            if max_close[code] > PRICE_MAX:
                over += 1
            elif va[code] and np.mean(va[code]) < LIQ_MIN:
                lowliq += 1
        print(f"never-in-universe={len(never)}: traded_in_raw={traded}, absent={absent}")
        print(f"  exclusion cause -> max_close>¥{PRICE_MAX:.0f}: {over} | "
              f"avg Va<¥{LIQ_MIN:.0f}: {lowliq}")

    # --- pooled distribution comparison (curated in universe vs non-curated) ---
    rows = []
    for q in qs:
        f = fbq[q]
        for t in ubq[q]:
            if t in f.index:
                r = f.loc[t]
                rows.append({"curated": t in curated,
                             **{c: float(r[c]) for c in FEATS}})
    df = pd.DataFrame(rows)
    base = df.curated.mean()
    print(f"\npooled ticker-quarter rows: {len(df)} "
          f"(curated={int(df.curated.sum())}, base rate={base:.3%})")
    print("\n=== distribution comparison (medians [IQR]) ===")
    for c in FEATS:
        a = df.loc[df.curated, c].dropna()
        b = df.loc[~df.curated, c].dropna()
        line = (f"  {c:26s} curated {np.median(a):11.4g} "
                f"[{np.percentile(a,25):.3g}..{np.percentile(a,75):.3g}] | "
                f"non {np.median(b):11.4g} "
                f"[{np.percentile(b,25):.3g}..{np.percentile(b,75):.3g}]")
        if HAVE_SCIPY:
            u, pu = mannwhitneyu(a, b, alternative="two-sided")
            ks, pks = ks_2samp(a, b)
            rb = 1 - 2 * u / (len(a) * len(b))
            line += (f"\n{'':28s}MW p={pu:.2e} rank-biserial={rb:+.3f} | "
                     f"KS D={ks:.3f} p={pks:.2e}")
        print(line)

    # --- best single-axis separating rule (purity lift over base rate) ---
    print(f"\n=== simple-rule separability (base rate {base:.2%}) ===")
    for c, direction in [("market_cap_jpy", "ge"), ("equity_ratio", "ge"),
                         ("pbr", "le"), ("avg_5d_trading_value_jpy", "ge")]:
        best = None
        for t in np.percentile(df[c].dropna(), np.arange(10, 95, 5)):
            sel = df[c] >= t if direction == "ge" else df[c] <= t
            if sel.sum() == 0:
                continue
            purity = df.loc[sel, "curated"].mean()
            recall = (sel & df.curated).sum() / df.curated.sum()
            lift = purity / base
            if best is None or lift > best[0]:
                best = (lift, t, purity, recall)
        if best:
            lift, t, purity, recall = best
            op = "≥" if direction == "ge" else "≤"
            print(f"  {c:26s} best {op} {t:12.4g}: purity={purity:.1%} "
                  f"(lift ×{lift:.2f}) recall={recall:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
