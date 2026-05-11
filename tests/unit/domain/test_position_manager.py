"""Tests for position exit rule evaluation including take-profit."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from squant.domain.enums import ExitReason
from squant.domain.models import Position
from squant.domain.position_manager import evaluate_exit
from squant.domain.quantity_calculator import compute_take_profit_price


def make_position(
    entry_price: float = 500.0,
    entry_date: date = date(2026, 5, 7),
    stop_loss_rate: float = 0.025,
) -> Position:
    ep = Decimal(str(entry_price))
    stop = ep * (1 - Decimal(str(stop_loss_rate)))
    return Position(
        ticker="1234.T",
        shares=100,
        entry_price=ep,
        intended_entry_price=ep,
        entry_date=entry_date,
        stop_loss_price=stop,
        trailing_stop_price=stop,
        highest_price_since_entry=ep,
        time_stop_date=date(2026, 5, 14),
    )


def make_ohlcv(n: int = 20, base: float = 500.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    idx = pd.bdate_range(end="2026-05-11", periods=n)
    close = pd.Series([base] * n, index=idx, dtype=float)
    high = close + 5.0
    low = close - 5.0
    return high, low, close


class TestTimeStop:
    def test_exit_after_5_trading_days(self):
        # entry 2026-05-07 (Thu), 5 trading days later = 2026-05-14 (Thu)
        pos = make_position(entry_date=date(2026, 5, 7))
        high, low, close = make_ohlcv()
        decision = evaluate_exit(pos, date(2026, 5, 14), Decimal("500"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TIME_STOP

    def test_no_exit_at_4_trading_days(self):
        pos = make_position(entry_date=date(2026, 5, 7))
        high, low, close = make_ohlcv()
        decision = evaluate_exit(pos, date(2026, 5, 13), Decimal("500"), high, low, close)
        assert not decision.should_exit


class TestStopLoss:
    def test_exit_below_stop_loss_price(self):
        pos = make_position(entry_price=500.0)
        # stop = 500 * 0.975 = 487.5; 485 < 487.5 → stop-loss
        high, low, close = make_ohlcv(base=485.0)
        decision = evaluate_exit(pos, date(2026, 5, 11), Decimal("485"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS

    def test_no_exit_above_stop_loss(self):
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=490.0)
        decision = evaluate_exit(pos, date(2026, 5, 11), Decimal("490"), high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.STOP_LOSS


class TestTakeProfit:
    def test_take_profit_price_formula(self):
        """TP = entry*(1+s)*(1+r)/(1-s) — 0.5% spread, 7% net target."""
        entry = Decimal("500")
        tp = compute_take_profit_price(entry)
        # 500 * 1.005 * 1.07 / 0.995 = 540.37...
        assert float(tp) == pytest.approx(500.0 * 1.005 * 1.07 / 0.995, rel=1e-6)
        assert float(tp) > 540.0
        assert float(tp) < 541.0

    def test_exit_at_take_profit(self):
        """Price at or above TP triggers TAKE_PROFIT exit."""
        pos = make_position(entry_price=500.0)
        tp_price = compute_take_profit_price(pos.entry_price)
        # Use price clearly above TP
        exit_price = Decimal(str(round(float(tp_price) + 1.0, 1)))
        high, low, close = make_ohlcv(base=float(exit_price))
        decision = evaluate_exit(pos, date(2026, 5, 11), exit_price, high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT
        assert "net +7%" in decision.note

    def test_no_exit_below_take_profit(self):
        """Price 6% above entry (below TP ≈ 8.1% gross) should not trigger TP."""
        pos = make_position(entry_price=500.0)
        price = Decimal("530")  # +6% — below TP
        high, low, close = make_ohlcv(base=float(price))
        decision = evaluate_exit(pos, date(2026, 5, 11), price, high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.TAKE_PROFIT

    def test_take_profit_requires_spread_adjusted_gross(self):
        """Gross 7% gain is NOT enough because spread eats into net return."""
        pos = make_position(entry_price=500.0)
        # gross +7% = 535; but TP (net +7%) ≈ 540.38 → should NOT trigger TP at 535
        price = Decimal("535")
        high, low, close = make_ohlcv(base=float(price))
        decision = evaluate_exit(pos, date(2026, 5, 11), price, high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.TAKE_PROFIT


class TestExitPriority:
    def test_time_stop_beats_take_profit(self):
        """After 5 days, time-stop fires even if above TP."""
        pos = make_position(entry_price=500.0, entry_date=date(2026, 5, 7))
        # 5 trading days from 2026-05-07 = 2026-05-14
        tp_price = compute_take_profit_price(pos.entry_price)
        exit_price = Decimal(str(round(float(tp_price) + 5.0, 1)))
        high, low, close = make_ohlcv(base=float(exit_price))
        decision = evaluate_exit(pos, date(2026, 5, 14), exit_price, high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TIME_STOP
