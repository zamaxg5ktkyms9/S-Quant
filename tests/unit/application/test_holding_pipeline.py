"""Tests for HoldingPipeline exit rule evaluation."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from squant.application.pipelines.holding_pipeline import HoldingPipeline
from squant.config.settings import Settings
from squant.domain.enums import ExitReason, SystemState
from squant.domain.models import CircuitBreakerStatus, PortfolioState, Position
from squant.infrastructure.data_validator import DataValidator

JST = timezone(timedelta(hours=9))
TODAY = date(2026, 5, 11)
NOW = datetime(2026, 5, 11, 20, 15, tzinfo=JST)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_position(
    entry_price: float = 500.0,
    entry_date: date = date(2026, 5, 6),  # 3 trading days before TODAY
    stop_loss_price: float | None = None,
) -> Position:
    ep = Decimal(str(entry_price))
    sl = Decimal(str(stop_loss_price)) if stop_loss_price else ep * Decimal("0.975")
    return Position(
        ticker="1234.T",
        shares=100,
        entry_price=ep,
        intended_entry_price=ep,
        entry_date=entry_date,
        stop_loss_price=sl,
        trailing_stop_price=sl,
        highest_price_since_entry=ep,
        time_stop_date=date(2026, 5, 13),
    )


def _make_ohlcv_df(close_price: float, n: int = 95) -> pd.DataFrame:
    """Return flat-column DataFrame matching fetch_ohlcv_full output.

    Default n=95 trading days satisfies DataValidator's HISTORY_DAYS_REQUIRED=90.
    """
    idx = pd.date_range(end=TODAY, periods=n, freq="B")
    close = pd.Series([close_price] * n, index=idx, dtype=float)
    high = close + 5.0
    low = close - 5.0
    volume = pd.Series([1_000_000.0] * n, index=idx)
    return pd.DataFrame(
        {"Adj Close": close, "High": high, "Low": low, "Volume": volume}
    )


def _make_pipeline(fake_ohlcv: pd.DataFrame | None = None) -> tuple[HoldingPipeline, MagicMock, MagicMock]:
    market_data = MagicMock()
    market_data.fetch_ohlcv_full.return_value = fake_ohlcv if fake_ohlcv is not None else _make_ohlcv_df(500.0)

    state_repo = MagicMock()
    state_repo.load_circuit_breaker.return_value = CircuitBreakerStatus(
        is_tripped=False, cumulative_loss_jpy=Decimal("0"), tripped_at=None
    )

    notifier = MagicMock()

    clock = MagicMock()
    clock.today_jst.return_value = TODAY
    clock.now_jst.return_value = NOW

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )

    pipeline = HoldingPipeline(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        validator=DataValidator(),
        clock=clock,
        settings=settings,
    )
    return pipeline, state_repo, notifier


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestHoldingPipelineNoData:
    def test_empty_ohlcv_returns_portfolio_unchanged(self):
        pipeline, state_repo, notifier = _make_pipeline(fake_ohlcv=pd.DataFrame())
        position = _make_position()
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.HOLDING
        notifier.send_error.assert_called_once()

    def test_none_position_resets_to_idle(self):
        pipeline, _, _ = _make_pipeline()
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("100000"),
            position=None,
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.IDLE


class TestHoldingPipelineStopLoss:
    def test_stop_loss_triggers_exit(self):
        # entry=500, stop=487.5; price=480 < 487.5 → stop-loss
        ohlcv = _make_ohlcv_df(close_price=480.0)
        pipeline, state_repo, notifier = _make_pipeline(fake_ohlcv=ohlcv)
        position = _make_position(entry_price=500.0)
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.SETTLING
        assert result.position is None
        notifier.send.assert_called_once()
        call_args = notifier.send.call_args[0][0]
        assert "stop" in call_args.lower() or "損切" in call_args.lower() or "exit" in call_args.lower()

    def test_pnl_negative_on_stop_loss(self):
        ohlcv = _make_ohlcv_df(close_price=480.0)
        pipeline, _, _ = _make_pipeline(fake_ohlcv=ohlcv)
        position = _make_position(entry_price=500.0)
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        # P&L = (480 - 500) * 100 = -2000
        assert result.cumulative_pnl_jpy < Decimal("0")


class TestHoldingPipelineTakeProfit:
    def test_take_profit_triggers_exit(self):
        # entry=500, TP ≈ 540.4 (net +7%); price=545 > TP → take-profit
        ohlcv = _make_ohlcv_df(close_price=545.0)
        pipeline, _, notifier = _make_pipeline(fake_ohlcv=ohlcv)
        position = _make_position(entry_price=500.0)
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.SETTLING
        assert result.cumulative_pnl_jpy > Decimal("0")


class TestHoldingPipelineTimeStop:
    def test_time_stop_after_5_days(self):
        # entry 2026-05-05 (Mon), 5 trading days later = 2026-05-12 (Tue)
        # But TODAY = 2026-05-11 (Mon), entry = 2026-05-05 → 5 trading days
        ohlcv = _make_ohlcv_df(close_price=510.0)
        pipeline, _, _ = _make_pipeline(fake_ohlcv=ohlcv)
        # 2026-04-28 → 2026-05-11 = 5 trading days (GW holidays excluded)
        position = _make_position(entry_price=500.0, entry_date=date(2026, 4, 28))
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.SETTLING


class TestHoldingPipelineHold:
    def test_hold_when_no_exit_triggered(self):
        # price=510 — above entry(500), above stop(487.5), below TP(~540)
        ohlcv = _make_ohlcv_df(close_price=510.0)
        pipeline, state_repo, notifier = _make_pipeline(fake_ohlcv=ohlcv)
        position = _make_position(entry_price=500.0, entry_date=date(2026, 5, 8))  # 2 days ago
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        assert result.state == SystemState.HOLDING
        assert result.position is not None
        assert result.position.ticker == "1234.T"
        notifier.send.assert_called_once()

    def test_highest_price_updated_on_hold(self):
        # price=520 > entry(500) → highest_price_since_entry should update
        ohlcv = _make_ohlcv_df(close_price=520.0)
        pipeline, _, _ = _make_pipeline(fake_ohlcv=ohlcv)
        position = _make_position(entry_price=500.0, entry_date=date(2026, 5, 8))
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("0"),
            position=position,
        )
        result = pipeline.run(portfolio, "run1")
        if result.state == SystemState.HOLDING and result.position:
            assert result.position.highest_price_since_entry >= Decimal("520")

    def test_dry_run_skips_repo_save(self):
        import os
        os.environ["DRY_RUN"] = "true"
        try:
            ohlcv = _make_ohlcv_df(close_price=480.0)  # triggers exit
            pipeline, state_repo, _ = _make_pipeline(fake_ohlcv=ohlcv)
            position = _make_position(entry_price=500.0)
            portfolio = PortfolioState(
                state=SystemState.HOLDING,
                cash_jpy=Decimal("0"),
                position=position,
            )
            pipeline.run(portfolio, "run1")
            state_repo.save_portfolio.assert_not_called()
        finally:
            os.environ.pop("DRY_RUN", None)
