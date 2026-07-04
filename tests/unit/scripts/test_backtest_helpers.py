"""Unit tests for backtest helper functions."""

import argparse
import sys
from decimal import Decimal

sys.path.insert(0, "src")

import pandas as pd
import pytest

# backtest.py はパッケージ外スクリプトなので直接インポート
sys.path.insert(0, "scripts")
from backtest import (
    _apply_param_overrides,
    _build_bps_map,
    _find_compatible_cache_path,
    _restore_param_defaults,
    _update_pbr,
)


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


# ── _apply_param_overrides idempotency ─────────────────────────────────────────

def _ns(**kwargs) -> argparse.Namespace:
    base = {"target_profit": None, "atr_mult": None, "rsi_upper": None,
            "rsi_lower": None, "time_stop": None, "price_max": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.fixture
def restore_params():
    """テスト後に全パラメータを pristine に戻す。"""
    yield
    _restore_param_defaults()


class TestApplyParamOverridesIdempotency:
    def test_none_args_keep_defaults(self, restore_params):
        from squant.config import constants
        before = (constants.TARGET_PROFIT_RATE, constants.ATR_TRAILING_MULTIPLIER,
                  constants.RSI_BUY_UPPER, constants.RSI_BUY_LOWER,
                  constants.TIME_STOP_TRADING_DAYS)
        _apply_param_overrides(_ns())
        after = (constants.TARGET_PROFIT_RATE, constants.ATR_TRAILING_MULTIPLIER,
                 constants.RSI_BUY_UPPER, constants.RSI_BUY_LOWER,
                 constants.TIME_STOP_TRADING_DAYS)
        assert before == after

    def test_override_then_none_restores_pristine(self, restore_params):
        """前セルの上書きが None 指定のセルに漏れない（in-process グリッドの肝）"""
        from squant.config import constants
        from squant.domain import position_manager, screener, signal_engine

        pristine = (constants.TARGET_PROFIT_RATE, constants.ATR_TRAILING_MULTIPLIER,
                    constants.RSI_BUY_UPPER, constants.RSI_BUY_LOWER,
                    constants.TIME_STOP_TRADING_DAYS, constants.PRICE_MAX)

        # セルA: 全部上書き
        _apply_param_overrides(_ns(
            target_profit=0.03, atr_mult=2.0, rsi_upper=50, rsi_lower=40, time_stop=3,
            price_max=2000,
        ))
        assert Decimal("2.0") == constants.ATR_TRAILING_MULTIPLIER
        assert Decimal("2.0") == position_manager.ATR_TRAILING_MULTIPLIER
        assert signal_engine.RSI_BUY_UPPER == 50.0
        assert position_manager.TIME_STOP_TRADING_DAYS == 3
        assert Decimal("2000") == constants.PRICE_MAX
        assert Decimal("2000") == screener.PRICE_MAX

        # セルB: 全部 None → pristine に戻ること（前セル値の残留 NG）
        _apply_param_overrides(_ns())
        restored = (constants.TARGET_PROFIT_RATE, constants.ATR_TRAILING_MULTIPLIER,
                    constants.RSI_BUY_UPPER, constants.RSI_BUY_LOWER,
                    constants.TIME_STOP_TRADING_DAYS, constants.PRICE_MAX)
        assert restored == pristine
        assert pristine[1] == position_manager.ATR_TRAILING_MULTIPLIER
        assert pristine[2] == signal_engine.RSI_BUY_UPPER
        assert pristine[3] == signal_engine.RSI_BUY_LOWER
        assert pristine[4] == position_manager.TIME_STOP_TRADING_DAYS
        assert pristine[5] == screener.PRICE_MAX

    def test_take_profit_patch_does_not_stack(self, restore_params):
        """繰り返し呼んでも closure が多重ラップされない"""
        from squant.domain import position_manager, quantity_calculator

        for rate in (0.02, 0.05, 0.03):
            _apply_param_overrides(_ns(target_profit=rate))
        # 最後の 0.03 が効いている（多重ラップなら過去の rate が混ざる）
        assert quantity_calculator.compute_take_profit_price(
            Decimal("100")) == Decimal("100") * Decimal("1.03")
        assert position_manager.compute_take_profit_price(
            Decimal("100")) == Decimal("100") * Decimal("1.03")

        # None に戻すと pristine の関数オブジェクトに戻る（デフォルトは TP なし = None）
        _apply_param_overrides(_ns())
        assert quantity_calculator.compute_take_profit_price \
            is position_manager.compute_take_profit_price
        assert quantity_calculator.compute_take_profit_price(Decimal("100")) is None


# ── _find_compatible_cache_path ────────────────────────────────────────────────

class TestFindCompatibleCachePath:
    def _touch(self, d, name):
        p = d / name
        p.write_bytes(b"")
        return p

    def test_picks_tightest_cover(self, tmp_path):
        from datetime import date
        wide = self._touch(tmp_path, "data_2021-01-01_2025-12-30.pkl")  # noqa: F841
        tight = self._touch(tmp_path, "data_2023-12-01_2024-12-30.pkl")
        self._touch(tmp_path, "data_2024-06-01_2024-12-30.pkl")  # カバー外（開始遅い）
        got = _find_compatible_cache_path(date(2024, 1, 4), date(2024, 12, 30), tmp_path)
        assert got == tight

    def test_none_when_no_cover(self, tmp_path):
        from datetime import date
        self._touch(tmp_path, "data_2024-06-01_2024-12-30.pkl")
        assert _find_compatible_cache_path(date(2024, 1, 4), date(2024, 12, 30), tmp_path) is None

    def test_ignores_malformed_names(self, tmp_path):
        from datetime import date
        self._touch(tmp_path, "data_garbage.pkl")
        self._touch(tmp_path, "data_2024-13-99_2024-12-30.pkl")
        ok = self._touch(tmp_path, "data_2023-01-01_2025-12-30.pkl")
        got = _find_compatible_cache_path(date(2024, 1, 4), date(2024, 12, 30), tmp_path)
        assert got == ok
