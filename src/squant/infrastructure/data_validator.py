"""Data quality validation layer for yfinance data."""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

import pandas as pd

from squant.config.constants import (
    ANOMALY_PRICE_CHANGE_MAX,
    HISTORY_DAYS_REQUIRED,
    NAN_RATIO_MAX,
    VOLUME_SPIKE_MAX_RATIO,
)
from squant.domain.exceptions import DataQualityError
from squant.utils.logging import get_logger

logger = get_logger(__name__)

_OHLC_COLS = ("Open", "High", "Low", "Close", "Volume")


class Severity(Enum):
    OK = 0
    SKIP_TICKER = 1
    ABORT_RUN = 2


@dataclass
class ValidationResult:
    severity: Severity
    issues: list[str] = field(default_factory=list)
    ticker: str = ""

    @property
    def ok(self) -> bool:
        return self.severity == Severity.OK


class DataValidator:
    def validate_close_series(
        self, ticker: str, close: pd.Series, expected_date: date
    ) -> ValidationResult:
        issues: list[str] = []

        if close is None or close.empty:
            return ValidationResult(Severity.SKIP_TICKER, ["empty series"], ticker)

        # Depth check
        if len(close) < HISTORY_DAYS_REQUIRED:
            return ValidationResult(
                Severity.SKIP_TICKER,
                [f"insufficient history: {len(close)} < {HISTORY_DAYS_REQUIRED}"],
                ticker,
            )

        # NaN ratio
        nan_ratio = close.isna().mean()
        if nan_ratio > NAN_RATIO_MAX:
            issues.append(f"NaN ratio {nan_ratio:.1%} exceeds {NAN_RATIO_MAX:.1%}")

        # Freshness
        if not self.check_freshness(close, expected_date):
            issues.append(f"stale: last bar {close.index[-1].date()}, expected {expected_date}")

        # Price anomaly
        issues.extend(self.detect_price_anomaly(close))

        if issues:
            return ValidationResult(Severity.SKIP_TICKER, issues, ticker)
        return ValidationResult(Severity.OK, [], ticker)

    def validate_volume_series(self, ticker: str, volume: pd.Series) -> ValidationResult:
        issues: list[str] = []

        if volume is None or volume.empty:
            return ValidationResult(Severity.SKIP_TICKER, ["empty volume series"], ticker)

        last_vol = volume.iloc[-1]
        if pd.isna(last_vol) or last_vol == 0:
            issues.append("zero or NaN volume — possible trading halt")
            return ValidationResult(Severity.SKIP_TICKER, issues, ticker)

        med_vol = volume.iloc[-21:-1].median()
        if med_vol > 0 and last_vol > VOLUME_SPIKE_MAX_RATIO * med_vol:
            issues.append(f"volume spike {last_vol/med_vol:.1f}× median — possible data error")
            return ValidationResult(Severity.SKIP_TICKER, issues, ticker)

        return ValidationResult(Severity.OK, [], ticker)

    def check_freshness(self, series: pd.Series, expected_date: date) -> bool:
        if series.empty:
            return False
        last_date = series.index[-1]
        if hasattr(last_date, "date"):
            last_date = last_date.date()
        return last_date == expected_date

    def detect_price_anomaly(self, close: pd.Series) -> list[str]:
        issues: list[str] = []
        if len(close) < 2:
            return issues
        prev = close.iloc[-2]
        last = close.iloc[-1]
        if prev > 0:
            move = abs(float(last) / float(prev) - 1)
            if move > ANOMALY_PRICE_CHANGE_MAX:
                issues.append(
                    f"price move {move:.1%} exceeds {ANOMALY_PRICE_CHANGE_MAX:.0%} threshold"
                )
        return issues

    def assert_universe_fresh(
        self,
        close_df: pd.DataFrame,
        expected_date: date,
        min_fresh_ratio: float = 0.5,
    ) -> None:
        """Abort the run if fewer than min_fresh_ratio of tickers have today's data."""
        if close_df.empty:
            raise DataQualityError("close_df is empty — no market data fetched")

        fresh_count = sum(
            1
            for col in close_df.columns
            if not close_df[col].dropna().empty
            and self.check_freshness(close_df[col].dropna(), expected_date)
        )
        ratio = fresh_count / len(close_df.columns)
        if ratio < min_fresh_ratio:
            raise DataQualityError(
                f"Only {fresh_count}/{len(close_df.columns)} tickers have fresh data "
                f"({ratio:.0%} < {min_fresh_ratio:.0%}) — possible yfinance outage"
            )
