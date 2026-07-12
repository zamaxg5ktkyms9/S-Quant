"""Unit tests for V-4 Monte Carlo distribution estimation."""

import sys

import numpy as np
import pytest

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from monte_carlo import (
    bootstrap_annual_paths,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    moments,
    sharpe_per_trade,
    summarize_paths,
    trial_sharpe_variance_from_grid,
)


def _rng():
    return np.random.default_rng(7)


class TestBootstrapAnnualPaths:
    def test_shapes_and_determinism(self):
        pnl = np.array([100.0, -50.0, 200.0, -80.0, 30.0])
        a = bootstrap_annual_paths(pnl, trades_per_year=90, n_sims=500,
                                   block_len=1, rng=np.random.default_rng(1))
        b = bootstrap_annual_paths(pnl, trades_per_year=90, n_sims=500,
                                   block_len=1, rng=np.random.default_rng(1))
        assert a["annual_pnl"].shape == (500,)
        assert np.array_equal(a["annual_pnl"], b["annual_pnl"])

    def test_all_losing_trades_always_trip_cb(self):
        """毎回 -¥50k なら 2 トレード目で純損失 ¥100k ≥ ¥90k → 全パス発動"""
        pnl = np.array([-50_000.0])
        out = bootstrap_annual_paths(pnl, trades_per_year=10, n_sims=200,
                                     block_len=1, rng=_rng())
        assert out["cb_tripped"].all()
        assert (out["annual_pnl"] == -500_000.0).all()

    def test_all_winning_trades_never_trip(self):
        pnl = np.array([10_000.0])
        out = bootstrap_annual_paths(pnl, trades_per_year=90, n_sims=200,
                                     block_len=1, rng=_rng())
        assert not out["cb_tripped"].any()
        assert (out["annual_pnl"] == 900_000.0).all()
        assert (out["max_dd"] == 0.0).all()

    def test_cb_uses_running_net_loss_not_final(self):
        """先に大損→後で回復しても CB は発動している（sticky・経路依存）"""
        # -95k → +200k の2種だけの列。95k 損が先頭に来たパスは必ず発動
        pnl = np.array([-95_000.0, 200_000.0])
        out = bootstrap_annual_paths(pnl, trades_per_year=2, n_sims=2000,
                                     block_len=1, rng=_rng())
        tripped = out["cb_tripped"]
        final_positive = out["annual_pnl"] > 0
        # 発動かつ年間黒字のパスが存在する = 経路で判定している証拠
        assert (tripped & final_positive).any()

    def test_block_bootstrap_preserves_consecutive_pairs(self):
        """block_len=trades_per_year なら元系列の連続部分列そのもの"""
        pnl = np.array([1.0, 2.0, 3.0, 4.0])
        out = bootstrap_annual_paths(pnl, trades_per_year=4, n_sims=50,
                                     block_len=4, rng=_rng())
        # 循環連続4件の和は常に 1+2+3+4 = 10
        assert (out["annual_pnl"] == 10.0).all()

    def test_checkpoint_cum_matches_walk(self):
        """全トレード +10k なら 30 取引時点の累積は必ず +300k"""
        pnl = np.array([10_000.0])
        out = bootstrap_annual_paths(pnl, trades_per_year=90, n_sims=50,
                                     block_len=1, rng=_rng())
        assert (out["checkpoint_cum"][30] == 300_000.0).all()
        assert set(out["checkpoint_cum"]) == {30, 60, 90}
        s = summarize_paths(out)
        assert s["checkpoint_cum_percentiles_jpy"]["30"]["5"] == 300_000.0

    def test_checkpoints_beyond_year_are_dropped(self):
        pnl = np.array([10_000.0])
        out = bootstrap_annual_paths(pnl, trades_per_year=45, n_sims=10,
                                     block_len=1, rng=_rng())
        assert set(out["checkpoint_cum"]) == {30}

    def test_empty_pnl_rejected(self):
        with pytest.raises(ValueError):
            bootstrap_annual_paths(np.array([]), trades_per_year=10, n_sims=10,
                                   block_len=1, rng=_rng())


class TestSummarizePaths:
    def test_probabilities_and_percentiles(self):
        paths = {
            "annual_pnl": np.array([-100_000.0, 50_000.0, 120_000.0, 200_000.0]),
            "cb_tripped": np.array([True, False, False, False]),
            "max_dd": np.array([-120_000.0, -30_000.0, -20_000.0, -10_000.0]),
        }
        s = summarize_paths(paths, initial_capital=600_000.0, target_jpy=100_000.0)
        assert s["p_cb_within_1y"] == 0.25
        assert s["p_annual_positive"] == 0.75
        assert s["p_annual_ge_target"] == 0.5
        assert s["annual_pnl_percentiles_jpy"]["50"] == 85_000.0
        # リターン% = pnl / 初期資本
        assert s["annual_return_percentiles_pct"]["50"] == pytest.approx(85_000 / 6_000)


class TestSharpeAndDSR:
    def test_sharpe_per_trade(self):
        r = np.array([0.01, -0.01, 0.01, -0.01, 0.02])
        assert sharpe_per_trade(r) == pytest.approx(
            r.mean() / r.std(ddof=1))

    def test_expected_max_sharpe_increases_with_trials(self):
        v = 0.02
        assert expected_max_sharpe(v, 1) == 0.0
        assert expected_max_sharpe(v, 100) < expected_max_sharpe(v, 1000)
        assert expected_max_sharpe(0.0, 100) == 0.0

    def test_dsr_half_when_sr_equals_benchmark(self):
        assert deflated_sharpe_ratio(0.2, 100, 0.0, 3.0, 0.2) == pytest.approx(0.5)

    def test_dsr_near_one_when_sr_dominates(self):
        assert deflated_sharpe_ratio(0.5, 400, 0.0, 3.0, 0.05) > 0.99

    def test_dsr_penalized_by_fat_tails(self):
        """尖度が大きいほど（テール太いほど）同じ SR でも DSR は下がる"""
        thin = deflated_sharpe_ratio(0.2, 100, 0.0, 3.0, 0.1)
        fat = deflated_sharpe_ratio(0.2, 100, 0.0, 12.0, 0.1)
        assert fat < thin

    def test_moments_of_normalish_sample(self):
        rng = np.random.default_rng(0)
        r = rng.normal(0, 1, 100_000)
        skew, kurt = moments(r)
        assert abs(skew) < 0.05
        assert abs(kurt - 3.0) < 0.1


class TestTrialVarianceFromGrid:
    def test_converts_annualized_to_per_trade(self):
        grid = [
            {"sharpe_ratio": 1.0, "trades_per_year": 100},  # per-trade 0.1
            {"sharpe_ratio": 2.0, "trades_per_year": 100},  # per-trade 0.2
            {"sharpe_ratio": None, "trades_per_year": 100},  # 無視
            {"sharpe_ratio": 1.0, "trades_per_year": 0},     # 無視
        ]
        var, n = trial_sharpe_variance_from_grid(grid)
        assert n == 2
        assert var == pytest.approx(np.array([0.1, 0.2]).var(ddof=1))

    def test_insufficient_data(self):
        assert trial_sharpe_variance_from_grid([]) == (0.0, 0)
