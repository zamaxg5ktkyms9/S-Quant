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
    """Return updated CircuitBreakerStatus after recording a completed trade P&L.

    cumulative_loss_jpy は**純**累積損失（勝ちトレードで相殺される。正=純損失、
    負=純利益）。設計（requirements §サーキットブレーカーの定義）どおり
    「1銘柄で大損しても他銘柄が稼いでいれば停止しない」ネット判定。

    2026-07-10 独立レビュー F-1 対応: 旧実装は損失絶対値のみを片側累積しており、
    勝率〜50%・多数の小損を出す noTP 構成では成績と無関係に数ヶ月で発動していた。

    一度 tripped になったら手動リセットまで解除しない（sticky）。
    """
    if trade.pnl_jpy is None:
        return status

    # 損失は加算・利益は減算のネット累積（正の値 = リセット時点からの純損失）
    new_cumulative = status.cumulative_loss_jpy - trade.pnl_jpy
    tripped = status.is_tripped or new_cumulative >= threshold

    return CircuitBreakerStatus(
        is_tripped=tripped,
        cumulative_loss_jpy=new_cumulative,
        tripped_at=now_jst() if tripped and not status.is_tripped else status.tripped_at,
    )
