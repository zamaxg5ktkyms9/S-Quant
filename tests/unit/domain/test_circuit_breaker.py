"""Tests for circuit breaker logic."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from squant.domain.circuit_breaker import is_tripped, update_after_trade
from squant.domain.enums import ExitReason, OrderSide
from squant.domain.models import CircuitBreakerStatus, TradeRecord

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 5, 11, 20, 0, tzinfo=JST)


def make_trade(pnl: float) -> TradeRecord:
    return TradeRecord(
        ticker="1234.T",
        side=OrderSide.SELL,
        shares=100,
        price=Decimal("500"),
        executed_at=NOW,
        pnl_jpy=Decimal(str(pnl)),
        exit_reason=ExitReason.STOP_LOSS if pnl < 0 else None,
    )


class TestIsTripped:
    def test_not_tripped_initially(self):
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        assert is_tripped(status) is False

    def test_tripped_when_flag_set(self):
        status = CircuitBreakerStatus(is_tripped=True, cumulative_loss_jpy=Decimal("30000"))
        assert is_tripped(status) is True


class TestUpdateAfterTrade:
    """2026-07-10 F-1 対応: cumulative_loss_jpy は純累積損失（勝ちで相殺）。"""

    def test_winning_trade_offsets_losses(self):
        """勝ちトレードは累積純損失を減らす（旧実装は無視していた = F-1 の核心）"""
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("5000"))
        trade = make_trade(+1500)
        new = update_after_trade(status, trade)
        assert new.cumulative_loss_jpy == Decimal("3500")
        assert new.is_tripped is False

    def test_net_profit_goes_negative(self):
        """純利益状態は負の値で表現される（損失バッファとして機能）"""
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        trade = make_trade(+8000)
        new = update_after_trade(status, trade)
        assert new.cumulative_loss_jpy == Decimal("-8000")
        # その後 ¥8,000 の損失が来ても純損失ゼロ → 発動しない
        new2 = update_after_trade(new, make_trade(-8000), threshold=Decimal("30000"))
        assert new2.cumulative_loss_jpy == Decimal("0")
        assert new2.is_tripped is False

    def test_losing_trade_accumulates(self):
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        trade = make_trade(-1250)
        new = update_after_trade(status, trade)
        assert new.cumulative_loss_jpy == Decimal("1250")

    def test_alternating_wins_losses_do_not_trip_when_net_ok(self):
        """勝率50%・期待値プラスの列で発動しない（旧実装は損失和だけで発動していた）"""
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        for _ in range(20):  # -7000 → +9000 を20回 = 損失和 ¥140k / 純利益 +¥40k
            status = update_after_trade(status, make_trade(-7000), threshold=Decimal("90000"))
            status = update_after_trade(status, make_trade(+9000), threshold=Decimal("90000"))
        assert status.is_tripped is False
        assert status.cumulative_loss_jpy == Decimal("-40000")

    def test_trips_at_threshold(self):
        status = CircuitBreakerStatus(
            is_tripped=False, cumulative_loss_jpy=Decimal("29000")
        )
        trade = make_trade(-1500)  # net = 30500 >= 30000
        new = update_after_trade(status, trade, threshold=Decimal("30000"))
        assert new.is_tripped is True
        assert new.cumulative_loss_jpy == Decimal("30500")

    def test_trip_is_sticky_until_manual_reset(self):
        """発動後に勝って純損失が閾値を下回っても自動解除しない（手動リセットのみ）"""
        status = CircuitBreakerStatus(
            is_tripped=True, cumulative_loss_jpy=Decimal("31000"), tripped_at=NOW,
        )
        new = update_after_trade(status, make_trade(+10000), threshold=Decimal("30000"))
        assert new.cumulative_loss_jpy == Decimal("21000")
        assert new.is_tripped is True  # sticky
        assert new.tripped_at == NOW

    def test_does_not_double_trip(self):
        status = CircuitBreakerStatus(
            is_tripped=True,
            cumulative_loss_jpy=Decimal("31000"),
            tripped_at=NOW,
        )
        trade = make_trade(-500)
        new = update_after_trade(status, trade, threshold=Decimal("30000"))
        assert new.is_tripped is True
        assert new.tripped_at == NOW  # unchanged

    def test_none_pnl_trade_is_ignored(self):
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("5000"))
        trade = TradeRecord(
            ticker="X", side=OrderSide.BUY, shares=1,
            price=Decimal("100"), executed_at=NOW, pnl_jpy=None,
        )
        new = update_after_trade(status, trade)
        assert new.cumulative_loss_jpy == Decimal("5000")
