"""Unit tests for backtest helper functions."""

import sys
from decimal import Decimal

sys.path.insert(0, "src")

import pandas as pd
import pytest

# backtest.py はパッケージ外スクリプトなので直接インポート
sys.path.insert(0, "scripts")
from backtest import _build_bps_map, _update_pbr


def _make_fund(tickers: list[str], pbr_values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"pbr": pbr_values, "equity_ratio": [0.5] * len(tickers)},
        index=tickers,
    )


def _make_adj_close(ticker_prices: dict[str, list[float]], dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(ticker_prices, index=pd.to_datetime(dates))


# ── _build_bps_map ─────────────────────────────────────────────────────────────

class TestBuildBpsMap:
    def test_basic(self):
        """BPS = latest_close / pbr で正しく計算される"""
        fund = _make_fund(["A"], [2.0])
        adj = _make_adj_close({"A": [1000.0]}, ["2024-01-05"])
        result = _build_bps_map(fund, adj)
        assert result == {"A": pytest.approx(500.0)}  # 1000 / 2.0

    def test_multiple_dates_uses_latest(self):
        """複数の日付がある場合は最新終値を使う"""
        fund = _make_fund(["A"], [2.0])
        adj = _make_adj_close({"A": [900.0, 1000.0]}, ["2024-01-04", "2024-01-05"])
        result = _build_bps_map(fund, adj)
        assert result == {"A": pytest.approx(500.0)}  # 1000 / 2.0

    def test_zero_pbr_excluded(self):
        """pbr=0 の銘柄はスキップされる"""
        fund = _make_fund(["A", "B"], [0.0, 1.5])
        adj = _make_adj_close({"A": [1000.0], "B": [600.0]}, ["2024-01-05"])
        result = _build_bps_map(fund, adj)
        assert "A" not in result
        assert "B" in result

    def test_missing_ticker_in_adj_excluded(self):
        """adj_close にない銘柄はスキップされる"""
        fund = _make_fund(["A", "B"], [2.0, 1.5])
        adj = _make_adj_close({"A": [1000.0]}, ["2024-01-05"])
        result = _build_bps_map(fund, adj)
        assert "B" not in result

    def test_negative_pbr_excluded(self):
        """pbr<0 の銘柄はスキップされる"""
        fund = _make_fund(["A"], [-1.0])
        adj = _make_adj_close({"A": [500.0]}, ["2024-01-05"])
        result = _build_bps_map(fund, adj)
        assert "A" not in result


# ── _update_pbr ────────────────────────────────────────────────────────────────

class TestUpdatePbr:
    def test_pbr_recalculated(self):
        """当日終値 / BPS でPBRが更新される"""
        fund = _make_fund(["A"], [2.0])
        bps_map = {"A": 500.0}  # BPS=500
        adj_slice = _make_adj_close({"A": [1500.0]}, ["2024-06-01"])

        result = _update_pbr(fund, bps_map, adj_slice)
        assert result.at["A", "pbr"] == pytest.approx(3.0)  # 1500 / 500

    def test_original_not_mutated(self):
        """元の fund_base は変更されない"""
        fund = _make_fund(["A"], [2.0])
        bps_map = {"A": 500.0}
        adj_slice = _make_adj_close({"A": [1500.0]}, ["2024-06-01"])

        _update_pbr(fund, bps_map, adj_slice)
        assert fund.at["A", "pbr"] == pytest.approx(2.0)

    def test_missing_in_bps_map_unchanged(self):
        """bps_map にない銘柄はPBRが変わらない"""
        fund = _make_fund(["A", "B"], [2.0, 1.5])
        bps_map = {"A": 500.0}
        adj_slice = _make_adj_close({"A": [1500.0], "B": [900.0]}, ["2024-06-01"])

        result = _update_pbr(fund, bps_map, adj_slice)
        assert result.at["B", "pbr"] == pytest.approx(1.5)

    def test_missing_in_adj_slice_unchanged(self):
        """adj_slice にない銘柄はPBRが変わらない"""
        fund = _make_fund(["A", "B"], [2.0, 1.5])
        bps_map = {"A": 500.0, "B": 400.0}
        adj_slice = _make_adj_close({"A": [1500.0]}, ["2024-06-01"])

        result = _update_pbr(fund, bps_map, adj_slice)
        assert result.at["B", "pbr"] == pytest.approx(1.5)

    def test_zero_bps_skipped(self):
        """bps=0 はゼロ除算を避けるためスキップされる"""
        fund = _make_fund(["A"], [2.0])
        bps_map = {"A": 0.0}
        adj_slice = _make_adj_close({"A": [1000.0]}, ["2024-06-01"])

        result = _update_pbr(fund, bps_map, adj_slice)
        assert result.at["A", "pbr"] == pytest.approx(2.0)
