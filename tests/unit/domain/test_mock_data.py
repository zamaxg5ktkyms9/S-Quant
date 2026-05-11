"""Tests using mock yfinance data (mock_yfinance.json) for DataValidator scenarios."""

from datetime import date

import pytest

from squant.infrastructure.data_validator import DataValidator, Severity
from tests.fixtures import build_close_series, build_volume_series, expected_validation

EXPECTED_DATE = date(2026, 5, 11)


class TestMockDataScenarios:
    """Validate each JSON scenario against the expected DataValidator verdict."""

    def setup_method(self) -> None:
        self.v = DataValidator()

    def _check(self, scenario: str) -> None:
        close = build_close_series(scenario)
        volume = build_volume_series(scenario)
        expect = expected_validation(scenario)

        close_result = self.v.validate_close_series("MOCK.T", close, EXPECTED_DATE)
        if close_result.ok:
            vol_result = self.v.validate_volume_series("MOCK.T", volume)
            actual = "OK" if vol_result.ok else "SKIP_TICKER"
        else:
            actual = close_result.severity.name

        assert actual == expect, (
            f"Scenario '{scenario}': expected {expect}, got {actual}. "
            f"Issues: {close_result.issues}"
        )

    def test_normal_scenario(self):
        self._check("normal")

    def test_anomaly_price_up_30pct(self):
        close = build_close_series("anomaly_price_up_30pct")
        result = self.v.validate_close_series("MOCK.T", close, EXPECTED_DATE)
        assert result.severity == Severity.SKIP_TICKER
        assert any("price move" in i for i in result.issues)

    def test_anomaly_price_down_30pct(self):
        close = build_close_series("anomaly_price_down_30pct")
        result = self.v.validate_close_series("MOCK.T", close, EXPECTED_DATE)
        assert result.severity == Severity.SKIP_TICKER
        assert any("price move" in i for i in result.issues)

    def test_zero_volume_skipped(self):
        close = build_close_series("zero_volume")
        volume = build_volume_series("zero_volume")
        close_result = self.v.validate_close_series("MOCK.T", close, EXPECTED_DATE)
        assert close_result.ok  # close itself is fine
        vol_result = self.v.validate_volume_series("MOCK.T", volume)
        assert vol_result.severity == Severity.SKIP_TICKER
        assert any("zero" in i.lower() for i in vol_result.issues)

    def test_missing_data_5pct_skipped(self):
        close = build_close_series("missing_data_5pct")
        result = self.v.validate_close_series("MOCK.T", close, EXPECTED_DATE)
        assert result.severity == Severity.SKIP_TICKER
        assert any("NaN" in i for i in result.issues)

    def test_insufficient_history_skipped(self):
        close = build_close_series("insufficient_history")
        result = self.v.validate_close_series("MOCK.T", close, EXPECTED_DATE)
        assert result.severity == Severity.SKIP_TICKER
        assert any("insufficient" in i for i in result.issues)
