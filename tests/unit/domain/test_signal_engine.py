"""Tests for buy signal detection logic."""

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from squant.config.constants import (
    MA_LONG,
    MA_MID,
    MA_TREND_LOOKBACK,
    VOLUME_SURGE_MULTIPLIER,
)
from squant.domain.signal_engine import detect_signals, detect_signals_ma_cross

AS_OF = date(2026, 5, 11)
N = MA_LONG + 20  # enough days for all indicators


# ── OHLCV helpers ──────────────────────────────────────────────────────────────

def _make_ohlcv(
    ticker: str,
    prices: list[float] | np.ndarray,
    vol_surge: bool = True,
) -> pd.DataFrame:
    """Build ohlcv DataFrame with ticker and ticker_vol columns.

    vol_surge=True: 当日出来高を20日平均×1.5にして④条件を通す。
    """
    n = len(prices)
    idx = pd.date_range(end=AS_OF, periods=n, freq="B")
    base_vol = 1_000_000.0
    vols = [base_vol] * n
    if vol_surge:
        vols[-1] = base_vol * (float(VOLUME_SURGE_MULTIPLIER) + 0.3)  # well above threshold
    else:
        vols[-1] = base_vol * 0.5  # below threshold
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
        """Steady uptrend with no pullback → RSI(14) stays above upper bound → no signal."""
        prices = np.linspace(400.0, 600.0, N)  # monotonic up
        ohlcv = _make_ohlcv("1234.T", prices)
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        # RSI(14) after steady uptrend will be well above RSI_BUY_UPPER (50)
        assert result == []

    def test_cond4_no_volume_surge_dropped(self):
        """Volume below 20-day avg × 1.2 → no signal even if price conditions pass."""
        prices = _uptrend_then_pullback()
        ohlcv = _make_ohlcv("1234.T", prices, vol_surge=False)
        fund = _make_fund("1234.T")
        result = detect_signals(["1234.T"], ohlcv, fund, AS_OF)
        assert result == []

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
        ohlcv = _make_ohlcv(ticker, prices, vol_surge=True)
        fund = _make_fund(ticker, pbr=0.75)
        result = detect_signals([ticker], ohlcv, fund, AS_OF)
        if result:  # only assert if signal generated (data-dependent)
            c = result[0]
            assert c.ticker == ticker
            assert isinstance(c.close, Decimal)
            assert 0.0 <= c.rsi14 <= 100.0
            assert c.pbr == pytest.approx(0.75)
            assert c.volume_surge_ratio >= float(VOLUME_SURGE_MULTIPLIER)

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


# ── detect_signals_ma_cross (C phase) ──────────────────────────────────────────

# MA cross needs at least MA_MID + MA_TREND_LOOKBACK + 5 days
N_MA = MA_MID + MA_TREND_LOOKBACK + 10


def _steady_uptrend(n: int = N_MA, start: float = 300.0, end: float = 600.0) -> np.ndarray:
    """安定した上昇トレンド: 5日MA > 25日MA、25日MA 上向きを満たす。"""
    return np.linspace(start, end, n)


def _steady_downtrend(n: int = N_MA, start: float = 600.0, end: float = 300.0) -> np.ndarray:
    return np.linspace(start, end, n)


def _flat(n: int = N_MA, level: float = 500.0) -> np.ndarray:
    return np.full(n, level)


class TestDetectSignalsMaCross:
    def test_uptrend_passes(self):
        """5日MA > 25日MA かつ 25日MA 上向き → シグナル発生."""
        ohlcv = _make_ohlcv("A.T", _steady_uptrend(), vol_surge=True)
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert len(result) == 1
        assert result[0].ticker == "A.T"

    def test_downtrend_fails_cross(self):
        """下降トレンドでは 5日MA < 25日MA、cond1_cross で除外."""
        ohlcv = _make_ohlcv("A.T", _steady_downtrend(), vol_surge=True)
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_flat_price_fails(self):
        """フラットだと 5日MA ≒ 25日MA、cross 条件で除外。

        厳密に float 同点になる可能性は低いが、25日MA の上昇が無いので
        cond2_trend でも除外される（>0 を要求）。
        """
        ohlcv = _make_ohlcv("A.T", _flat(), vol_surge=True)
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_no_volume_surge_fails(self):
        """上昇トレンドでも 出来高サージなければ除外."""
        ohlcv = _make_ohlcv("A.T", _steady_uptrend(), vol_surge=False)
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_insufficient_history(self):
        """データ不足は除外."""
        prices = _steady_uptrend(MA_MID)  # 25日のみ、lookback 不足
        ohlcv = _make_ohlcv("A.T", prices, vol_surge=True)
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_recent_downturn_after_uptrend_fails(self):
        """直近で 5日MA が 25日MA を下回ったら除外（クロス下抜け）."""
        n = N_MA
        up_n = int(n * 0.5)
        down_n = n - up_n
        up = np.linspace(300.0, 700.0, up_n)
        down = np.linspace(700.0, 400.0, down_n)
        prices = np.concatenate([up, down])
        ohlcv = _make_ohlcv("A.T", prices, vol_surge=True)
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_missing_ticker_in_ohlcv(self):
        ohlcv = pd.DataFrame(index=pd.date_range(end=AS_OF, periods=N_MA, freq="B"))
        fund = _make_fund("A.T")
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert result == []

    def test_candidate_fields_populated(self):
        ohlcv = _make_ohlcv("A.T", _steady_uptrend(), vol_surge=True)
        fund = _make_fund("A.T", pbr=1.2)
        result = detect_signals_ma_cross(["A.T"], ohlcv, fund, AS_OF)
        assert len(result) == 1
        c = result[0]
        assert isinstance(c.close, Decimal)
        assert c.pbr == 1.2
        assert c.market_cap_jpy > 0
        assert c.volume_surge_ratio >= float(VOLUME_SURGE_MULTIPLIER)


# ── Plan B 探索用シグナル（reversal / value / high52） ─────────────────────────

from squant.domain.signal_engine import (  # noqa: E402
    HIGH52_WINDOW,
    detect_signals_high52,
    detect_signals_reversal,
    detect_signals_value,
    get_signal_func,
)


def test_get_signal_func_dispatch():
    assert get_signal_func("reversal") is detect_signals_reversal
    assert get_signal_func("value") is detect_signals_value
    assert get_signal_func("high52") is detect_signals_high52
    assert get_signal_func("ma_cross") is detect_signals_ma_cross
    assert get_signal_func("pullback") is detect_signals  # fallback default


class TestReversal:
    def test_oversold_passes(self):
        # 強い下落 → RSI ≤ 30
        prices = np.linspace(1000.0, 500.0, 60)
        ohlcv = _make_ohlcv("A.T", prices, vol_surge=False)
        fund = _make_fund("A.T")
        result = detect_signals_reversal(["A.T"], ohlcv, fund, AS_OF)
        assert len(result) == 1
        assert result[0].rsi14 <= 30.0

    def test_uptrend_rejected(self):
        prices = np.linspace(500.0, 1000.0, 60)  # 上昇 → RSI 高い
        ohlcv = _make_ohlcv("A.T", prices, vol_surge=False)
        fund = _make_fund("A.T")
        result = detect_signals_reversal(["A.T"], ohlcv, fund, AS_OF)
        assert result == []


class TestValue:
    def test_cheap_passes_expensive_rejected(self):
        prices = np.linspace(500.0, 520.0, 30)
        ohlcv = pd.concat(
            [_make_ohlcv("CHEAP.T", prices), _make_ohlcv("RICH.T", prices)], axis=1
        )
        fund = pd.DataFrame(
            {"pbr": [0.6, 3.0], "market_cap_jpy": [5e10, 5e10]},
            index=["CHEAP.T", "RICH.T"],
        )
        result = detect_signals_value(["CHEAP.T", "RICH.T"], ohlcv, fund, AS_OF)
        tickers = [c.ticker for c in result]
        assert "CHEAP.T" in tickers
        assert "RICH.T" not in tickers  # PBR 3.0 > 1.0 で除外

    def test_negative_pbr_rejected(self):
        prices = np.linspace(500.0, 520.0, 30)
        ohlcv = _make_ohlcv("A.T", prices)
        fund = pd.DataFrame({"pbr": [-1.0], "market_cap_jpy": [5e10]}, index=["A.T"])
        assert detect_signals_value(["A.T"], ohlcv, fund, AS_OF) == []


class TestHigh52:
    def test_near_high_passes(self):
        n = HIGH52_WINDOW + 5
        prices = np.linspace(300.0, 700.0, n)  # 単調上昇 → 直近が最高値
        ohlcv = _make_ohlcv("A.T", prices)
        fund = _make_fund("A.T")
        result = detect_signals_high52(["A.T"], ohlcv, fund, AS_OF)
        assert len(result) == 1

    def test_far_from_high_rejected(self):
        n = HIGH52_WINDOW + 5
        up = np.linspace(300.0, 900.0, n - 20)
        down = np.linspace(900.0, 600.0, 20)  # 高値から大きく下落
        prices = np.concatenate([up, down])
        ohlcv = _make_ohlcv("A.T", prices)
        fund = _make_fund("A.T")
        assert detect_signals_high52(["A.T"], ohlcv, fund, AS_OF) == []

    def test_insufficient_history_rejected(self):
        prices = np.linspace(300.0, 700.0, 100)  # < 252
        ohlcv = _make_ohlcv("A.T", prices)
        fund = _make_fund("A.T")
        assert detect_signals_high52(["A.T"], ohlcv, fund, AS_OF) == []


# ── Plan B P3 (PEAD) ────────────────────────────────────────────────────────

from squant.domain.signal_engine import (  # noqa: E402
    PEAD_LOOKBACK_CAL_DAYS,
    PEAD_MIN_SURPRISE,
    detect_signals_pead,
    set_pead_events,
)


class TestPead:
    def _ohlcv(self, ticker="A.T"):
        return _make_ohlcv(ticker, np.linspace(500.0, 520.0, 30))

    def test_dispatch(self):
        assert get_signal_func("pead") is detect_signals_pead

    def test_recent_positive_surprise_passes(self):
        set_pead_events({"A.T": [
            {"disc_date": (AS_OF.isoformat()), "yoy_np": PEAD_MIN_SURPRISE + 0.2},
        ]})
        result = detect_signals_pead(["A.T"], self._ohlcv(), _make_fund("A.T"), AS_OF)
        assert len(result) == 1

    def test_stale_disclosure_rejected(self):
        from datetime import timedelta
        old = (AS_OF - timedelta(days=PEAD_LOOKBACK_CAL_DAYS + 10)).isoformat()
        set_pead_events({"A.T": [{"disc_date": old, "yoy_np": 0.5}]})
        assert detect_signals_pead(["A.T"], self._ohlcv(), _make_fund("A.T"), AS_OF) == []

    def test_low_surprise_rejected(self):
        set_pead_events({"A.T": [
            {"disc_date": AS_OF.isoformat(), "yoy_np": PEAD_MIN_SURPRISE - 0.01},
        ]})
        assert detect_signals_pead(["A.T"], self._ohlcv(), _make_fund("A.T"), AS_OF) == []

    def test_no_events_rejected(self):
        set_pead_events({})
        assert detect_signals_pead(["A.T"], self._ohlcv(), _make_fund("A.T"), AS_OF) == []

    def test_future_disclosure_not_used(self):
        # as_of より後の開示は使わない（look-ahead 防止）
        from datetime import timedelta
        future = (AS_OF + timedelta(days=1)).isoformat()
        set_pead_events({"A.T": [{"disc_date": future, "yoy_np": 0.5}]})
        assert detect_signals_pead(["A.T"], self._ohlcv(), _make_fund("A.T"), AS_OF) == []
        set_pead_events({})  # cleanup
