"""Share quantity calculation for 単元株（100株単位） — SBI証券ゼロ革命適用、手数料0前提。"""

import math
from decimal import Decimal

from squant.config.constants import (
    DEFAULT_BUDGET_JPY,
    EXECUTION_SPREAD_RATE,
    GAP_UP_CANCEL_THRESHOLD,
    SHARES_PER_UNIT,
    TARGET_PROFIT_RATE,
)
from squant.domain.exceptions import InsufficientCapitalError


def compute_quantity(
    available_cash: Decimal,
    prev_close: Decimal,
    gap_up_threshold: Decimal = GAP_UP_CANCEL_THRESHOLD,
    budget: Decimal = DEFAULT_BUDGET_JPY,
    shares_per_unit: int = SHARES_PER_UNIT,
) -> int:
    """単元株（100株単位）での発注株数を返す。

    最悪ケース執行価格 = prev_close × (1 + gap_up_threshold) で予算超過しないよう
    floor で切り下げ、さらに shares_per_unit の倍数に丸める。
    """
    effective_budget = min(available_cash, budget)
    worst_case_price = prev_close * (1 + gap_up_threshold)
    raw_qty = math.floor(effective_budget / worst_case_price)
    qty = (raw_qty // shares_per_unit) * shares_per_unit

    if qty <= 0:
        raise InsufficientCapitalError(
            f"Insufficient capital: ¥{effective_budget} cannot buy {shares_per_unit} shares "
            f"at ¥{prev_close} (worst case ¥{worst_case_price})"
        )
    return qty


def compute_cancel_threshold(prev_close: Decimal, gap_up_threshold: Decimal) -> Decimal:
    """始値がこの価格を超えたらオペレーターが発注を見送る閾値。"""
    return prev_close * (1 + gap_up_threshold)


def compute_stop_loss_price(entry_price: Decimal, stop_loss_rate: Decimal) -> Decimal:
    return entry_price * (1 - stop_loss_rate)


def compute_take_profit_price(
    entry_price: Decimal,
    target_net_rate: Decimal = TARGET_PROFIT_RATE,
    spread_rate: Decimal = EXECUTION_SPREAD_RATE,
) -> Decimal:
    """利確価格。単元株+ゼロ革命でspread_rate=0なら entry × (1+target) と等価。

    保守的に旧S株式（両端スプレッド補正）も維持しておくが、デフォルトでは0で計算される。
    """
    if spread_rate == 0:
        return entry_price * (1 + target_net_rate)
    return entry_price * (1 + spread_rate) * (1 + target_net_rate) / (1 - spread_rate)


def compute_net_pnl(
    entry_price: Decimal,
    exit_price: Decimal,
    shares: int,
    spread_rate: Decimal = EXECUTION_SPREAD_RATE,
) -> Decimal:
    """純損益。単元株+ゼロ革命では (exit - entry) × shares と等価。"""
    if spread_rate == 0:
        return (exit_price - entry_price) * shares
    effective_entry = entry_price * (1 + spread_rate)
    effective_exit = exit_price * (1 - spread_rate)
    return (effective_exit - effective_entry) * shares
