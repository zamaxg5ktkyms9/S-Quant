"""Unit tests for F1 point-in-time backtest support (delist exit, as-of data)."""

import sys
from datetime import date
from decimal import Decimal

import pandas as pd

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from backtest import BacktestState, _build_bps_map_asof, _process_exit
from build_pit_cache import build_frames, fundamentals_asof, load_snapshots

from squant.domain.models import Position


def _pos(ticker="1000.T", entry=1000.0):
    return Position(
        ticker=ticker,
        shares=100,
        entry_price=Decimal(str(entry)),
        intended_entry_price=Decimal(str(entry)),
        entry_date=date(2024, 6, 3),
        stop_loss_price=Decimal(str(entry * 0.975)),
        trailing_stop_price=Decimal(str(entry * 0.975)),
        highest_price_since_entry=Decimal(str(entry)),
        time_stop_date=date(2024, 6, 10),
    )


def _cache_df(last_day: str, close=900.0):
    idx = pd.to_datetime(["2024-06-03", "2024-06-04", last_day])
    return pd.DataFrame({
        "AdjO": [1000.0, 990.0, close],
        "AdjH": [1010.0, 1000.0, close + 5],
        "AdjL": [990.0, 980.0, close - 5],
        "AdjC": [1000.0, 990.0, close],
    }, index=idx)


class TestDelistExit:
    def test_stale_data_forces_delist_exit_in_pit_mode(self):
        state = BacktestState(cash=Decimal("0"), initial_capital=Decimal("600000"))
        df = _cache_df("2024-06-05", close=900.0)
        result = _process_exit(state, _pos(), date(2024, 7, 1),
                               {"1000.T": df}, delist_after_days=15)
        assert result is None
        assert len(state.trades) == 1
        t = state.trades[0]
        assert t.reason == "DELISTED"
        assert t.exit_price == Decimal("900")
        assert t.exit_date == date(2024, 6, 5)   # 最終取引日
        assert state.cash == Decimal("90000")    # 900 × 100株

    def test_short_gap_does_not_delist(self):
        """数日の欠損（一時停止等）では手仕舞いしない"""
        state = BacktestState(cash=Decimal("0"), initial_capital=Decimal("600000"))
        df = _cache_df("2024-06-25")
        result = _process_exit(state, _pos(), date(2024, 7, 1),
                               {"1000.T": df}, delist_after_days=15)
        assert result is not None
        assert state.trades == []

    def test_default_mode_keeps_position(self):
        """delist_after_days=None（従来モード）は挙動不変 = 継続扱い"""
        state = BacktestState(cash=Decimal("0"), initial_capital=Decimal("600000"))
        df = _cache_df("2024-06-05")
        result = _process_exit(state, _pos(), date(2024, 7, 1), {"1000.T": df})
        assert result is not None
        assert state.trades == []


class TestBpsMapAsof:
    def test_uses_asof_close_not_future(self):
        idx = pd.to_datetime(["2024-06-03", "2024-12-30"])
        adj = pd.DataFrame({"1000.T": [1000.0, 2000.0]}, index=idx)
        fund = pd.DataFrame({"pbr": [2.0]}, index=pd.Index(["1000.T"], name="ticker"))
        bps = _build_bps_map_asof(fund, adj, date(2024, 6, 30))
        assert bps["1000.T"] == 500.0  # 1000/2.0（12月の2000ではない）

    def test_skips_missing_and_nonpositive(self):
        idx = pd.to_datetime(["2024-06-03"])
        adj = pd.DataFrame({"1000.T": [1000.0]}, index=idx)
        fund = pd.DataFrame(
            {"pbr": [0.0, 1.0]}, index=pd.Index(["1000.T", "9999.T"], name="ticker"))
        assert _build_bps_map_asof(fund, adj, date(2024, 6, 30)) == {}


class TestFundamentalsAsof:
    def test_picks_latest_disclosure_before_asof(self):
        fins = {"1000.T": [
            {"DiscDate": "2024-02-01", "EqAR": 0.40, "BPS": 500.0, "Eq": 0, "ShOutFY": 1_000_000},
            {"DiscDate": "2024-05-01", "EqAR": 0.45, "BPS": 550.0, "Eq": 0, "ShOutFY": 1_000_000},
            {"DiscDate": "2024-08-01", "EqAR": 0.50, "BPS": 600.0, "Eq": 0, "ShOutFY": 1_000_000},
        ]}
        idx = pd.to_datetime(["2024-06-28", "2024-07-01"])
        adj = pd.DataFrame({"1000.T": [1100.0, 1200.0]}, index=idx)
        full_cache = {"1000.T": pd.DataFrame(
            {"Va": [200e6, 300e6]}, index=idx)}
        df = fundamentals_asof(fins, ["1000.T"], date(2024, 7, 1), adj, full_cache)
        r = df.loc["1000.T"]
        # as-of 7/1: 5/1 開示（8/1 は未来なので使わない）、終値 1200
        assert r["equity_ratio"] == 0.45
        assert r["pbr"] == 1200.0 / 550.0
        assert r["market_cap_jpy"] == 1_000_000 * 1200.0
        assert r["avg_5d_trading_value_jpy"] == 250e6

    def test_derives_bps_from_equity_when_missing(self):
        fins = {"1000.T": [
            {"DiscDate": "2024-05-01", "EqAR": 0.4, "BPS": "", "Eq": 5e8, "ShOutFY": 1_000_000},
        ]}
        idx = pd.to_datetime(["2024-06-28"])
        adj = pd.DataFrame({"1000.T": [1000.0]}, index=idx)
        df = fundamentals_asof(fins, ["1000.T"], date(2024, 7, 1), adj, {})
        assert df.loc["1000.T", "pbr"] == 1000.0 / 500.0  # BPS = 5e8/1e6

    def test_no_disclosure_yields_zeroes(self):
        idx = pd.to_datetime(["2024-06-28"])
        adj = pd.DataFrame({"1000.T": [1000.0]}, index=idx)
        df = fundamentals_asof({}, ["1000.T"], date(2024, 7, 1), adj, {})
        assert df.loc["1000.T", "pbr"] == 0.0
        assert df.loc["1000.T", "equity_ratio"] == 0.0


class TestBuildFrames:
    def test_frames_shapes_and_ticker_mapping(self):
        bars = {
            date(2024, 6, 3): [
                {"Date": "2024-06-03", "Code": "10000", "AdjC": 100.0, "AdjVo": 1000,
                 "AdjO": 99.0, "AdjH": 101.0, "AdjL": 98.0, "Va": 1e8},
                {"Date": "2024-06-03", "Code": "99999", "AdjC": 5.0},  # union 外
            ],
            date(2024, 6, 4): [
                {"Date": "2024-06-04", "Code": "10000", "AdjC": 102.0, "AdjVo": 1100,
                 "AdjO": 100.0, "AdjH": 103.0, "AdjL": 99.0, "Va": 1.1e8},
            ],
        }
        adj, vol, cache = build_frames(bars, {"10000"})
        assert list(adj.columns) == ["1000.T"]
        assert adj.shape == (2, 1)
        assert cache["1000.T"].loc["2024-06-04", "AdjC"] == 102.0


class TestLoadSnapshots:
    def test_roundtrip(self, tmp_path):
        (tmp_path / "2024Q3.csv").write_text(
            "# point-in-time universe as of 2024-07-01 (generated)\n"
            "ticker\n1000.T\n2000.T\n"
        )
        snaps = load_snapshots(tmp_path)
        assert snaps == {date(2024, 7, 1): ["1000.T", "2000.T"]}
