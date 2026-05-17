"""Tests for screener fundamental filters."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from squant.config.constants import (
    EQUITY_RATIO_MIN,
    LIQUIDITY_MIN_JPY,
    MARKET_CAP_MIN_JPY,
    PBR_MAX,
    PBR_MIN,
    PRICE_MAX,
    PRICE_MIN,
)
from squant.domain.screener import apply_fundamental_filters, exclude_recent_sales

AS_OF = date(2026, 5, 11)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_fund(
    market_cap: float = float(MARKET_CAP_MIN_JPY) * 2,
    liquidity: float = float(LIQUIDITY_MIN_JPY) * 2,
    pbr: float = (float(PBR_MIN) + float(PBR_MAX)) / 2,
    equity_ratio: float = float(EQUITY_RATIO_MIN) + 0.1,
) -> dict:
    return {
        "market_cap_jpy": market_cap,
        "avg_5d_trading_value_jpy": liquidity,
        "pbr": pbr,
        "equity_ratio": equity_ratio,
    }


def _make_ohlcv(ticker: str, price: float = 500.0, n: int = 10) -> pd.DataFrame:
    idx = pd.date_range(end=AS_OF, periods=n, freq="B")
    return pd.DataFrame({ticker: [price] * n}, index=idx)


def _make_fundamentals(tickers: list[str], overrides: dict | None = None) -> pd.DataFrame:
    overrides = overrides or {}
    rows = {t: _make_fund(**(overrides.get(t, {}))) for t in tickers}
    return pd.DataFrame(rows).T


# ── apply_fundamental_filters ─────────────────────────────────────────────────

class TestApplyFundamentalFilters:
    def test_empty_universe_returns_empty(self):
        result = apply_fundamental_filters([], pd.DataFrame(), pd.DataFrame(), AS_OF, set())
        assert result.empty

    def test_single_ticker_all_pass(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert len(result) == 1
        assert result.iloc[0]["ticker"] == "1234.T"

    def test_missing_fundamentals_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = pd.DataFrame()  # no data
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["no_fundamentals"] == 1

    def test_market_cap_below_min_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"], {"1234.T": {"market_cap": MARKET_CAP_MIN_JPY - 1}})
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["market_cap"] == 1

    def test_liquidity_below_min_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"], {"1234.T": {"liquidity": LIQUIDITY_MIN_JPY - 1}})
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["liquidity"] == 1

    def test_pbr_below_min_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"], {"1234.T": {"pbr": float(PBR_MIN) - 0.01}})
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["pbr"] == 1

    def test_pbr_above_max_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"], {"1234.T": {"pbr": float(PBR_MAX) + 0.01}})
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["pbr"] == 1

    def test_equity_ratio_below_min_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"], {"1234.T": {"equity_ratio": float(EQUITY_RATIO_MIN) - 0.01}})
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["equity_ratio"] == 1

    def test_price_below_min_dropped(self):
        ohlcv = _make_ohlcv("1234.T", price=float(PRICE_MIN) - 1)
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["price"] == 1

    def test_price_above_max_dropped(self):
        ohlcv = _make_ohlcv("1234.T", price=float(PRICE_MAX) + 1)
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["price"] == 1

    def test_price_missing_from_ohlcv_dropped(self):
        ohlcv = pd.DataFrame()  # no price data
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.empty
        assert result.attrs["filter_counts"]["price"] == 1

    def test_blackout_dropped(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"])
        blackouts = {("1234.T", AS_OF)}  # same day = blackout
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, blackouts)
        assert result.empty
        assert result.attrs["filter_counts"]["blackout"] == 1

    def test_filter_counts_all_zero_when_pass(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        fc = result.attrs["filter_counts"]
        assert all(v == 0 for v in fc.values())

    def test_multiple_tickers_independent(self):
        ohlcv = pd.concat([_make_ohlcv("A.T"), _make_ohlcv("B.T"), _make_ohlcv("C.T")], axis=1)
        fund = _make_fundamentals(
            ["A.T", "B.T", "C.T"],
            {
                "A.T": {},  # passes
                "B.T": {"market_cap": 0},  # fails market_cap
                "C.T": {},  # passes
            },
        )
        result = apply_fundamental_filters(["A.T", "B.T", "C.T"], ohlcv, fund, AS_OF, set())
        assert len(result) == 2
        assert set(result["ticker"]) == {"A.T", "C.T"}
        assert result.attrs["filter_counts"]["market_cap"] == 1

    def test_result_columns(self):
        ohlcv = _make_ohlcv("1234.T")
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert {"ticker", "close", "market_cap_jpy", "pbr", "equity_ratio"}.issubset(result.columns)

    def test_close_value_is_last_price(self):
        prices = [400.0, 450.0, 500.0]
        idx = pd.date_range(end=AS_OF, periods=3, freq="B")
        ohlcv = pd.DataFrame({"1234.T": prices}, index=idx)
        fund = _make_fundamentals(["1234.T"])
        result = apply_fundamental_filters(["1234.T"], ohlcv, fund, AS_OF, set())
        assert result.iloc[0]["close"] == pytest.approx(500.0)


# ── exclude_recent_sales ──────────────────────────────────────────────────────

class TestExcludeRecentSales:
    def _make_df(self, tickers: list[str]) -> pd.DataFrame:
        return pd.DataFrame({"ticker": tickers, "close": [500.0] * len(tickers)})

    def test_removes_forbidden_ticker(self):
        df = self._make_df(["A.T", "B.T", "C.T"])
        result = exclude_recent_sales(df, {"B.T"})
        assert set(result["ticker"]) == {"A.T", "C.T"}

    def test_keeps_all_when_no_forbidden(self):
        df = self._make_df(["A.T", "B.T"])
        result = exclude_recent_sales(df, set())
        assert len(result) == 2

    def test_empty_df_returns_empty(self):
        result = exclude_recent_sales(pd.DataFrame(columns=["ticker"]), {"A.T"})
        assert result.empty

    def test_removes_all_if_all_forbidden(self):
        df = self._make_df(["A.T", "B.T"])
        result = exclude_recent_sales(df, {"A.T", "B.T"})
        assert result.empty
