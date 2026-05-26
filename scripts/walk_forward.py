"""
Walk-Forward Analysis — 過剰最適化（in-sample over-fit）の検証

手法:
1. ローリング3窓（デフォルト）: 各窓で IS=1年・OOS=1年 を1年ずつスライド
   - 窓1: IS=2022 → OOS=2023
   - 窓2: IS=2023 → OOS=2024
   - 窓3: IS=2024 → OOS=2025
2. 各窓で IS grid search → ベストパラメータ抽出 → OOS で固定検証
3. 各窓を「ロバスト判定基準」で pass/fail
4. 全窓集計で全体 verdict

ロバスト判定基準（事前定義）:
- OOS PF ≥ 1.0
- OOS 月リターン ≥ +0.1%
両方満たすとき "robust" 判定。

全体 verdict:
- 2/3 窓以上 robust       → ✅ Robust（戦略採用可）
- 1/3 窓 robust           → ⚠ Marginal（時期次第・要追加検証）
- 0/3 窓 robust           → ❌ Overfitted / Non-viable（戦略撤退）

恒久対策（2026-05-26 追加・docs/operational_notes.md 参照）:
- デフォルト workers=1（並列I/O 競合による subprocess timeout 多発を防ぐ）
- デフォルト subprocess_timeout=60s（速い組合せ用、遅い組合せは早期 N/A）
- --max-wall-clock-seconds で全体タイムアウト（default 3600s）
- ETA 自動監視: 10%進捗時点で初期推定の 5倍超なら abort
- --benchmark フラグで小規模事前実測（10 combos × 1窓）

単窓モード（後方互換）:
    python scripts/walk_forward.py --single --is-start 2024-01-04 --is-end 2024-12-30 \
        --oos-start 2025-01-06 --oos-end 2025-12-30
"""

import argparse
import itertools
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grid_search import GRID, run_one  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent

# rolling 3窓（α案）。J-Quants Light Plan 4年データ範囲に合わせた設計。
DEFAULT_WINDOWS: list[tuple[str, str, str, str]] = [
    ("2022-01-04", "2022-12-30", "2023-01-04", "2023-12-29"),
    ("2023-01-04", "2023-12-29", "2024-01-04", "2024-12-30"),
    ("2024-01-04", "2024-12-30", "2025-01-06", "2025-12-30"),
]

ROBUST_PF_THRESHOLD = 1.0
ROBUST_MONTHLY_PCT_THRESHOLD = 0.1
MIN_TRADES_FOR_IS_BEST = 5  # IS Grid Search のベスト選定で必要な最小取引数（trades=0 を弾く）

# 恒久対策パラメータ
ETA_INFLATION_ABORT_RATIO = 5.0   # 10%進捗時点の実 ETA が初期推定の 5倍を超えたら abort
ETA_INFLATION_WARN_RATIO = 2.0    # 同 2倍超で警告
ETA_CHECK_PROGRESS_PCT = 10       # ETA 再計算する進捗パーセント

# ベンチマーク用: 全 GRID から先頭 N 組合せだけ実行
BENCHMARK_COMBOS = 10
BENCHMARK_WINDOW = ("2024-01-04", "2024-12-30", "2025-01-06", "2025-12-30")


class WallClockExceeded(Exception):
    """全体タイムアウト到達時に raise される。"""


class ETAInflationAbort(Exception):
    """ETA 異常膨張による自動 abort 時に raise される。"""


def _runner(args_tuple):
    params, start, end, budget, max_positions, timeout, signal = args_tuple
    return params, run_one(
        params, start, end,
        budget=budget, max_positions=max_positions, timeout=timeout,
        signal=signal,
    )


def grid_search_period(
    start: str, end: str, workers: int,
    budget: int | None = None, max_positions: int | None = None,
    subprocess_timeout: int = 60,
    initial_eta_estimate: float | None = None,
    wall_clock_deadline: float | None = None,
    combos_override: list[dict] | None = None,
    signal: str | None = None,
) -> list[dict]:
    """指定期間で grid search を実行し、結果リストを返す。

    workers=1 ならシリアル実行（pickle ロード I/O 競合を回避）。
    workers≥2 なら ProcessPoolExecutor で並列実行。

    恒久対策:
    - wall_clock_deadline（monotonic 時刻）を超えたら WallClockExceeded を raise
    - 10% 進捗時点で実 ETA を再計算し、initial_eta_estimate の N倍超なら
      警告 or ETAInflationAbort を raise
    """
    keys = list(GRID.keys())
    combos = combos_override if combos_override is not None else [
        dict(zip(keys, vs)) for vs in itertools.product(*GRID.values())
    ]
    total = len(combos)
    print(f"  Grid search: {total} 組合せ ({start} 〜 {end}, workers={workers}, timeout={subprocess_timeout}s)", flush=True)
    if initial_eta_estimate is not None:
        print(f"    初期 ETA 推定: {initial_eta_estimate:.0f}s "
              f"(警告閾値 {ETA_INFLATION_WARN_RATIO}x, abort 閾値 {ETA_INFLATION_ABORT_RATIO}x)",
              flush=True)

    t0 = time.time()
    results: list[dict] = []
    completed = 0
    eta_check_threshold = max(1, int(total * ETA_CHECK_PROGRESS_PCT / 100))
    eta_checked = False

    def _check_health(completed_n: int) -> None:
        """全体タイムアウトと ETA 膨張をチェック。"""
        nonlocal eta_checked
        if wall_clock_deadline is not None and time.monotonic() >= wall_clock_deadline:
            raise WallClockExceeded(
                f"全体タイムアウト到達 ({wall_clock_deadline} monotonic)、"
                f"{completed_n}/{total} 完了時点で中断"
            )
        if not eta_checked and completed_n >= eta_check_threshold and initial_eta_estimate:
            elapsed = time.time() - t0
            measured_eta = elapsed / completed_n * total
            ratio = measured_eta / initial_eta_estimate
            print(f"    [ETA再計算 @{completed_n}/{total}] elapsed={elapsed:.0f}s → "
                  f"全体予測 {measured_eta:.0f}s ({ratio:.1f}× of {initial_eta_estimate:.0f}s)",
                  flush=True)
            if ratio >= ETA_INFLATION_ABORT_RATIO:
                raise ETAInflationAbort(
                    f"ETA が初期推定の {ratio:.1f}倍に膨張 — 異常終了。"
                    f"設定（workers/timeout）を見直してください"
                )
            elif ratio >= ETA_INFLATION_WARN_RATIO:
                print(f"    ⚠ ETA が初期推定の {ratio:.1f}倍 — 継続するが詰まりの可能性", flush=True)
            eta_checked = True

    def _report_progress(completed_n: int) -> None:
        if completed_n % 30 == 0 or completed_n == total:
            elapsed = time.time() - t0
            eta = elapsed / completed_n * (total - completed_n)
            print(f"    {completed_n}/{total}  elapsed {elapsed:.0f}s  ETA {eta:.0f}s", flush=True)

    if workers <= 1:
        # シリアル実行: pickle I/O 競合と subprocess timeout 多発を回避
        for c in combos:
            metrics = run_one(
                c, start, end,
                budget=budget, max_positions=max_positions, timeout=subprocess_timeout,
                signal=signal,
            )
            completed += 1
            if metrics is not None:
                results.append(metrics)
            _check_health(completed)
            _report_progress(completed)
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_runner, (c, start, end, budget, max_positions, subprocess_timeout, signal)): c
                for c in combos
            }
            try:
                for fut in as_completed(futures):
                    params, metrics = fut.result()
                    completed += 1
                    if metrics is not None:
                        results.append(metrics)
                    _check_health(completed)
                    _report_progress(completed)
            except (WallClockExceeded, ETAInflationAbort):
                # 残り futures を cancel して上位に伝播
                for f in futures:
                    f.cancel()
                ex.shutdown(wait=False, cancel_futures=True)
                raise
    return results


def _is_robust(oos_metrics: dict) -> bool:
    pf = oos_metrics.get("profit_factor") or 0
    monthly = oos_metrics.get("monthly_pnl_pct", 0)
    return pf >= ROBUST_PF_THRESHOLD and monthly >= ROBUST_MONTHLY_PCT_THRESHOLD


def evaluate_window(
    is_start: str, is_end: str, oos_start: str, oos_end: str,
    workers: int, label: str,
    budget: int | None = None, max_positions: int | None = None,
    subprocess_timeout: int = 60,
    initial_eta_estimate: float | None = None,
    wall_clock_deadline: float | None = None,
    signal: str | None = None,
) -> dict | None:
    """1窓を評価して IS/OOS メトリクスと robust 判定を返す。失敗時 None。"""
    print(f"\n{'='*80}")
    print(f"[Window {label}]  IS={is_start}〜{is_end}  OOS={oos_start}〜{oos_end}")
    print(f"{'='*80}")

    print(f"  [{label}-IS] Grid Search", flush=True)
    is_results = grid_search_period(
        is_start, is_end, workers,
        budget=budget, max_positions=max_positions, subprocess_timeout=subprocess_timeout,
        initial_eta_estimate=initial_eta_estimate,
        wall_clock_deadline=wall_clock_deadline,
        signal=signal,
    )
    if not is_results:
        print(f"  ❌ Window {label}: IS で結果が得られず")
        return None

    # trades=0 / 極端に少ない取引数は除外（"何もしない=収益0%" がベストになる欠陥対策）
    qualified = [r for r in is_results if r.get("trades", 0) >= MIN_TRADES_FOR_IS_BEST]
    print(f"  [{label}-IS] qualified (trades≥{MIN_TRADES_FOR_IS_BEST}): {len(qualified)}/{len(is_results)}")
    if not qualified:
        print(f"  ❌ Window {label}: IS で trades≥{MIN_TRADES_FOR_IS_BEST} のパラメータがなし — 戦略が機能していない")
        return None
    qualified.sort(key=lambda r: r["monthly_pnl_pct"], reverse=True)
    is_best = qualified[0]
    best_params = is_best["params"]

    print(f"  [{label}-IS] Best:", end=" ")
    for k, v in best_params.items():
        if v is not None:
            print(f"{k}={v}", end=" ")
    print(
        f"\n  [{label}-IS] trades={is_best['trades']} "
        f"monthly={is_best['monthly_pnl_pct']:+.2f}% "
        f"PF={is_best.get('profit_factor') or 0:.2f} "
        f"DD={is_best['max_dd_pct']:+.1f}%"
    )

    print(f"  [{label}-OOS] 固定パラメータで実行", flush=True)
    oos_metrics = run_one(
        best_params, oos_start, oos_end,
        budget=budget, max_positions=max_positions, timeout=subprocess_timeout,
        signal=signal,
    )
    if oos_metrics is None:
        print(f"  ❌ Window {label}: OOS バックテスト失敗")
        return None
    print(
        f"  [{label}-OOS] trades={oos_metrics['trades']} "
        f"monthly={oos_metrics['monthly_pnl_pct']:+.2f}% "
        f"PF={oos_metrics.get('profit_factor') or 0:.2f} "
        f"DD={oos_metrics['max_dd_pct']:+.1f}%"
    )

    robust = _is_robust(oos_metrics)
    print(f"  [{label}] Robust判定: {'✅ PASS' if robust else '❌ FAIL'}  "
          f"(PF≥{ROBUST_PF_THRESHOLD}, monthly≥{ROBUST_MONTHLY_PCT_THRESHOLD}%)")

    return {
        "label": label,
        "in_sample": {
            "period": f"{is_start}〜{is_end}",
            "best_params": best_params,
            "metrics": is_best,
        },
        "out_of_sample": {
            "period": f"{oos_start}〜{oos_end}",
            "metrics": oos_metrics,
        },
        "robust": robust,
    }


def _print_summary(windows: list[dict]) -> str:
    """全窓のサマリを表で出力し、全体 verdict 文字列を返す。"""
    print(f"\n{'='*80}")
    print("Walk-Forward Summary")
    print(f"{'='*80}")

    header = f"{'Window':<8} {'IS period':<22} {'OOS period':<22} {'OOS trades':>10} {'OOS monthly%':>13} {'OOS PF':>7} {'OOS DD%':>8} {'Robust':>7}"
    print(header)
    print("-" * len(header))

    robust_count = 0
    for w in windows:
        oos = w["out_of_sample"]["metrics"]
        is_period = w["in_sample"]["period"]
        oos_period = w["out_of_sample"]["period"]
        pf = oos.get("profit_factor") or 0
        robust = w["robust"]
        if robust:
            robust_count += 1
        print(
            f"{w['label']:<8} {is_period:<22} {oos_period:<22} "
            f"{oos['trades']:>10d} {oos['monthly_pnl_pct']:>+12.2f}% "
            f"{pf:>7.2f} {oos['max_dd_pct']:>+7.1f}% "
            f"{'✅' if robust else '❌':>7}"
        )

    n = len(windows)
    threshold = (n + 1) // 2  # 過半数: n=1→1, n=2→1, n=3→2
    print(f"\nロバスト窓: {robust_count}/{n}")
    if n >= 3 and robust_count >= 2:
        verdict = f"✅ Robust ({robust_count}/{n} 窓で基準達成) — 戦略採用検討可"
    elif n < 3 and robust_count >= threshold:
        verdict = f"⚠ Inconclusive ({robust_count}/{n} 窓・サンプル少) — 追加窓で再検証推奨"
    elif robust_count >= 1:
        verdict = f"⚠ Marginal ({robust_count}/{n} 窓のみ達成) — 時期依存・追加検証推奨"
    else:
        verdict = f"❌ Overfitted / Non-viable (0/{n} 窓で基準未達) — 戦略撤退検討"

    print(f"Verdict: {verdict}")
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--single", action="store_true",
        help="単窓モード（後方互換）。--is-start/--is-end/--oos-start/--oos-end を要求"
    )
    parser.add_argument("--is-start",   default="2024-01-04")
    parser.add_argument("--is-end",     default="2024-12-30")
    parser.add_argument("--oos-start",  default="2025-01-06")
    parser.add_argument("--oos-end",    default="2025-12-30")
    parser.add_argument("--workers", type=int, default=1,
                        help="並列度（default: 1 = シリアル）。docs/operational_notes.md 参照")
    parser.add_argument("--budget", type=int, default=None,
                        help="バックテスト予算（円）。指定しなければ backtest.py のデフォルト")
    parser.add_argument("--max-positions", type=int, default=None,
                        help="同時保有銘柄数の上限。指定しなければ backtest.py のデフォルト")
    parser.add_argument("--subprocess-timeout", type=int, default=60,
                        help="各 subprocess の timeout 秒（default: 60）")
    parser.add_argument("--max-wall-clock-seconds", type=int, default=3600,
                        help="全体タイムアウト秒（default: 3600 = 1時間）。超過で自動 abort")
    parser.add_argument("--benchmark", action="store_true",
                        help=f"事前ベンチ（{BENCHMARK_COMBOS} combos × 1窓）で ETA を実測してから本実行")
    parser.add_argument("--signal", choices=["pullback", "ma_cross"], default=None,
                        help="シグナル種別を全 backtest に渡す（default: backtest.py のデフォルト=pullback）")
    parser.add_argument("--out-label", default="rolling3", help="出力JSONのファイル名ラベル")
    args = parser.parse_args()

    print(f"{'='*80}")
    print(f"Walk-Forward Analysis")
    print(f"{'='*80}")

    if args.single:
        windows_def = [(args.is_start, args.is_end, args.oos_start, args.oos_end)]
        print(f"Mode: single window")
    else:
        windows_def = DEFAULT_WINDOWS
        print(f"Mode: rolling {len(windows_def)} windows (default α案)")
    print(f"並列度: {args.workers}")
    if args.budget is not None:
        print(f"Budget: ¥{args.budget:,}")
    if args.max_positions is not None:
        print(f"Max positions: {args.max_positions}")
    print(f"subprocess timeout: {args.subprocess_timeout}s")
    print(f"全体タイムアウト: {args.max_wall_clock_seconds}s")
    if args.signal is not None:
        print(f"Signal: {args.signal}")
    print(f"ロバスト基準: OOS PF ≥ {ROBUST_PF_THRESHOLD} AND OOS 月リターン ≥ {ROBUST_MONTHLY_PCT_THRESHOLD}%")
    print(f"IS Best 選定: trades ≥ {MIN_TRADES_FOR_IS_BEST} のパラメータからのみ選出")

    # ベンチマーク先行（実測 ETA 推定）
    initial_eta_per_window: float | None = None
    if args.benchmark:
        print(f"\n{'='*80}")
        print(f"[BENCHMARK] {BENCHMARK_COMBOS} combos × 1 window で実測中…")
        print(f"{'='*80}")
        keys = list(GRID.keys())
        all_combos = [dict(zip(keys, vs)) for vs in itertools.product(*GRID.values())]
        bench_combos = all_combos[:BENCHMARK_COMBOS]
        bench_t0 = time.time()
        bench_results = grid_search_period(
            BENCHMARK_WINDOW[0], BENCHMARK_WINDOW[1], args.workers,
            budget=args.budget, max_positions=args.max_positions,
            subprocess_timeout=args.subprocess_timeout,
            combos_override=bench_combos,
            signal=args.signal,
        )
        bench_elapsed = time.time() - bench_t0
        per_combo = bench_elapsed / max(BENCHMARK_COMBOS, 1)
        total_combos = len(all_combos)
        per_window = per_combo * total_combos
        total_estimate = per_window * len(windows_def)
        print(f"\n[BENCHMARK 結果] {BENCHMARK_COMBOS} combos: {bench_elapsed:.1f}s "
              f"({per_combo:.2f}s/combo, {len(bench_results)} valid)")
        print(f"  1窓 ({total_combos} combos) 推定: {per_window:.0f}s "
              f"({per_window/60:.1f}分)")
        print(f"  全 {len(windows_def)} 窓推定: {total_estimate:.0f}s "
              f"({total_estimate/60:.1f}分)")
        if total_estimate > args.max_wall_clock_seconds:
            print(f"⚠ 推定 ({total_estimate:.0f}s) > 全体タイムアウト ({args.max_wall_clock_seconds}s)")
            print(f"  --max-wall-clock-seconds を上げるか、--workers を増やしてください")
        initial_eta_per_window = per_window

    overall_t0 = time.monotonic()
    wall_clock_deadline = overall_t0 + args.max_wall_clock_seconds

    window_results: list[dict] = []
    try:
        for i, (is_s, is_e, oos_s, oos_e) in enumerate(windows_def, 1):
            label = f"W{i}"
            r = evaluate_window(
                is_s, is_e, oos_s, oos_e, args.workers, label,
                budget=args.budget, max_positions=args.max_positions,
                subprocess_timeout=args.subprocess_timeout,
                initial_eta_estimate=initial_eta_per_window,
                wall_clock_deadline=wall_clock_deadline,
                signal=args.signal,
            )
            if r is None:
                print(f"⚠ Window {label} スキップ")
                continue
            window_results.append(r)
    except WallClockExceeded as e:
        print(f"\n❌ 全体タイムアウト: {e}", flush=True)
    except ETAInflationAbort as e:
        print(f"\n❌ ETA 異常膨張で自動 abort: {e}", flush=True)

    if not window_results:
        print("\n全窓で結果が得られませんでした。")
        sys.exit(1)

    verdict = _print_summary(window_results)

    out_dir = REPO_ROOT / "docs" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "single" if args.single else "rolling",
        "robust_criteria": {
            "profit_factor_min": ROBUST_PF_THRESHOLD,
            "monthly_pnl_pct_min": ROBUST_MONTHLY_PCT_THRESHOLD,
        },
        "windows": window_results,
        "verdict": verdict,
    }
    out_path = out_dir / f"walkforward_{args.out_label}.json"
    with out_path.open("w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n保存: {out_path}")


if __name__ == "__main__":
    main()
