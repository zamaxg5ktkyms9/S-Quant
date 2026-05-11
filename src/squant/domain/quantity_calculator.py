"""Share quantity calculation for S-shares (1-share unit)."""

import math
from decimal import Decimal

from squant.config.constants import (
    DEFAULT_BUDGET_JPY,
    GAP_UP_CANCEL_THRESHOLD,
    SSHARE_SPREAD_RATE,
    TARGET_PROFIT_RATE,
)
from squant.domain.exceptions import InsufficientCapitalError


def compute_quantity(
    available_cash: Decimal,
    prev_close: Decimal,
    gap_up_threshold: Decimal = GAP_UP_CANCEL_THRESHOLD,
    budget: Decimal = DEFAULT_BUDGET_JPY,
) -> int:
    """Return share count (S-shares: 1-share units).

    Uses worst-case execution price (prev_close * (1 + gap_up_threshold)).
    math.floor guarantees capital is NEVER exceeded even on a gap-up open.
    """
    effective_budget = min(available_cash, budget)
    worst_case_price = prev_close * (1 + gap_up_threshold)
    qty = math.floor(effective_budget / worst_case_price)

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


def compute_take_profit_price(
    entry_price: Decimal,
    target_net_rate: Decimal = TARGET_PROFIT_RATE,
    spread_rate: Decimal = SSHARE_SPREAD_RATE,
) -> Decimal:
    """Gross exit price at which net profit (after S-share spread) reaches target_net_rate.

    S株のスプレッドを考慮した純利益ベースの利確価格。
    Net P&L per share = exit_price*(1-s) - entry_price*(1+s)
    Target: exit_price*(1-s) = entry_price*(1+s)*(1+r)
    → exit_price = entry_price*(1+s)*(1+r) / (1-s)
    """
    return entry_price * (1 + spread_rate) * (1 + target_net_rate) / (1 - spread_rate)


def compute_net_pnl(
    entry_price: Decimal,
    exit_price: Decimal,
    shares: int,
    spread_rate: Decimal = SSHARE_SPREAD_RATE,
) -> Decimal:
    """Net P&L after S-share spread on both entry (ask) and exit (bid)."""
    effective_entry = entry_price * (1 + spread_rate)
    effective_exit = exit_price * (1 - spread_rate)
    return (effective_exit - effective_entry) * shares
