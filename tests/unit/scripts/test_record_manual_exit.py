"""Unit tests for record_manual_exit (intraday stop fill reconciliation)."""

import sys
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from record_manual_exit import (
    ManualExitError,
    build_manual_exit,
    default_intended_price,
)

from squant.domain.enums import ExitReason, SystemState
from squant.domain.models import PortfolioState, Position

# 2026-07-10（金）= TSE 営業日。実例: 2201.T のザラ場ストップ約定
_SALE_DATE = date(2026, 7, 10)


def _position(**overrides) -> Position:
    kwargs = {
        "ticker": "2201.T",
        "shares": 100,
        "entry_price": Decimal("2717.5"),
        "intended_entry_price": Decimal("2733.5"),
        "entry_date": date(2026, 7, 9),
        "stop_loss_price": Decimal("2649.5625"),
        "trailing_stop_price": Decimal("2649.5625"),
        "highest_price_since_entry": Decimal("2717.5"),
        "time_stop_date": date(2026, 7, 16),
    }
    kwargs.update(overrides)
    return Position(**kwargs)


def _portfolio(**overrides) -> PortfolioState:
    kwargs = {
        "state": SystemState.HOLDING,
        "cash_jpy": Decimal("328250.0"),
        "positions": (_position(),),
        "cumulative_pnl_jpy": Decimal("0"),
    }
    kwargs.update(overrides)
    return PortfolioState(**kwargs)


class TestBuildManualExit:
    def test_live_production_case(self):
        """実例: 2201.T ×100 @¥2,663 売却（7/10）"""
        result = build_manual_exit(
            _portfolio(), ticker="2201.T", price=Decimal("2663"),
            sale_date=_SALE_DATE, reason=ExitReason.STOP_LOSS,
        )
        assert result.pnl_jpy == Decimal("-5450.0")
        assert result.new_portfolio.cash_jpy == Decimal("594550.0")
        assert result.new_portfolio.state == SystemState.SETTLING
        assert result.new_portfolio.positions == ()
        assert result.new_portfolio.cumulative_pnl_jpy == Decimal("-5450.0")
        # T+2: 金曜売却 → 火曜受渡
        assert result.settle_date == date(2026, 7, 14)
        assert result.new_portfolio.settle_dates == (date(2026, 7, 14),)
        # 恒等式: 現金 + 保有原価(0) = 初期資本 + 実現損益
        assert result.new_portfolio.cash_jpy == Decimal("600000") + result.pnl_jpy
        # trades 行
        assert result.trade.price == Decimal("2663")
        assert result.trade.pnl_jpy == Decimal("-5450.0")
        assert result.trade.exit_reason == ExitReason.STOP_LOSS
        assert result.trade.executed_at.date() == _SALE_DATE
        assert result.recent_sale.settlement_date == date(2026, 7, 14)

    def test_multi_position_keeps_holding(self):
        other = _position(ticker="9999.T", entry_price=Decimal("1000"),
                          stop_loss_price=Decimal("975"),
                          trailing_stop_price=Decimal("975"))
        pf = _portfolio(positions=(_position(), other))
        result = build_manual_exit(
            pf, ticker="2201.T", price=Decimal("2663"),
            sale_date=_SALE_DATE, reason=ExitReason.STOP_LOSS,
        )
        assert result.new_portfolio.state == SystemState.HOLDING
        assert [p.ticker for p in result.new_portfolio.positions] == ["9999.T"]

    def test_unknown_ticker_rejected(self):
        with pytest.raises(ManualExitError, match="保有していません"):
            build_manual_exit(
                _portfolio(), ticker="7203.T", price=Decimal("2663"),
                sale_date=_SALE_DATE, reason=ExitReason.STOP_LOSS,
            )

    def test_price_deviation_guard(self):
        """±15% 超の価格は桁誤りとして拒否"""
        with pytest.raises(ManualExitError, match="乖離"):
            build_manual_exit(
                _portfolio(), ticker="2201.T", price=Decimal("26630"),
                sale_date=_SALE_DATE, reason=ExitReason.STOP_LOSS,
            )
        with pytest.raises(ManualExitError, match="乖離"):
            build_manual_exit(
                _portfolio(), ticker="2201.T", price=Decimal("266"),
                sale_date=_SALE_DATE, reason=ExitReason.STOP_LOSS,
            )

    def test_non_trading_day_rejected(self):
        with pytest.raises(ManualExitError, match="営業日ではありません"):
            build_manual_exit(
                _portfolio(), ticker="2201.T", price=Decimal("2663"),
                sale_date=date(2026, 7, 11), reason=ExitReason.STOP_LOSS,  # 土曜
            )

    def test_sale_before_entry_rejected(self):
        with pytest.raises(ManualExitError, match="エントリー日.*より前"):
            build_manual_exit(
                _portfolio(), ticker="2201.T", price=Decimal("2663"),
                sale_date=date(2026, 7, 8), reason=ExitReason.STOP_LOSS,
            )

    def test_future_sale_date_rejected(self):
        with pytest.raises(ManualExitError, match="未来日"):
            build_manual_exit(
                _portfolio(), ticker="2201.T", price=Decimal("2663"),
                sale_date=date(2027, 7, 9), reason=ExitReason.STOP_LOSS,
            )


class TestDefaultIntendedPrice:
    def test_effective_stop_is_max_of_hard_and_trailing(self):
        pos = _position(trailing_stop_price=Decimal("2680"))
        assert default_intended_price(pos) == Decimal("2680")
        assert default_intended_price(_position()) == Decimal("2649.5625")
