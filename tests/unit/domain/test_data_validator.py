"""Tests for DataValidator."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from squant.infrastructure.data_validator import DataValidator, Severity


def make_clean_series(n: int = 100, end_date: date = date(2026, 5, 11)) -> pd.Series:
    idx = pd.bdate_range(end=str(end_date), periods=n)
    prices = 500 + np.cumsum(np.random.default_rng(0).normal(0, 3, n))
    return pd.Series(prices, index=idx)


class TestValidateCloseSeries:
    def setup_method(self):
        self.v = DataValidator()

    def test_valid_series_returns_ok(self):
        s = make_clean_series(100)
        result = self.v.validate_close_series("T.T", s, date(2026, 5, 11))
        assert result.ok

    def test_empty_series_returns_skip(self):
        result = self.v.validate_close_series("T.T", pd.Series([], dtype=float), date(2026, 5, 11))
        assert result.severity == Severity.SKIP_TICKER

    def test_insufficient_history_returns_skip(self):
        s = make_clean_series(50)
        result = self.v.validate_close_series("T.T", s, date(2026, 5, 11))
        assert result.severity == Severity.SKIP_TICKER
        assert any("insufficient" in i for i in result.issues)

    def test_stale_data_has_issue(self):
        s = make_clean_series(100, end_date=date(2026, 5, 8))   # Friday, not Monday
        result = self.v.validate_close_series("T.T", s, date(2026, 5, 11))  # expect Monday
        assert result.severity == Severity.SKIP_TICKER
        assert any("stale" in i for i in result.issues)

    def test_price_anomaly_30pct_returns_skip(self):
        s = make_clean_series(100)
        # Inject 50% spike on last bar
        s.iloc[-1] = s.iloc[-2] * 1.50
        result = self.v.validate_close_series("T.T", s, date(2026, 5, 11))
        assert result.severity == Severity.SKIP_TICKER
        assert any("price move" in i for i in result.issues)


class TestDetectPriceAnomaly:
    def setup_method(self):
        self.v = DataValidator()

    def test_normal_move_no_issues(self):
        s = pd.Series([500.0, 505.0])
        issues = self.v.detect_price_anomaly(s)
        assert issues == []

    def test_35pct_move_is_anomaly(self):
        s = pd.Series([500.0, 675.0])  # +35%
        issues = self.v.detect_price_anomaly(s)
        assert len(issues) == 1
        assert "price move" in issues[0]


class TestAssertUniverseFresh:
    def setup_method(self):
        self.v = DataValidator()

    def test_all_fresh_passes(self):
        n = 100
        expected = date(2026, 5, 11)
        idx = pd.bdate_range(end=str(expected), periods=n)
        df = pd.DataFrame(
            {"A": pd.Series(range(n), index=idx), "B": pd.Series(range(n), index=idx)}
        )
        self.v.assert_universe_fresh(df, expected)  # should not raise

    def test_all_stale_raises(self):
        from squant.domain.exceptions import DataQualityError
        n = 100
        old_date = date(2026, 5, 8)  # Friday
        idx = pd.bdate_range(end=str(old_date), periods=n)
        df = pd.DataFrame({"A": pd.Series(range(n), index=idx)})
        with pytest.raises(DataQualityError):
            self.v.assert_universe_fresh(df, date(2026, 5, 11))
