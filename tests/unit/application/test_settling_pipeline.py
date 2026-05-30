"""Tests for SettlingPipeline multi-settlement support (B phase, 2026-05-30)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from squant.application.pipelines.settling_pipeline import SettlingPipeline
from squant.config.settings import Settings
from squant.domain.enums import SystemState
from squant.domain.models import PortfolioState, Position

JST = timezone(timedelta(hours=9))


def _make_pipeline(today: date = date(2026, 5, 13)):
    repo = MagicMock()
    notifier = MagicMock()
    clock = MagicMock()
    clock.today_jst.return_value = today
    clock.now_jst.return_value = datetime.combine(today, datetime.min.time(), tzinfo=JST)
    pipeline = SettlingPipeline(
        state_repo=repo,
        notifier=notifier,
        clock=clock,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
    )
    return pipeline, repo, notifier


def _make_position(ticker: str) -> Position:
    return Position(
        ticker=ticker,
        shares=100,
        entry_price=Decimal("500"),
        intended_entry_price=Decimal("500"),
        entry_date=date(2026, 5, 6),
        stop_loss_price=Decimal("487"),
        trailing_stop_price=Decimal("487"),
        highest_price_since_entry=Decimal("500"),
        time_stop_date=date(2026, 5, 13),
    )


class TestSettlingPipeline:
    def test_no_settle_dates_resets_to_idle(self):
        """SETTLING state with no settle_dates → IDLE."""
        pipeline, repo, _ = _make_pipeline()
        portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=Decimal("100000"),
            settle_dates=(),
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.IDLE
        repo.save_portfolio.assert_called_once()

    def test_all_dates_unlocked_transitions_idle(self):
        """All settle_dates have passed → IDLE + cumulative notify."""
        today = date(2026, 5, 13)
        pipeline, _, notifier = _make_pipeline(today)
        portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=Decimal("200000"),
            settle_dates=(date(2026, 5, 10), date(2026, 5, 11)),
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.IDLE
        assert result.settle_dates == ()
        # Notification mentions full release
        assert notifier.send.called
        msg = notifier.send.call_args[0][0]
        assert "全銘柄" in msg or "解放" in msg

    def test_partial_unlock_keeps_remaining(self):
        """Some dates passed, some still locked → SETTLING (with remaining)."""
        today = date(2026, 5, 13)
        pipeline, _, notifier = _make_pipeline(today)
        portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=Decimal("100000"),
            settle_dates=(date(2026, 5, 10), date(2026, 5, 20)),  # past, future
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.SETTLING
        assert result.settle_dates == (date(2026, 5, 20),)
        # Notification mentions partial
        msg = notifier.send.call_args[0][0]
        assert "部分" in msg or "1件解放" in msg

    def test_all_locked_no_state_change(self):
        """All settle_dates still in the future → notify + stay SETTLING (no save)."""
        today = date(2026, 5, 13)
        pipeline, repo, _ = _make_pipeline(today)
        portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=Decimal("100000"),
            settle_dates=(date(2026, 5, 20),),
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.SETTLING
        assert result.settle_dates == (date(2026, 5, 20),)
        # No save when nothing changed
        repo.save_portfolio.assert_not_called()

    def test_unlock_with_positions_transitions_holding(self):
        """All settle_dates unlocked but positions still held → HOLDING."""
        today = date(2026, 5, 13)
        pipeline, _, _ = _make_pipeline(today)
        portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=Decimal("100000"),
            positions=(_make_position("A.T"),),
            settle_dates=(date(2026, 5, 10),),
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.HOLDING
        assert result.positions == portfolio.positions
        assert result.settle_dates == ()
