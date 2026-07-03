"""
S-Quant パラメータ Grid Search

scripts/backtest.py を subprocess で繰り返し呼び出し、各組合せのメトリクスを集計する。
OHLCV キャッシュ（.backtest_cache/）が必要なため、先に backtest.py を1回フル実行しておくこと。

評価指標（ランキング基準）:
- monthly_pnl_pct: 月平均リターン%（最大化）
- profit_factor: PF（参考）
- max_dd_pct: 最大ドローダウン%（参考）
"""

import argparse
import itertools
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
BACKTEST_SCRIPT = REPO_ROOT / "scripts" / "backtest.py"

# in-process bridge to backtest.py: re-uses ~33 MB pickled OHLCV across the
# whole grid instead of paying for one subprocess + pickle reload per cell.
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from backtest import load_cache as _load_cache_for_backtest  # noqa: E402
from backtest import precompute_daily_candidates, run_one_backtest  # noqa: E402

# パラメータ Grid（180通り = 5×3×4×3）
GRID = {
    "target_profit": [0.02, 0.03, 0.04, 0.05, 0.06],
    "atr_mult":      [1.5, 2.0, 2.5],
    "rsi_upper":     [45, 50, 55, 60],
    "time_stop":     [3, 5, 7],
}


def run_one(
    params: dict, start: str, end: str,
    budget: int | None = None, max_positions: int | None = None,
    timeout: int = 120, signal: str | None = None,
) -> dict | None:
    """1組合せ実行。stdoutから __METRICS_JSON__ 行を抽出して返す。"""
    cmd = [
        str(PYTHON_BIN), str(BACKTEST_SCRIPT),
        "--start", start, "--end", end,
        "--target-profit", str(params["target_profit"]),
        "--atr-mult",      str(params["atr_mult"]),
        "--rsi-upper",     str(params["rsi_upper"]),
        "--time-stop",     str(params["time_stop"]),
        "--quiet", "--json",
    ]
    if budget is not None:
        cmd += ["--budget", str(budget)]
    if max_positions is not None:
        cmd += ["--max-positions", str(max_positions)]
    if signal is not None:
        cmd += ["--signal", signal]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            cwd=REPO_ROOT, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None

    # 全行から __METRICS_JSON__ を探す
    for line in result.stdout.splitlines():
        if line.startswith("__METRICS_JSON__"):
            try:
                return json.loads(line[len("__METRICS_JSON__"):])
            except json.JSONDecodeError:
                return None
    return None


class InProcessGridRunner:
    """One in-process grid / walk-forward run sharing a single loaded cache.

    subprocess 経路 (``run_one``) と同じ metrics 形状を返すので、下流の
    集計・ソート・robust 判定はそのまま動く。``run()`` は
    ``_apply_param_overrides`` 経由でモジュールレベル定数を書き換えるため
    スレッド安全ではない — 呼び出しは必ずシリアルにすること。
    """

    def __init__(
        self, data: dict, *,
        budget: int | None = None, max_positions: int | None = None,
        signal: str | None = None,
    ) -> None:
        self.data = data
        self.budget = budget
        self.max_positions = max_positions
        self.signal = signal
        # 日次候補リストの memo。候補は出口パラメータに依存しないため、
        # ma_cross は (期間) 単位で、pullback は (期間, RSI帯) 単位で共有できる。
        self._candidates_memo: dict[tuple, dict] = {}

    def _effective_signal(self) -> str:
        return self.signal if self.signal is not None else "ma_cross"

    def _precomputed_for(self, params: dict, start: str, end: str) -> dict:
        from datetime import date as _date
        signal = self._effective_signal()
        if signal == "ma_cross":
            key: tuple = (start, end, signal)
            rsi_kwargs: dict = {}
        else:  # pullback は RSI 帯がシグナル条件に入る
            key = (start, end, signal, params.get("rsi_upper"), params.get("rsi_lower"))
            rsi_kwargs = {
                "rsi_upper": params.get("rsi_upper"),
                "rsi_lower": params.get("rsi_lower"),
            }
        pre = self._candidates_memo.get(key)
        if pre is None:
            pre = precompute_daily_candidates(
                _date.fromisoformat(start), _date.fromisoformat(end), self.data,
                signal_strategy=signal, **rsi_kwargs,
            )
            self._candidates_memo[key] = pre
        return pre

    def run(self, params: dict, start: str, end: str) -> dict | None:
        from datetime import date as _date
        kwargs: dict = {}
        if self.budget is not None:
            kwargs["budget"] = self.budget
        if self.max_positions is not None:
            kwargs["max_positions"] = self.max_positions
        if self.signal is not None:
            kwargs["signal_strategy"] = self.signal
        try:
            return run_one_backtest(
                _date.fromisoformat(start), _date.fromisoformat(end), self.data,
                target_profit=params.get("target_profit"),
                atr_mult=params.get("atr_mult"),
                rsi_upper=params.get("rsi_upper"),
                rsi_lower=params.get("rsi_lower"),
                time_stop=params.get("time_stop"),
                precomputed_candidates=self._precomputed_for(params, start, end),
                **kwargs,
            )
        except Exception:
            # subprocess 経路は None を返す仕様に合わせるが、in-process では
            # traceback を残さないとデバッグ不能になるため stderr に出す。
            import traceback
            print(f"  [inprocess] FAILED: {params}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            return None


def load_cache_for_grid(start: str, end: str, cache_dir: str = ".backtest_cache") -> dict:
    """Load the OHLCV/fundamentals cache once for an in-process grid run."""
    from datetime import date as _date
    return _load_cache_for_backtest(
        _date.fromisoformat(start), _date.fromisoformat(end),
        cache_dir=Path(cache_dir),
    )


def _runner(args_tuple):
    params, start, end, budget, max_positions, signal = args_tuple
    return params, run_one(
        params, start, end, budget=budget, max_positions=max_positions, signal=signal,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-04")
    parser.add_argument("--end",   default="2025-12-30")
    parser.add_argument("--mode", choices=["inprocess", "subprocess"], default="inprocess",
                        help="inprocess: キャッシュ1回ロードでシリアル実行（高速・推奨）。"
                             "subprocess: 旧経路（同値性検証用）")
    parser.add_argument("--workers", type=int, default=4,
                        help="並列ワーカー数（subprocess モードのみ。inprocess はシリアル実行）")
    parser.add_argument("--top", type=int, default=20, help="表示する上位件数")
    parser.add_argument("--budget", type=int, default=None,
                        help="バックテスト予算（円）。指定しなければ backtest.py のデフォルト")
    parser.add_argument("--max-positions", type=int, default=None,
                        help="同時保有銘柄数の上限。指定しなければ backtest.py のデフォルト")
    parser.add_argument("--signal", choices=["pullback", "ma_cross"], default=None,
                        help="シグナル種別。指定しなければ backtest.py のデフォルト")
    args = parser.parse_args()

    keys = list(GRID.keys())
    combos = [dict(zip(keys, vs)) for vs in itertools.product(*GRID.values())]
    total = len(combos)
    print(f"Grid search: {total} 組合せ, mode={args.mode}"
          + (f", {args.workers} 並列" if args.mode == "subprocess" else " (シリアル)"), flush=True)
    print(f"パラメータ: {GRID}", flush=True)
    print()

    t0 = time.time()
    results: list[dict] = []
    completed = 0

    def _report(completed_n: int) -> None:
        if completed_n % 10 == 0 or completed_n == total:
            elapsed = time.time() - t0
            eta = elapsed / completed_n * (total - completed_n)
            print(f"  [{completed_n}/{total}] elapsed {elapsed:.0f}s  ETA {eta:.0f}s", flush=True)

    if args.mode == "inprocess":
        data = load_cache_for_grid(args.start, args.end)
        runner = InProcessGridRunner(
            data, budget=args.budget, max_positions=args.max_positions, signal=args.signal,
        )
        for c in combos:
            metrics = runner.run(c, args.start, args.end)
            completed += 1
            if metrics is None:
                print(f"  [{completed}/{total}] FAILED: {c}", flush=True)
                continue
            results.append(metrics)
            _report(completed)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(
                    _runner,
                    (c, args.start, args.end, args.budget, args.max_positions, args.signal),
                ): c
                for c in combos
            }
            for fut in as_completed(futures):
                params, metrics = fut.result()
                completed += 1
                if metrics is None:
                    print(f"  [{completed}/{total}] FAILED: {params}", flush=True)
                    continue
                results.append(metrics)
                _report(completed)

    if not results:
        print("結果なし。backtest.py のキャッシュやエラーを確認してください。")
        sys.exit(1)

    # ランキング: 月平均リターン% 降順
    results.sort(key=lambda r: r["monthly_pnl_pct"], reverse=True)

    print(f"\n{'='*100}")
    print(f"Top {args.top} by monthly_pnl_pct (out of {len(results)})")
    print(f"{'='*100}")
    print(
        f"{'rank':>4} {'tp':>5} {'atr':>4} {'rsi':>4} {'ts':>3} "
        f"{'trades':>6} {'win%':>5} {'monthly%':>8} {'total':>8} {'PF':>5} {'maxDD%':>7} "
        f"{'reasons':<40}"
    )
    for i, r in enumerate(results[: args.top], 1):
        p = r["params"]
        reasons = ",".join(f"{k}:{v}" for k, v in sorted(r["by_reason"].items()))
        pf_disp = f"{r['profit_factor']:>5.2f}" if r["profit_factor"] != float("inf") else "  inf"
        print(
            f"{i:>4} {p['target_profit']:>5.2f} {p['atr_mult']:>4.1f} "
            f"{int(p['rsi_upper']):>4d} {int(p['time_stop']):>3d} "
            f"{r['trades']:>6d} {r['win_rate']*100:>5.1f} "
            f"{r['monthly_pnl_pct']:>+8.2f} {r['total_pnl']:>+8.0f} "
            f"{pf_disp} {r['max_dd_pct']:>+7.1f} "
            f"{reasons:<40}"
        )

    # ベスト3の詳細表示
    print(f"\n{'='*100}")
    print("Top 3 詳細")
    print(f"{'='*100}")
    for i, r in enumerate(results[:3], 1):
        p = r["params"]
        print(f"\n[{i}位] target_profit={p['target_profit']} atr_mult={p['atr_mult']} "
              f"rsi_upper={p['rsi_upper']} time_stop={p['time_stop']}")
        print(f"  trades={r['trades']}  signals={r['signals']}  "
              f"win_rate={r['win_rate']*100:.1f}%  trades/month={r['trades_per_month']:.2f}")
        print(f"  total_pnl=¥{r['total_pnl']:+,.0f}  monthly=¥{r['monthly_pnl']:+,.0f} ({r['monthly_pnl_pct']:+.2f}%)")
        print(f"  avg_win=¥{r['avg_win']:+,.0f}  avg_loss=¥{r['avg_loss']:+,.0f}  PF={r['profit_factor']:.2f}")
        print(f"  max_dd=¥{r['max_dd']:+,.0f} ({r['max_dd_pct']:+.1f}%)")
        print(f"  by_reason: {r['by_reason']}")

    # サマリ保存
    out_path = REPO_ROOT / ".backtest_cache" / "grid_search_results.json"
    with out_path.open("w") as f:
        json.dump(results, f, indent=2)
    print(f"\n全結果を保存: {out_path}")


if __name__ == "__main__":
    main()
