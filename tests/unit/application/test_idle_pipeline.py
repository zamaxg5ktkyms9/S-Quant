"""Direct unit tests for IdlePipeline.run (multi-position scan path).

Until now IdlePipeline was only exercised through daily_runner, where it
was replaced by a MagicMock — so a runtime bug in the real body (e.g. the
Decimal-not-imported lint error in a90516e) was invisible to pytest. These
tests call IdlePipeline.run directly with all external I/O mocked, and
short-circuit the heavy domain functions (screener / signal_engine) via
monkeypatch so we exercise the pipeline's own control flow and the
Decimal-based dynamic-budget logic.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

from squant.application.pipelines.idle_pipeline import IdlePipeline
from squant.config.settings import Settings
from squant.domain import screener, signal_engine
from squant.domain.enums import SystemState
from squant.domain.models import (
    Candidate,
    PendingSignal,
    PortfolioState,
    Position,
    Signal,
)
from squant.infrastructure.data_validator import DataValidator, Severity, ValidationResult

JST = timezone(timedelta(hours=9))
TODAY = date(2026, 5, 11)
NOW = datetime(2026, 5, 11, 20, 15, tzinfo=JST)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ohlcv(tickers, n: int = 120):
    """Wide (Date × ticker) adj-close + volume frames matching fetch_ohlcv output."""
    idx = pd.date_range(end=TODAY, periods=n, freq="B")
    adj = pd.DataFrame({t: [500.0] * n for t in tickers}, index=idx)
    vol = pd.DataFrame({t: [1_000_000.0] * n for t in tickers}, index=idx)
    return adj, vol


def _filtered_df(tickers):
    """Mimic screener.apply_fundamental_filters output (ticker column + attrs)."""
    df = pd.DataFrame(
        {
            "ticker": list(tickers),
            "close": [500.0] * len(tickers),
            "market_cap_jpy": [5e9] * len(tickers),
            "pbr": [1.0] * len(tickers),
            "equity_ratio": [0.5] * len(tickers),
            "avg_5d_trading_value_jpy": [1e8] * len(tickers),
        }
    )
    df.attrs["filter_counts"] = {}
    return df


def _candidate(ticker: str, close: float = 500.0, rsi: float = 40.0,
               surge: float = 1.5, pbr: float = 1.0) -> Candidate:
    return Candidate(
        ticker=ticker,
        close=Decimal(str(close)),
        rsi14=rsi,
        volume_surge_ratio=surge,
        pbr=pbr,
        market_cap_jpy=5e9,
    )


def _make_signal(ticker: str) -> Signal:
    return Signal(
        ticker=ticker,
        reference_price=Decimal("500"),
        shares=100,
        cancel_above_price=Decimal("510"),
        stop_loss_price=Decimal("487.5"),
        rsi=40.0,
        reason="existing",
        generated_at=NOW,
    )


def _pending(ticker: str) -> PendingSignal:
    return PendingSignal(signal=_make_signal(ticker))


def _position(ticker: str) -> Position:
    ep = Decimal("500")
    return Position(
        ticker=ticker,
        shares=100,
        entry_price=ep,
        intended_entry_price=ep,
        entry_date=date(2026, 5, 6),
        stop_loss_price=ep * Decimal("0.975"),
        trailing_stop_price=ep * Decimal("0.975"),
        highest_price_since_entry=ep,
        time_stop_date=date(2026, 5, 13),
    )


def _ok_validator(abort: bool = False) -> MagicMock:
    # spec=DataValidator so `assert_universe_fresh` is a plain stubbable method
    # rather than MagicMock's special `assert_*` assertion helper.
    v = MagicMock(spec=DataValidator)
    if abort:
        v.validate_close_series.return_value = ValidationResult(
            Severity.ABORT_RUN, ["forced abort"], "X"
        )
    else:
        v.validate_close_series.return_value = ValidationResult(Severity.OK, [], "")
    v.validate_volume_series.return_value = ValidationResult(Severity.OK, [], "")
    return v


def _base_pipeline(universe, validator=None):
    market_data = MagicMock()
    market_data.fetch_ohlcv.return_value = _ohlcv(universe)
    market_data.fetch_fundamentals.return_value = pd.DataFrame()

    state_repo = MagicMock()
    state_repo.load_recent_sales.return_value = []

    notifier = MagicMock()

    clock = MagicMock()
    clock.today_jst.return_value = TODAY
    clock.now_jst.return_value = NOW

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    pipeline = IdlePipeline(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        validator=validator or _ok_validator(),
        clock=clock,
        settings=settings,
        universe=list(universe),
        blackouts=set(),
    )
    return pipeline, state_repo, notifier


def _setup(
    monkeypatch,
    *,
    universe=("A.T", "B.T"),
    strategy: str = "ma_cross",
    max_positions: int = 2,
    screen_tickers=None,
    candidates=None,
    validator=None,
):
    """Wire a pipeline with screener + signal_engine short-circuited.

    Settings are driven via env vars (monkeypatch.setenv) because the fields
    use aliases — this mirrors the DRY_RUN env pattern in test_holding_pipeline.
    """
    monkeypatch.setenv("SIGNAL_STRATEGY", strategy)
    monkeypatch.setenv("MAX_POSITIONS", str(max_positions))

    pipeline, repo, notifier = _base_pipeline(universe, validator=validator)

    screen = list(universe if screen_tickers is None else screen_tickers)
    monkeypatch.setattr(
        screener, "apply_fundamental_filters", lambda *a, **k: _filtered_df(screen)
    )

    if candidates is not None:
        sig_mock = MagicMock(return_value=list(candidates))
    else:
        # default: one candidate per ticker actually passed to the signal func
        sig_mock = MagicMock(side_effect=lambda tickers, *a, **k: [
            _candidate(t) for t in tickers
        ])

    target = "detect_signals_ma_cross" if strategy == "ma_cross" else "detect_signals"
    other = "detect_signals" if strategy == "ma_cross" else "detect_signals_ma_cross"
    monkeypatch.setattr(signal_engine, target, sig_mock)
    monkeypatch.setattr(signal_engine, other, MagicMock(return_value=[]))

    return pipeline, repo, notifier, sig_mock


def _idle_portfolio(cash: str = "200000", positions=(), pending=()) -> PortfolioState:
    return PortfolioState(
        state=SystemState.IDLE,
        cash_jpy=Decimal(cash),
        positions=tuple(positions),
        pending_signals=tuple(pending),
    )


# ── A. Configuration / state ─────────────────────────────────────────────────

class TestIdlePipelineSlotsAndState:
    def test_no_open_slots_returns_unchanged(self, monkeypatch):
        """in_use_slots >= max_positions → return portfolio, no pending saved."""
        pipeline, repo, notifier, _ = _setup(
            monkeypatch, universe=("A.T", "B.T"), max_positions=2
        )
        # Two positions already held (different tickers so candidates survive
        # screening) → in_use_slots == max_positions → open_slots == 0.
        portfolio = _idle_portfolio(
            positions=(_position("X.T"), _position("Y.T"))
        )
        result = pipeline.run(portfolio, "run1")
        assert result is portfolio
        repo.save_pending_signals.assert_not_called()
        repo.save_portfolio.assert_not_called()

    def test_abort_validation_raises(self, monkeypatch):
        """A ticker flagged ABORT_RUN by the validator aborts the whole run."""
        pipeline, _, _, _ = _setup(
            monkeypatch, universe=("A.T", "B.T"), validator=_ok_validator(abort=True)
        )
        with pytest.raises(RuntimeError, match="Abort-level validation failure"):
            pipeline.run(_idle_portfolio(), "run1")

    def test_empty_screen_emits_no_signal(self, monkeypatch):
        """Zero candidates after fundamental screening → format_no_signal path."""
        pipeline, repo, notifier, sig_mock = _setup(
            monkeypatch, universe=("A.T", "B.T"), screen_tickers=[]
        )
        portfolio = _idle_portfolio()
        result = pipeline.run(portfolio, "run1")
        assert result is portfolio
        # signal detection never reached, no pending persisted
        sig_mock.assert_not_called()
        repo.save_pending_signals.assert_not_called()
        # a no-signal notification was sent (2-positional-arg send with blocks)
        assert any(len(c.args) >= 2 for c in notifier.send.call_args_list)

    def test_held_positions_excluded_from_signals(self, monkeypatch):
        """exclude_held_positions keeps held tickers out of signal detection."""
        pipeline, _, _, sig_mock = _setup(
            monkeypatch,
            universe=("A.T", "B.T", "C.T"),
            screen_tickers=["A.T", "B.T", "C.T"],
            max_positions=3,
            # A.T is held → must be dropped before signal detection
            candidates=None,
        )
        portfolio = _idle_portfolio(positions=(_position("A.T"),))
        pipeline.run(portfolio, "run1")
        passed = sig_mock.call_args[0][0]
        assert "A.T" not in passed
        assert set(passed) == {"B.T", "C.T"}

    def test_freshness_check_skipped_when_bypass_trading_day(self, monkeypatch):
        """bypass_trading_day_check=True → assert_universe_fresh is NOT called.

        Reason: a non-trading day legitimately has no fresh OHLCV for `today`,
        so the third built-in skip (DataValidator.assert_universe_fresh) must
        be lifted in tandem with the trading-day guard. Without this, a
        weekend workflow_dispatch with both bypasses on still aborts on
        "Only 0/N tickers have fresh data".
        """
        monkeypatch.setenv("BYPASS_TRADING_DAY_CHECK", "true")
        validator = _ok_validator()
        pipeline, _, _, _ = _setup(
            monkeypatch,
            universe=("A.T", "B.T"),
            screen_tickers=[],  # short-circuit after the freshness step
            validator=validator,
        )
        pipeline.run(_idle_portfolio(), "run1")
        validator.assert_universe_fresh.assert_not_called()

    def test_freshness_check_runs_when_bypass_off(self, monkeypatch):
        """Regression: with bypass off, assert_universe_fresh is still called."""
        validator = _ok_validator()
        pipeline, _, _, _ = _setup(
            monkeypatch,
            universe=("A.T", "B.T"),
            screen_tickers=[],
            validator=validator,
        )
        pipeline.run(_idle_portfolio(), "run1")
        validator.assert_universe_fresh.assert_called_once()


# ── B. Dynamic budget + multiple pendings (Decimal path) ─────────────────────

class TestIdlePipelineDynamicBudget:
    def test_two_slots_split_budget_and_append(self, monkeypatch):
        """open_slots=2, cash ¥200k → 2 new pendings appended to existing one."""
        pipeline, repo, _, _ = _setup(
            monkeypatch, universe=("A.T", "B.T"), max_positions=3
        )
        # One pending already in flight (Z.T) → in_use_slots=1, open_slots=2.
        portfolio = _idle_portfolio(cash="200000", pending=(_pending("Z.T"),))
        result = pipeline.run(portfolio, "run1")

        assert result.state == SystemState.SIGNAL_SENT
        assert len(result.pending_signals) == 3
        tickers = {p.signal.ticker for p in result.pending_signals}
        assert tickers == {"Z.T", "A.T", "B.T"}
        # persisted exactly once with the full merged tuple
        repo.save_pending_signals.assert_called_once()
        saved = repo.save_pending_signals.call_args[0][0]
        assert len(saved) == 3

    def test_all_candidates_unaffordable_returns_unchanged(self, monkeypatch):
        """Every candidate too pricey → InsufficientCapital skip → no pendings."""
        pipeline, repo, notifier, _ = _setup(
            monkeypatch,
            universe=("A.T", "B.T"),
            max_positions=2,
            # ¥50k cash split over 2 slots = ¥25k/slot; ¥2,000 stocks need ≥¥200k
            candidates=[_candidate("A.T", close=2000.0),
                        _candidate("B.T", close=2000.0)],
        )
        portfolio = _idle_portfolio(cash="50000")
        result = pipeline.run(portfolio, "run1")

        assert result is portfolio
        repo.save_pending_signals.assert_not_called()
        # no combined BUY summary was sent (no 2-arg send carrying blocks)
        assert not any(len(c.args) >= 2 for c in notifier.send.call_args_list)


# ── C. signal_strategy switching ─────────────────────────────────────────────

class TestIdlePipelineStrategySwitch:
    def test_ma_cross_strategy(self, monkeypatch):
        pipeline, _, _, sig_mock = _setup(
            monkeypatch, universe=("A.T",), strategy="ma_cross", max_positions=1
        )
        result = pipeline.run(_idle_portfolio(), "run1")
        sig_mock.assert_called_once()
        reason = result.pending_signals[0].signal.reason
        assert "5×25日MAクロス" in reason

    def test_pullback_strategy(self, monkeypatch):
        pipeline, _, _, sig_mock = _setup(
            monkeypatch, universe=("A.T",), strategy="pullback", max_positions=1
        )
        result = pipeline.run(_idle_portfolio(), "run1")
        sig_mock.assert_called_once()
        reason = result.pending_signals[0].signal.reason
        assert "RSI=" in reason


# ── D. Notification aggregation ──────────────────────────────────────────────

class TestIdlePipelineNotification:
    def test_multiple_signals_single_summary_send(self, monkeypatch):
        """N signals → exactly one combined summary send (no per-ticker spam)."""
        pipeline, _, notifier, _ = _setup(
            monkeypatch, universe=("A.T", "B.T"), max_positions=2
        )
        pipeline.run(_idle_portfolio(cash="200000"), "run1")
        # The buy summary is the only send carrying Slack blocks (2 pos. args).
        summary_sends = [c for c in notifier.send.call_args_list if len(c.args) >= 2]
        assert len(summary_sends) == 1
