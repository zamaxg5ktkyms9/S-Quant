"""
Walk-Forward Analysis — 過剰最適化（in-sample over-fit）の検証

手法:
1. 全期間を in-sample / out-of-sample に2分割
2. in-sample で grid search → ベストパラメータを抽出
3. out-of-sample で固定パラメータバックテスト
4. in-sample 性能と out-of-sample 性能を比較

過剰最適化が起きていれば、out-of-sample で性能が大幅劣化する。
逆に両者が近ければ、戦略のロバスト性が示唆される。

実行例:
    python scripts/walk_forward.py
    # → IS: 2024-01〜2024-12 で grid search
    #    OOS: 2025-01〜2025-12 で IS ベストパラメータをそのまま使う
"""

import argparse
import itertools
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# grid_search.py から関数を取り込む
sys.path.insert(0, str(Path(__file__).resolve().parent))
from grid_search import GRID, run_one  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent


def _runner(args_tuple):
    params, start, end = args_tuple
    return params, run_one(params, start, end)


def grid_search_period(start: str, end: str, workers: int) -> list[dict]:
    """指定期間で grid search を実行し、結果リストを返す。"""
    keys = list(GRID.keys())
    combos = [dict(zip(keys, vs)) for vs in itertools.product(*GRID.values())]
    total = len(combos)
    print(f"  Grid search: {total} 組合せ ({start} 〜 {end})", flush=True)

    t0 = time.time()
    results: list[dict] = []
    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_runner, (c, start, end)): c for c in combos}
        for fut in as_completed(futures):
            params, metrics = fut.result()
            completed += 1
            if metrics is None:
                continue
            results.append(metrics)
            if completed % 30 == 0 or completed == total:
                elapsed = time.time() - t0
                eta = elapsed / completed * (total - completed)
                print(f"    {completed}/{total}  elapsed {elapsed:.0f}s  ETA {eta:.0f}s", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--is-start",   default="2024-01-04", help="In-sample 開始日")
    parser.add_argument("--is-end",     default="2024-12-30", help="In-sample 終了日")
    parser.add_argument("--oos-start",  default="2025-01-06", help="Out-of-sample 開始日")
    parser.add_argument("--oos-end",    default="2025-12-30", help="Out-of-sample 終了日")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    print(f"=" * 80)
    print(f"Walk-Forward Analysis")
    print(f"=" * 80)
    print(f"In-Sample  : {args.is_start} 〜 {args.is_end}")
    print(f"Out-of-Sample: {args.oos_start} 〜 {args.oos_end}")
    print(f"並列度: {args.workers}")
    print()

    # === IN-SAMPLE: Grid Search ===
    print(f"[1/3] In-Sample Grid Search", flush=True)
    is_results = grid_search_period(args.is_start, args.is_end, args.workers)
    if not is_results:
        print("In-sample で結果が得られませんでした。")
        sys.exit(1)

    is_results.sort(key=lambda r: r["monthly_pnl_pct"], reverse=True)
    is_best = is_results[0]
    best_params = is_best["params"]

    print()
    print(f"  In-Sample ベストパラメータ:")
    for k, v in best_params.items():
        if v is not None:
            print(f"    {k}: {v}")
    print(f"  In-Sample メトリクス:")
    print(f"    trades={is_best['trades']}  win_rate={is_best['win_rate']*100:.1f}%  "
          f"monthly={is_best['monthly_pnl_pct']:+.2f}%  PF={is_best.get('profit_factor', 0):.2f}  "
          f"maxDD={is_best['max_dd_pct']:+.1f}%")
    print()

    # === OUT-OF-SAMPLE: IS ベストパラメータで1回実行 ===
    print(f"[2/3] Out-of-Sample 検証（IS ベストパラメータで実行）", flush=True)
    oos_metrics = run_one(best_params, args.oos_start, args.oos_end)
    if oos_metrics is None:
        print("OOS バックテストに失敗しました。")
        sys.exit(1)

    print()
    print(f"  Out-of-Sample メトリクス:")
    print(f"    trades={oos_metrics['trades']}  win_rate={oos_metrics['win_rate']*100:.1f}%  "
          f"monthly={oos_metrics['monthly_pnl_pct']:+.2f}%  "
          f"PF={oos_metrics.get('profit_factor') or 0:.2f}  "
          f"maxDD={oos_metrics['max_dd_pct']:+.1f}%")
    print()

    # === 比較レポート ===
    print(f"[3/3] IS vs OOS 比較")
    print(f"-" * 80)
    rows = [
        ("Trades",        f"{is_best['trades']}",                       f"{oos_metrics['trades']}"),
        ("Win Rate (%)",  f"{is_best['win_rate']*100:.1f}",             f"{oos_metrics['win_rate']*100:.1f}"),
        ("Monthly (%)",   f"{is_best['monthly_pnl_pct']:+.2f}",          f"{oos_metrics['monthly_pnl_pct']:+.2f}"),
        ("Total P&L",     f"¥{is_best['total_pnl']:+,.0f}",              f"¥{oos_metrics['total_pnl']:+,.0f}"),
        ("CAGR (%)",      f"{is_best.get('cagr_pct', 0):+.2f}",          f"{oos_metrics.get('cagr_pct', 0):+.2f}"),
        ("Sharpe",        f"{is_best.get('sharpe_ratio', 0):.2f}",       f"{oos_metrics.get('sharpe_ratio', 0):.2f}"),
        ("Sortino",       f"{is_best.get('sortino_ratio') or 0:.2f}",    f"{oos_metrics.get('sortino_ratio') or 0:.2f}"),
        ("Calmar",        f"{is_best.get('calmar_ratio', 0):.2f}",       f"{oos_metrics.get('calmar_ratio', 0):.2f}"),
        ("Profit Factor", f"{is_best.get('profit_factor') or 0:.2f}",    f"{oos_metrics.get('profit_factor') or 0:.2f}"),
        ("Max DD (%)",    f"{is_best['max_dd_pct']:+.1f}",               f"{oos_metrics['max_dd_pct']:+.1f}"),
    ]
    print(f"  {'Metric':<18} {'In-Sample':>14}  {'Out-of-Sample':>14}")
    print(f"  {'-'*18} {'-'*14}  {'-'*14}")
    for label, isv, oosv in rows:
        print(f"  {label:<18} {isv:>14}  {oosv:>14}")

    # 性能劣化率
    def _pct_change(is_v: float, oos_v: float) -> str:
        if is_v == 0:
            return "n/a"
        ch = (oos_v - is_v) / abs(is_v) * 100
        sign = "+" if ch >= 0 else ""
        return f"{sign}{ch:.0f}%"

    print()
    print(f"  劣化率 (OOS - IS) / |IS|:")
    print(f"    Monthly: {_pct_change(is_best['monthly_pnl_pct'], oos_metrics['monthly_pnl_pct'])}")
    print(f"    PF     : {_pct_change(is_best.get('profit_factor') or 0, oos_metrics.get('profit_factor') or 0)}")
    print(f"    Sharpe : {_pct_change(is_best.get('sharpe_ratio') or 0, oos_metrics.get('sharpe_ratio') or 0)}")
    print()

    # 過剰最適化の判定
    is_m = is_best["monthly_pnl_pct"]
    oos_m = oos_metrics["monthly_pnl_pct"]
    if is_m > 0 and oos_m > 0 and oos_m >= is_m * 0.5:
        verdict = "✅ Robust: OOS が IS の50%以上を維持。過剰最適化リスクは小さい"
    elif is_m > 0 and oos_m > 0:
        verdict = "⚠ Moderate: OOS が IS の半分以下に低下。やや過剰最適化の兆候"
    elif is_m > 0 and oos_m <= 0:
        verdict = "❌ Overfitted: IS でプラスだが OOS でマイナス。明確な過剰最適化"
    else:
        verdict = "判定不能: IS 自体がマイナス"
    print(f"  Verdict: {verdict}")

    # 保存
    out_dir = REPO_ROOT / "docs" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "in_sample": {"period": f"{args.is_start}〜{args.is_end}", "best_params": best_params, "metrics": is_best},
        "out_of_sample": {"period": f"{args.oos_start}〜{args.oos_end}", "metrics": oos_metrics},
        "verdict": verdict,
    }
    out_path = out_dir / f"walkforward_{args.is_start}_{args.oos_end}.json"
    with out_path.open("w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n保存: {out_path}")


if __name__ == "__main__":
    main()
