"""Position exit rule evaluation — pure business logic, no I/O."""

from datetime import date
from decimal import Decimal

import pandas as pd

from squant.config.constants import ATR_TRAILING_MULTIPLIER, GAP_UP_CANCEL_THRESHOLD
from squant.domain.enums import ExitReason
from squant.domain.indicators import atr
from squant.domain.models import ExitDecision, Position
from squant.utils.jst import count_trading_days


def evaluate_exit(
    position: Position,
    today: date,
    latest_close: Decimal,
    high_series: pd.Series,
    low_series: pd.Series,
    close_series: pd.Series,
) -> ExitDecision:
    """Determine if the position should be exited and compute updated trailing stop.

    Returns ExitDecision with should_exit=True and the exit reason if any rule triggers.
    """
    # 1. Time stop: 5 trading days elapsed
    days_held = count_trading_days(position.entry_date, today)
    if days_held >= 5:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.TIME_STOP,
            note=f"Held {days_held} trading days (limit 5)",
        )

    # 2. Compute updated trailing stop via 1.5× ATR
    updated_trailing = _compute_trailing_stop(
        position, latest_close, high_series, low_series, close_series
    )

    # 3. Hard stop-loss check
    if latest_close <= position.stop_loss_price:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.STOP_LOSS,
            note=f"Close ¥{latest_close} ≤ stop-loss ¥{position.stop_loss_price}",
            updated_trailing_stop=updated_trailing,
        )

    # 4. Trailing stop check
    effective_stop = max(position.stop_loss_price, updated_trailing)
    if latest_close <= effective_stop:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.TRAILING_STOP,
            note=f"Close ¥{latest_close} ≤ trailing stop ¥{effective_stop}",
            updated_trailing_stop=updated_trailing,
        )

    return ExitDecision(
        should_exit=False,
        reason=None,
        note=f"HOLD ({days_held} days, RSI check at signal)",
        updated_trailing_stop=updated_trailing,
    )


def _compute_trailing_stop(
    position: Position,
    latest_close: Decimal,
    high_series: pd.Series,
    low_series: pd.Series,
    close_series: pd.Series,
) -> Decimal:
    """Compute 1.5× ATR trailing stop; only ratchets up, never down."""
    atr_series = atr(high_series, low_series, close_series, period=14)
    if atr_series.empty or pd.isna(atr_series.iloc[-1]):
        return position.trailing_stop_price

    latest_atr = Decimal(str(round(float(atr_series.iloc[-1]), 2)))
    new_stop = latest_close - ATR_TRAILING_MULTIPLIER * latest_atr
    # Only ratchet up
    return max(position.trailing_stop_price, new_stop)


def should_cancel_gap_up(
    intended_price: Decimal,
    actual_open: Decimal,
    threshold: Decimal = GAP_UP_CANCEL_THRESHOLD,
) -> bool:
    """True if the opening gap exceeds the cancellation threshold."""
    return actual_open > intended_price * (1 + threshold)
