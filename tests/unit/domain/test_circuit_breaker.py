"""Tests for circuit breaker logic."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

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
    def test_winning_trade_does_not_change_loss(self):
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        trade = make_trade(+1500)
        new = update_after_trade(status, trade)
        assert new.cumulative_loss_jpy == Decimal("0")
        assert new.is_tripped is False

    def test_losing_trade_accumulates(self):
        status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        trade = make_trade(-1250)
        new = update_after_trade(status, trade)
        assert new.cumulative_loss_jpy == Decimal("1250")

    def test_trips_at_threshold(self):
        status = CircuitBreakerStatus(
            is_tripped=False, cumulative_loss_jpy=Decimal("29000")
        )
        trade = make_trade(-1500)  # total = 30500 >= 30000
        new = update_after_trade(status, trade, threshold=Decimal("30000"))
        assert new.is_tripped is True
        assert new.cumulative_loss_jpy == Decimal("30500")

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
