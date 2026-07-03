"""subprocess 経路と in-process 経路のバックテスト同値性テスト。

grid_search / walk_forward を in-process 化した際の最重要保証:
「同一パラメータ・同一データなら、旧 subprocess 経路と完全に同じ metrics を返す」。

合成データ（決定的・ネットワーク不要）を一時キャッシュに書き、
1) `python scripts/backtest.py --json` の __METRICS_JSON__ 出力
2) `backtest.run_one_backtest()` の戻り値
を比較する。
"""

import json
import math
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backtest as bt  # noqa: E402

START = "2024-07-01"
END = "2024-12-30"
CACHE_NAME = "data_2024-01-01_2024-12-30.pkl"

# 同値性を確認するパラメータセット（2セット目は in-process の上書きリセットも検証する）
PARAM_SETS = [
    {"target_profit": 0.04, "atr_mult": 2.0, "rsi_upper": 55, "time_stop": 5},
    {"target_profit": 0.06, "atr_mult": 1.5, "rsi_upper": 60, "time_stop": 3},
]

# 比較する metrics キー（params はサブセット比較、float は approx）
INT_KEYS = ["trades", "signals", "gap_up_skipped", "insufficient_skipped",
            "max_consecutive_wins", "max_consecutive_losses", "max_dd_duration_trades"]
FLOAT_KEYS = ["total_pnl", "total_return_pct", "monthly_pnl_pct", "cagr_pct",
              "final_equity", "win_rate", "expectancy", "profit_factor",
              "max_dd", "max_dd_pct", "sharpe_ratio", "sortino_ratio", "calmar_ratio"]


def _real_tickers(n: int = 3) -> list[str]:
    df = pd.read_csv(REPO_ROOT / "data" / "universe.csv", comment="#")
    return df["ticker"].dropna().astype(str).tolist()[:n]


def _build_synthetic_cache(cache_dir: Path) -> None:
    """上昇トレンド＋周期的な出来高サージを持つ決定的な合成データを書き出す。

    MA クロス（5MA>25MA・25MA上向き）が成立し、サージ日にシグナルが出る形。
    価格は Phase 1 の ¥100〜¥1,000 帯に収める。
    """
    tickers = _real_tickers(3)
    dates = pd.bdate_range("2024-01-01", "2024-12-30")
    n = len(dates)
    t = np.arange(n, dtype=float)

    adj_close = {}
    volume = {}
    full_cache = {}
    for k, ticker in enumerate(tickers):
        # 緩い上昇 + 銘柄ごとに位相のずれた波（決定的）
        close = 350.0 + 30 * k + 0.5 * t + 12 * np.sin((t + 7 * k) / 6.0)
        close = np.round(close, 1)
        # 5営業日ごとに出来高 2倍（surge ratio > 1.2 を確実に満たす）
        vol = np.where((t.astype(int) + 2 * k) % 5 == 0, 220_000.0, 100_000.0)

        adj_close[ticker] = close
        volume[ticker] = vol
        full_cache[ticker] = pd.DataFrame(
            {
                "AdjO": np.round(close * 0.998, 1),
                "AdjH": np.round(close * 1.012, 1),
                "AdjL": np.round(close * 0.988, 1),
                "AdjC": close,
                "AdjVo": vol,
            },
            index=dates,
        )

    fundamentals = pd.DataFrame(
        {
            "market_cap_jpy": [50e8, 80e8, 60e8],
            "pbr": [1.2, 0.9, 1.5],
            "equity_ratio": [0.5, 0.6, 0.45],
            "avg_5d_trading_value_jpy": [2e8, 3e8, 2.5e8],
        },
        index=tickers,
    )

    import pickle
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / CACHE_NAME).open("wb") as f:
        pickle.dump(
            {
                "adj_close": pd.DataFrame(adj_close, index=dates),
                "volume": pd.DataFrame(volume, index=dates),
                "full_cache": full_cache,
                "fundamentals": fundamentals,
            },
            f,
        )


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("bt_cache")
    _build_synthetic_cache(d)
    return d


def _run_subprocess(cache_dir: Path, params: dict) -> dict:
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "backtest.py"),
        "--start", START, "--end", END,
        "--cache-dir", str(cache_dir),
        "--budget", "200000", "--max-positions", "2", "--signal", "ma_cross",
        "--target-profit", str(params["target_profit"]),
        "--atr-mult", str(params["atr_mult"]),
        "--rsi-upper", str(params["rsi_upper"]),
        "--time-stop", str(params["time_stop"]),
        "--quiet", "--json",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=180,
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr[-2000:]}"
    for line in result.stdout.splitlines():
        if line.startswith("__METRICS_JSON__"):
            return json.loads(line[len("__METRICS_JSON__"):])
    raise AssertionError(f"__METRICS_JSON__ not found:\n{result.stdout[-2000:]}")


def _run_inprocess(
    cache_dir: Path, params: dict,
    signal: str = "ma_cross", precomputed: bool = False,
) -> dict:
    data = bt.load_cache(date.fromisoformat(START), date.fromisoformat(END), cache_dir=cache_dir)
    pre = None
    if precomputed:
        rsi_kwargs = {} if signal == "ma_cross" else {"rsi_upper": params["rsi_upper"]}
        pre = bt.precompute_daily_candidates(
            date.fromisoformat(START), date.fromisoformat(END), data,
            signal_strategy=signal, **rsi_kwargs,
        )
    return bt.run_one_backtest(
        date.fromisoformat(START), date.fromisoformat(END), data,
        budget=200_000, max_positions=2, signal_strategy=signal,
        target_profit=params["target_profit"],
        atr_mult=params["atr_mult"],
        rsi_upper=params["rsi_upper"],
        time_stop=params["time_stop"],
        precomputed_candidates=pre,
    )


def _assert_metrics_equal(sub: dict, inp: dict, label: str) -> None:
    for k in INT_KEYS:
        assert sub[k] == inp[k], f"{label}: {k} subprocess={sub[k]} inprocess={inp[k]}"
    for k in FLOAT_KEYS:
        s, i = sub[k], inp[k]
        if isinstance(s, float) and math.isinf(s):
            assert math.isinf(i), f"{label}: {k} subprocess=inf inprocess={i}"
        else:
            assert i == pytest.approx(s, rel=1e-9, abs=1e-9), \
                f"{label}: {k} subprocess={s} inprocess={i}"
    assert sub["by_reason"] == inp["by_reason"], f"{label}: by_reason mismatch"


class TestInprocessEquivalence:
    @pytest.mark.parametrize("params", PARAM_SETS, ids=["set_a", "set_b"])
    def test_metrics_match_subprocess(self, cache_dir, params):
        sub = _run_subprocess(cache_dir, params)
        inp = _run_inprocess(cache_dir, params)
        _assert_metrics_equal(sub, inp, label=str(params))
        # 取引が発生していること（"0件同士で一致" の空テストを防ぐ）
        assert inp["trades"] > 0

    def test_back_to_back_inprocess_no_state_leak(self, cache_dir):
        """同一プロセスで A→B→A と回しても A の結果が変わらない（状態リーク検出）"""
        a1 = _run_inprocess(cache_dir, PARAM_SETS[0])
        _ = _run_inprocess(cache_dir, PARAM_SETS[1])
        a2 = _run_inprocess(cache_dir, PARAM_SETS[0])
        _assert_metrics_equal(a1, a2, label="A-rerun")


class TestPrecomputeEquivalence:
    """日次候補の事前計算経路が、毎日スキャンする経路と同値であることの保証。"""

    @pytest.mark.parametrize("params", PARAM_SETS, ids=["set_a", "set_b"])
    def test_matches_scan_path_ma_cross(self, cache_dir, params):
        scan = _run_inprocess(cache_dir, params)
        pre = _run_inprocess(cache_dir, params, precomputed=True)
        _assert_metrics_equal(scan, pre, label=f"precompute {params}")
        assert pre["trades"] > 0

    def test_matches_scan_path_pullback(self, cache_dir):
        """pullback は RSI 帯がシグナル条件に入る — その経路でも同値。"""
        params = PARAM_SETS[0]
        scan = _run_inprocess(cache_dir, params, signal="pullback")
        pre = _run_inprocess(cache_dir, params, signal="pullback", precomputed=True)
        _assert_metrics_equal(scan, pre, label="precompute pullback")

    def test_runner_memoizes_and_matches(self, cache_dir):
        """InProcessGridRunner が memo を共有しつつ、直接実行と同じ metrics を返す。"""
        from grid_search import InProcessGridRunner

        data = bt.load_cache(
            date.fromisoformat(START), date.fromisoformat(END), cache_dir=cache_dir,
        )
        runner = InProcessGridRunner(data, budget=200_000, max_positions=2, signal="ma_cross")
        r_a = runner.run(PARAM_SETS[0], START, END)
        _ = runner.run(PARAM_SETS[1], START, END)
        # ma_cross の候補は出口パラメータに依存しないため memo は期間単位で1件
        assert len(runner._candidates_memo) == 1
        base = _run_inprocess(cache_dir, PARAM_SETS[0])
        _assert_metrics_equal(base, r_a, label="runner vs direct")
