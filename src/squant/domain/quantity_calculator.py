"""Share quantity calculation for S-shares (1-share unit)."""

from decimal import Decimal

from squant.config.constants import DEFAULT_BUDGET_JPY, GAP_UP_CANCEL_THRESHOLD
from squant.domain.exceptions import InsufficientCapitalError


def compute_quantity(
    available_cash: Decimal,
    prev_close: Decimal,
    gap_up_threshold: Decimal = GAP_UP_CANCEL_THRESHOLD,
    budget: Decimal = DEFAULT_BUDGET_JPY,
) -> int:
    """Return share count (S-shares: 1-share units).

    Uses the worst-case execution price (prev_close * (1 + gap_up_threshold))
    so that capital is provably never exceeded if the order fills below
    the cancellation threshold.
    """
    effective_budget = min(available_cash, budget)
    worst_case_price = prev_close * (1 + gap_up_threshold)
    qty = int(effective_budget // worst_case_price)

    if qty <= 0:
        raise InsufficientCapitalError(
            f"Insufficient capital: ¥{effective_budget} cannot buy 1 share at ¥{prev_close}"
        )
    return qty


def compute_cancel_threshold(prev_close: Decimal, gap_up_threshold: Decimal) -> Decimal:
    """Return the yen price above which the operator should cancel the order."""
    return prev_close * (1 + gap_up_threshold)


def compute_stop_loss_price(entry_price: Decimal, stop_loss_rate: Decimal) -> Decimal:
    return entry_price * (1 - stop_loss_rate)
