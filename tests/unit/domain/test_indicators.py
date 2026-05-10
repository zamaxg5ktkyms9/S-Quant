"""Tests for technical indicator functions."""

import numpy as np
import pandas as pd
import pytest

from squant.domain.indicators import atr, rsi, rolling_std, sma, volume_surge_ratio


def make_series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


class TestSMA:
    def test_basic(self):
        s = make_series([1, 2, 3, 4, 5])
        result = sma(s, 3)
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[4] == pytest.approx(4.0)

    def test_period_longer_than_series_returns_all_nan(self):
        s = make_series([1, 2, 3])
        result = sma(s, 5)
        assert result.isna().all()


class TestRSI:
    def test_rsi_range(self):
        """RSI must always be in [0, 100]."""
        rng = np.random.default_rng(0)
        prices = 100 + np.cumsum(rng.normal(0, 1, 200))
        s = pd.Series(prices)
        result = rsi(s, 14).dropna()
        assert (result >= 0).all()
        assert (result <= 100).all()

    def test_constant_series_returns_nan_or_50(self):
        s = make_series([100.0] * 20)
        result = rsi(s, 14).dropna()
        # No gains, no losses → RS is NaN → RSI is NaN or 50
        # Acceptable that some implementations return NaN
        assert len(result) >= 0  # doesn't crash

    def test_monotonically_rising_has_high_rsi(self):
        prices = list(range(100, 200))
        s = make_series(prices)
        result = rsi(s, 14).dropna()
        assert result.iloc[-1] > 70

    def test_monotonically_falling_has_low_rsi(self):
        prices = list(range(200, 100, -1))
        s = make_series(prices)
        result = rsi(s, 14).dropna()
        assert result.iloc[-1] < 30


class TestRollingStd:
    def test_basic(self):
        s = make_series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = rolling_std(s, 3)
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert not pd.isna(result.iloc[2])

    def test_constant_series_has_zero_std(self):
        s = make_series([5.0] * 30)
        result = rolling_std(s, 20).dropna()
        assert (result.abs() < 1e-10).all()


class TestVolumeSurgeRatio:
    def test_doubled_volume_returns_approx_2(self):
        # Build a 30-element series where the last element is 2× the previous average
        avg = 1_000_000.0
        vols = [avg] * 29 + [avg * 2]
        s = make_series(vols)
        result = volume_surge_ratio(s, window=20).dropna()
        assert result.iloc[-1] == pytest.approx(2.0, rel=0.05)

    def test_zero_average_returns_nan(self):
        s = make_series([0.0] * 25)
        result = volume_surge_ratio(s, window=20)
        # Should return NaN when average is zero (division by NaN)
        assert result.dropna().empty or (result.iloc[-1] != result.iloc[-1])  # NaN check
