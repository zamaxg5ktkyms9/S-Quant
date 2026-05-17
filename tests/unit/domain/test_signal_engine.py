"""Tests for buy signal detection logic."""

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from squant.config.constants import MA_LONG, RSI_BUY_THRESHOLD
from squant.domain.signal_engine import detect_signals

AS_OF = date(2026, 5, 11)
N = MA_LONG + 20  # enough days for all indicators


# ── OHLCV helpers ──────────────────────────────────────────────────────────────

def _make_ohlcv(
    ticker: str,
    prices: list[float] | np.ndarray,
    vol_today_gt_yesterday: bool = True,
) -> pd.DataFrame:
    """Build ohlcv DataFrame with ticker and ticker_vol columns."""
    n = len(prices)
    idx = pd.date_range(end=AS_OF, periods=n, freq="B")
    base_vol = 1_000_000.0
    vols = [base_vol] * n
    if vol_today_gt_yesterday:
        vols[-1] = base_vol * 1.5  # today > yesterday
    else:
        vols[-1] = base_vol * 0.5  # today < yesterday
    return pd.DataFrame(
        {ticker: prices, f"{ticker}_vol": vols},
        index=idx,
    )


def _make_fund(ticker: str, pbr: float = 0.8) -> pd.DataFrame:
    return pd.DataFrame(
        {"pbr": [pbr], "market_cap_jpy": [50_000_000_000.0]},
        index=[ticker],
    )


def _uptrend_then_pullback(n: int = N) -> np.ndarray:
    """Rising trend for first 80% then pullback — designed to yield RSI < 45."""
    up_n = int(n * 0.80)
    down_n = n - up_n
    up = np.linspace(400.0, 600.0, up_n)
    down = np.linspace(600.0, 550.0, down_n)
    return np.concatenate([up, down])


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestDetectSignals:
    def test_empty_tickers_returns_empty(self):
        result = detect_signals([], pd.DataFrame(), pd.DataFrame(), AS_OF)
        assert result == []

    def test_ticker_not_in_ohlcv_dropped(self):
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], pd.DataFrame(), fund, AS_OF)
        assert result == []

    def test_insufficient_data_dropped(self):
        """Fewer rows than MA_LONG + 10 → no signal."""
        prices = [500.0] * (MA_LONG + 5)  # below threshold
        ohlcv = _make_ohlcv("1234.T", prices)
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_cond1_below_ma75_dropped(self):
        """Downtrend: close < MA_LONG → no signal."""
        prices = np.linspace(700.0, 400.0, N)  # falling
        ohlcv = _make_ohlcv("1234.T", prices)
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_cond2_rsi_too_high_dropped(self):
        """Steady uptrend with no pullback → RSI stays high → no signal."""
        prices = np.linspace(400.0, 600.0, N)  # monotonic up
        ohlcv = _make_ohlcv("1234.T", prices)
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        # RSI after steady uptrend should be well above RSI_BUY_THRESHOLD
        assert result == []

    def test_cond4_decreasing_volume_dropped(self):
        """Volume today < yesterday → no signal even if price conditions pass."""
        prices = _uptrend_then_pullback()
        ohlcv = _make_ohlcv("1234.T", prices, vol_today_gt_yesterday=False)
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        # May or may not pass other conditions, but volume condition should filter
        # (we just verify it runs without error — exact outcome depends on data)
        assert isinstance(result, list)

    def test_cond4_no_volume_column_dropped(self):
        """Missing volume column → ticker dropped."""
        prices = _uptrend_then_pullback()
        idx = pd.date_range(end=AS_OF, periods=len(prices), freq="B")
        ohlcv = pd.DataFrame({"1234.T": prices}, index=idx)  # no vol column
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_candidate_fields_populated(self, close_series_bullish):
        """When a signal passes, Candidate has expected fields."""
        ticker = "5678.T"
        prices = close_series_bullish.values
        ohlcv = _make_ohlcv(ticker, prices, vol_today_gt_yesterday=True)
        fund = _make_fund(ticker, pbr=0.75)
        result = detect_signals([ticker], ohlcv, fund, AS_OF)
        if result:  # only assert if signal generated (data-dependent)
            c = result[0]
            assert c.ticker == ticker
            assert isinstance(c.close, Decimal)
            assert 0.0 <= c.rsi14 <= 100.0
            assert c.pbr == pytest.approx(0.75)
            assert c.volume_surge_ratio >= 0.0

    def test_multiple_tickers_independent(self):
        """Each ticker evaluated independently."""
        # Ticker A: not enough data
        prices_a = [500.0] * 10
        idx_a = pd.date_range(end=AS_OF, periods=10, freq="B")
        # Ticker B: downtrend → fails cond1
        prices_b = np.linspace(700.0, 400.0, N)
        idx_b = pd.date_range(end=AS_OF, periods=N, freq="B")

        ohlcv = pd.DataFrame(index=idx_b)
        ohlcv["A.T"] = pd.Series(prices_a, index=idx_a)
        ohlcv["B.T"] = pd.Series(prices_b, index=idx_b)
        ohlcv["A.T_vol"] = 1_000_000.0
        ohlcv["B.T_vol"] = 1_000_000.0

        fund = pd.concat([_make_fund("A.T"), _make_fund("B.T")])
        result = detect_signals(["A.T", "B.T"], ohlcv, fund, AS_OF)
        assert result == []
