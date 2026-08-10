"""Plan B — PIT 対応 Walk-Forward 検証（探索用シグナル向け）.

walk_forward.py は非PITキャッシュ・curated ユニバース前提で、内部の InProcessGridRunner が
ma_cross/pullback 向けメモ化のため、PIT + 新シグナルには使えない。本スクリプトは PIT キャッシュを
1回だけロードし、run_one_backtest(pit=True) を in-process で回して IS/OOS Robust 判定を出す。

判定ルールは walk_forward.py と厳密に一致させる:
- IS ベスト = trades ≥ MIN_TRADES_FOR_IS_BEST のうち monthly_pnl_pct 最大。
- Robust = OOS PF ≥ 1.0 AND OOS 月次 ≥ +0.1%。2/3 または 3/3 窓で Robust verdict。

グリッドは high52 に効く出口レバーに絞った 12 combos（過剰最適化・多重検定を抑えるため意図的に
小さく保つ）。2026H1 は真正 OOS ホールドアウトとして WF には *使わない*。
"""

import argparse
import itertools
import json
import pickle
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as bt

# walk_forward.py と一致させる判定定数
ROBUST_PF_THRESHOLD = 1.0
ROBUST_MONTHLY_PCT_THRESHOLD = 0.1
MIN_TRADES_FOR_IS_BEST = 5

# 出口パラメータの絞り込みグリッド（12 combos）
GRID = {
    "target_profit": [None, 0.08],
    "atr_mult": [2.5, 3.0],
    "time_stop": [5, 10, 15],
}

# rolling 3窓（IS 1年 → OOS 1年）。2026H1 は真正OOSとして除外。
WINDOWS = [
    ("W1", "2022-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
    ("W2", "2023-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("W3", "2024-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
]


def _d(s: str) -> date:
    y, m, dd = map(int, s.split("-"))
    return date(y, m, dd)


def _run(data, signal, start, end, params, budget, max_positions):
    return bt.run_one_backtest(
        _d(start), _d(end), data,
        signal_strategy=signal, pit=True,
        budget=budget, max_positions=max_positions,
        target_profit=params["target_profit"],
        atr_mult=params["atr_mult"],
        time_stop=params["time_stop"],
    )


def _is_robust(m: dict) -> bool:
    pf = m.get("profit_factor") or 0.0
    monthly = m.get("monthly_pnl_pct", 0.0)
    return pf >= ROBUST_PF_THRESHOLD and monthly >= ROBUST_MONTHLY_PCT_THRESHOLD


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", required=True)
    ap.add_argument("--budget", type=int, default=600000)
    ap.add_argument("--max-positions", type=int, default=2)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--out-label", required=True)
    args = ap.parse_args()

    cache = args.cache or str(
        sorted(Path(".backtest_cache").glob("pit_data_*.pkl"))[-1]
    )
    print(f"PIT cache: {cache}", flush=True)
    with open(cache, "rb") as fh:
        data = pickle.load(fh)

    combos = [dict(zip(GRID.keys(), vs, strict=True))
              for vs in itertools.product(*GRID.values())]
    print(f"Signal: {args.signal}  combos/IS: {len(combos)}  windows: {len(WINDOWS)}")

    window_results = []
    t_start = time.time()
    for label, is_s, is_e, oos_s, oos_e in WINDOWS:
        print(f"\n{'='*70}\n[{label}] IS={is_s}..{is_e}  OOS={oos_s}..{oos_e}\n{'='*70}")
        is_results = []
        for c in combos:
            m = _run(data, args.signal, is_s, is_e, c, args.budget, args.max_positions)
            m["_params"] = c
            is_results.append(m)
            print(f"  IS {c} -> monthly={m['monthly_pnl_pct']:+.2f}% "
                  f"PF={m.get('profit_factor') or 0:.2f} DD={m['max_dd_pct']:+.1f}% "
                  f"trades={m['trades']}", flush=True)
        qualified = [r for r in is_results if r.get("trades", 0) >= MIN_TRADES_FOR_IS_BEST]
        if not qualified:
            print(f"  ❌ {label}: IS で trades≥{MIN_TRADES_FOR_IS_BEST} なし")
            continue
        qualified.sort(key=lambda r: r["monthly_pnl_pct"], reverse=True)
        best = qualified[0]
        bp = best["_params"]
        print(f"  [{label}-IS best] {bp}  monthly={best['monthly_pnl_pct']:+.2f}% "
              f"PF={best.get('profit_factor') or 0:.2f} DD={best['max_dd_pct']:+.1f}%")

        oos = _run(data, args.signal, oos_s, oos_e, bp, args.budget, args.max_positions)
        robust = _is_robust(oos)
        print(f"  [{label}-OOS] monthly={oos['monthly_pnl_pct']:+.2f}% "
              f"PF={oos.get('profit_factor') or 0:.2f} DD={oos['max_dd_pct']:+.1f}% "
              f"trades={oos['trades']}  Robust={'✅ PASS' if robust else '❌ FAIL'}")

        window_results.append({
            "label": label,
            "is_period": f"{is_s}..{is_e}",
            "oos_period": f"{oos_s}..{oos_e}",
            "best_params": bp,
            "is_metrics": {k: best[k] for k in
                           ("monthly_pnl_pct", "profit_factor", "max_dd_pct", "trades", "win_rate")},
            "oos_metrics": {k: oos[k] for k in
                            ("monthly_pnl_pct", "profit_factor", "max_dd_pct", "trades", "win_rate")},
            "robust": robust,
        })

    n_robust = sum(1 for w in window_results if w["robust"])
    n = len(window_results)
    verdict = ("Robust" if n_robust >= 2 else "Marginal" if n_robust == 1 else "Overfitted")
    print(f"\n{'='*70}\nVERDICT: {n_robust}/{n} robust → {verdict}  "
          f"(elapsed {time.time()-t_start:.0f}s)\n{'='*70}")

    out = {
        "signal": args.signal, "budget": args.budget, "max_positions": args.max_positions,
        "grid": {k: list(v) for k, v in GRID.items()},
        "robust_criteria": {"oos_pf_min": ROBUST_PF_THRESHOLD,
                            "oos_monthly_pct_min": ROBUST_MONTHLY_PCT_THRESHOLD},
        "windows": window_results,
        "n_robust": n_robust, "n_windows": n, "verdict": verdict,
    }
    outpath = Path("docs/backtests") / f"walkforward_pit_{args.out_label}.json"
    outpath.write_text(json.dumps(out, indent=2, default=str))
    print(f"saved: {outpath}")


if __name__ == "__main__":
    main()
