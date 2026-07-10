"""Tests for DailyRunner._dispatch — especially SIGNAL_SENT state handling."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from squant.application.daily_runner import DailyRunner
from squant.config.settings import Settings
from squant.domain.enums import ExecutionStatus, SystemState
from squant.domain.models import (
    CircuitBreakerStatus,
    PendingSignal,
    PortfolioState,
    Signal,
)

JST = timezone(timedelta(hours=9))
TODAY = date(2026, 5, 11)
NOW = datetime(2026, 5, 11, 20, 15, tzinfo=JST)
YESTERDAY = date(2026, 5, 8)  # previous trading day


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_signal(generated_at: datetime | None = None) -> Signal:
    return Signal(
        ticker="1234.T",
        reference_price=Decimal("500"),
        shares=100,
        cancel_above_price=Decimal("510"),
        stop_loss_price=Decimal("487"),
        rsi=35.0,
        reason="test",
        generated_at=generated_at or NOW,
    )


def _make_pending(
    status: ExecutionStatus = ExecutionStatus.PENDING,
    generated_at: datetime | None = None,
    actual_price: float | None = None,
    actual_shares: int | None = None,
) -> PendingSignal:
    return PendingSignal(
        signal=_make_signal(generated_at),
        execution_status=status,
        actual_entry_price=Decimal(str(actual_price)) if actual_price else None,
        actual_shares=actual_shares,
    )


def _make_runner(
    portfolio_state: SystemState = SystemState.IDLE,
    pending: PendingSignal | None = None,
) -> tuple[DailyRunner, MagicMock]:
    state_repo = MagicMock()
    state_repo.load_portfolio.return_value = PortfolioState(
        state=portfolio_state,
        cash_jpy=Decimal("100000"),
    )
    state_repo.load_pending_signal.return_value = pending
    state_repo.load_pending_signals.return_value = (pending,) if pending is not None else ()
    state_repo.load_circuit_breaker.return_value = CircuitBreakerStatus(
        is_tripped=False, cumulative_loss_jpy=Decimal("0"), tripped_at=None
    )
    state_repo.has_run_today.return_value = False

    market_data = MagicMock()
    market_data.check_connectivity.return_value = True

    notifier = MagicMock()

    clock = MagicMock()
    clock.today_jst.return_value = TODAY
    clock.now_jst.return_value = NOW

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        bypass_execution_time_guard=False,
        bypass_trading_day_check=False,
    )

    idle_pipeline = MagicMock()
    idle_pipeline.run.return_value = PortfolioState(
        state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
    )

    holding_pipeline = MagicMock()
    settling_pipeline = MagicMock()

    runner = DailyRunner(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        clock=clock,
        settings=settings,
        idle_pipeline=idle_pipeline,
        holding_pipeline=holding_pipeline,
        settling_pipeline=settling_pipeline,
    )
    return runner, state_repo


# ── SIGNAL_SENT state tests ───────────────────────────────────────────────────

class TestDispatchSignalSent:
    def test_pending_none_resets_to_idle(self):
        """Bug fix: SIGNAL_SENT + pending=None should NOT call idle_pipeline."""
        runner, state_repo = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT,
            pending=None,
        )
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.IDLE
        runner._idle.run.assert_not_called()

    def test_pending_none_saves_idle_state(self):
        runner, state_repo = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT,
            pending=None,
        )
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
        )
        runner._dispatch(portfolio, "run1")
        state_repo.save_portfolio.assert_called_once()
        saved = state_repo.save_portfolio.call_args[0][0]
        assert saved.state == SystemState.IDLE

    def test_filled_confirms_entry(self):
        pending = _make_pending(
            status=ExecutionStatus.FILLED,
            actual_price=502.0,
            actual_shares=100,
        )
        runner, _ = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT,
            pending=pending,
        )
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.HOLDING

    def test_cancelled_resets_to_idle(self):
        pending = _make_pending(status=ExecutionStatus.CANCELLED)
        runner, _ = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT,
            pending=pending,
        )
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.IDLE
        runner._idle.run.assert_not_called()

    def test_same_day_pending_waits(self):
        """Signal generated today + still PENDING → return same portfolio (wait)."""
        pending = _make_pending(
            status=ExecutionStatus.PENDING,
            generated_at=NOW,  # same day
        )
        runner, _ = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT,
            pending=pending,
        )
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.SIGNAL_SENT
        runner._idle.run.assert_not_called()

    def test_timed_out_pending_resets_to_idle(self):
        """Signal generated yesterday + still PENDING → timeout → IDLE."""
        yesterday_dt = datetime(2026, 5, 8, 20, 15, tzinfo=JST)
        pending = _make_pending(
            status=ExecutionStatus.PENDING,
            generated_at=yesterday_dt,
        )
        runner, state_repo = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT,
            pending=pending,
        )
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000")
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.IDLE
        runner._notifier.send.assert_called_once()
        msg = runner._notifier.send.call_args[0][0]
        assert "オペレータ応答なし" in msg


# ── IDLE / HOLDING / SETTLING dispatch ───────────────────────────────────────

class TestDispatchOtherStates:
    def test_idle_calls_idle_pipeline(self):
        runner, _ = _make_runner(portfolio_state=SystemState.IDLE)
        portfolio = PortfolioState(state=SystemState.IDLE, cash_jpy=Decimal("100000"))
        runner._dispatch(portfolio, "run1")
        runner._idle.run.assert_called_once_with(portfolio, "run1")

    def test_holding_calls_holding_pipeline(self):
        runner, _ = _make_runner(portfolio_state=SystemState.HOLDING)
        portfolio = PortfolioState(state=SystemState.HOLDING, cash_jpy=Decimal("0"))
        runner._dispatch(portfolio, "run1")
        runner._holding.run.assert_called_once_with(portfolio, "run1")

    def test_settling_calls_settling_pipeline(self):
        runner, _ = _make_runner(portfolio_state=SystemState.SETTLING)
        portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=Decimal("100000"),
            settle_dates=(date(2026, 5, 13),),
        )
        runner._dispatch(portfolio, "run1")
        runner._settling.run.assert_called_once_with(portfolio, "run1")


# ── Multi-pending confirmation (2026-05-30) ───────────────────────────────────

def _make_signal_for(ticker: str, generated_at: datetime | None = None) -> Signal:
    return Signal(
        ticker=ticker,
        reference_price=Decimal("500"),
        shares=100,
        cancel_above_price=Decimal("510"),
        stop_loss_price=Decimal("487"),
        rsi=35.0,
        reason="test",
        generated_at=generated_at or NOW,
    )


def _make_pending_for(
    ticker: str,
    status: ExecutionStatus = ExecutionStatus.PENDING,
    generated_at: datetime | None = None,
    actual_price: float | None = None,
    actual_shares: int | None = None,
) -> PendingSignal:
    return PendingSignal(
        signal=_make_signal_for(ticker, generated_at),
        execution_status=status,
        actual_entry_price=Decimal(str(actual_price)) if actual_price else None,
        actual_shares=actual_shares,
    )


class TestMultiPendingConfirmation:
    def _runner_with_pendings(self, pendings: tuple):
        runner, state_repo = _make_runner(portfolio_state=SystemState.SIGNAL_SENT)
        # Override the multi-load to return our prepared tuple
        state_repo.load_pending_signals.return_value = pendings
        return runner, state_repo

    def test_two_filled_signals_both_confirmed(self):
        """Both PendingSignals are FILLED → both positions appended → HOLDING."""
        pendings = (
            _make_pending_for("A.T", status=ExecutionStatus.FILLED,
                              actual_price=500.0, actual_shares=100),
            _make_pending_for("B.T", status=ExecutionStatus.FILLED,
                              actual_price=600.0, actual_shares=100),
        )
        runner, _ = self._runner_with_pendings(pendings)
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT,
            cash_jpy=Decimal("150000"),
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.HOLDING
        assert {p.ticker for p in result.positions} == {"A.T", "B.T"}
        # Cash decreased by both fills: 100*500 + 100*600 = 110000
        assert result.cash_jpy == Decimal("40000")

    def test_one_filled_one_pending_keeps_partial(self):
        """One FILLED + one same-day PENDING → HOLDING + remaining pending kept."""
        pendings = (
            _make_pending_for("A.T", status=ExecutionStatus.FILLED,
                              actual_price=500.0, actual_shares=100),
            _make_pending_for("B.T", status=ExecutionStatus.PENDING,
                              generated_at=NOW),  # same day
        )
        runner, _ = self._runner_with_pendings(pendings)
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT,
            cash_jpy=Decimal("200000"),
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.HOLDING
        assert len(result.positions) == 1
        assert result.positions[0].ticker == "A.T"
        # B.T still pending (kept for operator)
        assert len(result.pending_signals) == 1
        assert result.pending_signals[0].signal.ticker == "B.T"

    def test_mixed_cancelled_and_filled(self):
        """One CANCELLED + one FILLED → HOLDING with only the filled position."""
        pendings = (
            _make_pending_for("A.T", status=ExecutionStatus.CANCELLED),
            _make_pending_for("B.T", status=ExecutionStatus.FILLED,
                              actual_price=500.0, actual_shares=100),
        )
        runner, _ = self._runner_with_pendings(pendings)
        portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT,
            cash_jpy=Decimal("100000"),
        )
        result = runner._dispatch(portfolio, "run1")
        assert result.state == SystemState.HOLDING
        assert {p.ticker for p in result.positions} == {"B.T"}
        # Cancelled signal is dropped (not kept as pending)
        assert result.pending_signals == ()


# ── Circuit breaker gate（2026-07-10 F-1 対応）────────────────────────────────

def _tripped_status() -> CircuitBreakerStatus:
    return CircuitBreakerStatus(
        is_tripped=True, cumulative_loss_jpy=Decimal("95000"), tripped_at=NOW,
    )


class TestCircuitBreakerGate:
    def test_tripped_holding_continues_exit_management(self):
        """発動中でも HOLDING は dispatch され、出口管理が継続する（F-1 修正の核心）"""
        runner, state_repo = _make_runner(portfolio_state=SystemState.HOLDING)
        state_repo.load_circuit_breaker.return_value = _tripped_status()
        runner._holding.run.return_value = PortfolioState(
            state=SystemState.HOLDING, cash_jpy=Decimal("100000"),
        )
        result = runner.run()
        runner._holding.run.assert_called_once()
        assert result.success is True
        assert result.note != "circuit_breaker_tripped"

    def test_tripped_idle_halts_new_entries(self):
        """発動中の IDLE は新規シグナル生成をしない"""
        runner, state_repo = _make_runner(portfolio_state=SystemState.IDLE)
        state_repo.load_circuit_breaker.return_value = _tripped_status()
        result = runner.run()
        runner._idle.run.assert_not_called()
        assert result.note == "circuit_breaker_tripped"

    def test_tripped_signal_sent_cancels_pendings(self):
        """発動中に未消化 pending が残っていたらキャンセルして発注を防ぐ"""
        pending = _make_pending(ExecutionStatus.PENDING)
        runner, state_repo = _make_runner(
            portfolio_state=SystemState.SIGNAL_SENT, pending=pending,
        )
        state_repo.load_portfolio.return_value = PortfolioState(
            state=SystemState.SIGNAL_SENT, cash_jpy=Decimal("100000"),
            pending_signals=(pending,),
        )
        state_repo.load_circuit_breaker.return_value = _tripped_status()
        result = runner.run()
        state_repo.cancel_pending_signal.assert_called_once_with(None)
        runner._idle.run.assert_not_called()
        assert result.note == "circuit_breaker_tripped"
