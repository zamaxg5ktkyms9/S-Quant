"""Tests for position exit rule evaluation (改訂版: 単元株・ザラ場モード・+6% TP)."""

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


# ── 終値モード（intraday_*=None）────────────────────────────────────────────

class TestTimeStopCloseMode:
    def test_exit_after_5_trading_days_when_no_other_trigger(self):
        # entry 2026-05-07, 5営業日後 = 2026-05-14。値動きはニュートラル
        pos = make_position(entry_date=date(2026, 5, 7))
        high, low, close = make_ohlcv()
        decision = evaluate_exit(pos, date(2026, 5, 14), Decimal("500"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TIME_STOP

    def test_no_exit_at_4_trading_days_neutral_price(self):
        pos = make_position(entry_date=date(2026, 5, 7))
        high, low, close = make_ohlcv()
        decision = evaluate_exit(pos, date(2026, 5, 13), Decimal("500"), high, low, close)
        assert not decision.should_exit


class TestStopLossCloseMode:
    def test_exit_below_stop_loss_price(self):
        pos = make_position(entry_price=500.0)
        # stop = 487.5; 485 < 487.5 → stop-loss
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


class TestTakeProfitCloseMode:
    def test_take_profit_price_is_6pct_above_entry(self):
        entry = Decimal("500")
        tp = compute_take_profit_price(entry)
        # 単元株+ゼロ革命: spread=0 → entry × 1.06 = 530
        assert float(tp) == pytest.approx(530.0, rel=1e-6)

    def test_exit_at_take_profit(self):
        pos = make_position(entry_price=500.0)
        tp_price = compute_take_profit_price(pos.entry_price)
        exit_price = Decimal(str(round(float(tp_price) + 1.0, 1)))
        high, low, close = make_ohlcv(base=float(exit_price))
        decision = evaluate_exit(pos, date(2026, 5, 11), exit_price, high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT

    def test_no_exit_below_take_profit(self):
        pos = make_position(entry_price=500.0)
        price = Decimal("520")  # +4%、TP(530)未満
        high, low, close = make_ohlcv(base=float(price))
        decision = evaluate_exit(pos, date(2026, 5, 11), price, high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.TAKE_PROFIT


# ── ザラ場モード（intraday_high/low 渡し）─────────────────────────────────

class TestIntradayMode:
    def test_intraday_low_triggers_stop_at_stop_price(self):
        """日中安値がストップ価格を割り込んだら、ストップ価格で約定。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=495.0)
        # 終値¥495だが日中安値¥486で逆指値発動
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("495"), high, low, close,
            intraday_high=Decimal("497"), intraday_low=Decimal("486"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS
        assert decision.exit_price == pos.stop_loss_price

    def test_intraday_high_triggers_tp_at_tp_price(self):
        """日中高値が利確価格に到達したら、利確価格で約定。"""
        pos = make_position(entry_price=500.0)
        # 終値¥525だが日中高値¥532でTP(530)発動
        high, low, close = make_ohlcv(base=525.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("525"), high, low, close,
            intraday_high=Decimal("532"), intraday_low=Decimal("523"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT
        assert decision.exit_price == compute_take_profit_price(pos.entry_price)

    def test_intraday_no_trigger_when_in_range(self):
        """日中の高安がストップにもTPにも触れない場合は継続。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=505.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("505"), high, low, close,
            intraday_high=Decimal("510"), intraday_low=Decimal("498"),
        )
        assert not decision.should_exit


class TestExitPriority:
    def test_stop_loss_beats_take_profit_in_intraday(self):
        """同日にlow≤stopとhigh≥tpが両方成立 → 保守的にストップ優先。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=510.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("510"), high, low, close,
            intraday_high=Decimal("532"), intraday_low=Decimal("485"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS

    def test_take_profit_beats_time_stop(self):
        """改訂版: ザラ場でTPに到達したらタイムストップより優先（早期に約定）。"""
        pos = make_position(entry_price=500.0, entry_date=date(2026, 5, 7))
        tp_price = compute_take_profit_price(pos.entry_price)
        exit_price = Decimal(str(round(float(tp_price) + 1.0, 1)))
        high, low, close = make_ohlcv(base=float(exit_price))
        # 5営業日経過日にTPも同時成立 → TAKE_PROFITが優先
        decision = evaluate_exit(
            pos, date(2026, 5, 14), exit_price, high, low, close,
            intraday_high=exit_price, intraday_low=Decimal("525"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT
