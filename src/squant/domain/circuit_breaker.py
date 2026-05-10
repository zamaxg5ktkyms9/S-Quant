"""Circuit breaker — halts all trading when cumulative loss exceeds threshold."""

from decimal import Decimal

from squant.config.constants import CIRCUIT_BREAKER_LOSS_JPY
from squant.domain.models import CircuitBreakerStatus, TradeRecord
from squant.utils.jst import now_jst


def is_tripped(status: CircuitBreakerStatus) -> bool:
    return status.is_tripped


def update_after_trade(
    status: CircuitBreakerStatus,
    trade: TradeRecord,
    threshold: Decimal = CIRCUIT_BREAKER_LOSS_JPY,
) -> CircuitBreakerStatus:
    """Return updated CircuitBreakerStatus after recording a completed trade P&L."""
    if trade.pnl_jpy is None:
        return status

    new_cumulative = status.cumulative_loss_jpy + (
        -trade.pnl_jpy if trade.pnl_jpy < 0 else Decimal("0")
    )
    tripped = new_cumulative >= threshold

    return CircuitBreakerStatus(
        is_tripped=tripped,
        cumulative_loss_jpy=new_cumulative,
        tripped_at=now_jst() if tripped and not status.is_tripped else status.tripped_at,
    )
