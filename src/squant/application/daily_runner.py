"""Daily runner — state machine dispatcher and top-level error boundary."""

import dataclasses
import traceback
import uuid
from dataclasses import dataclass
from datetime import date

from squant.config.constants import EXECUTION_GUARD_HOUR_JST
from squant.config.settings import Settings
from squant.domain import circuit_breaker as cb_module
from squant.domain.enums import SystemState
from squant.domain.exceptions import DataQualityError, SQuantError
from squant.domain.models import PortfolioState, RunRecord
from squant.infrastructure.interfaces import IClock, IMarketDataClient, INotifier, IStateRepository
from squant.presentation.slack_formatter import format_circuit_breaker
from squant.utils.jst import is_tse_trading_day
from squant.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RunResult:
    success: bool
    run_id: str
    state_before: SystemState
    state_after: SystemState | None
    note: str = ""


class DailyRunner:
    def __init__(
        self,
        state_repo: IStateRepository,
        market_data: IMarketDataClient,
        notifier: INotifier,
        clock: IClock,
        settings: Settings,
        idle_pipeline: "IdlePipeline",          # type: ignore[name-defined]  # noqa: F821
        holding_pipeline: "HoldingPipeline",    # type: ignore[name-defined]  # noqa: F821
        settling_pipeline: "SettlingPipeline",  # type: ignore[name-defined]  # noqa: F821
    ) -> None:
        self._repo = state_repo
        self._data = market_data
        self._notifier = notifier
        self._clock = clock
        self._settings = settings
        self._idle = idle_pipeline
        self._holding = holding_pipeline
        self._settling = settling_pipeline

    def run(self) -> RunResult:
        run_id = str(uuid.uuid4())[:8]
        today = self._clock.today_jst()
        state_before: SystemState | None = None

        try:
            # 20:00 JST execution guard — GitHub Actions runs at 20:30 JST; local runs before
            # market data is available are rejected here. Bypass available via Settings for
            # workflow_dispatch testing (env BYPASS_EXECUTION_TIME_GUARD=true).
            # On skip we send a one-liner Slack so the operator can tell "the job ran and
            # the guard fired" from "the job did not run at all" — the latter is invisible
            # otherwise (GitHub schedule lag has actually delayed runs by 4 hours).
            now = self._clock.now_jst()
            if now.hour < EXECUTION_GUARD_HOUR_JST:
                if self._settings.bypass_execution_time_guard:
                    logger.warning(
                        f"execution_time_guard bypassed via Settings "
                        f"(now={now.strftime('%H:%M')} JST, guard={EXECUTION_GUARD_HOUR_JST}:00)"
                    )
                else:
                    logger.info(
                        f"Execution time guard: {now.strftime('%H:%M')} JST "
                        f"< {EXECUTION_GUARD_HOUR_JST}:00 JST — skipping"
                    )
                    self._notifier.send(
                        f"[S-Quant] ⏰ skip: {now.strftime('%H:%M')} JST "
                        f"< {EXECUTION_GUARD_HOUR_JST}:00 ガード — full run には "
                        "bypass_execution_time_guard=true で workflow_dispatch を"
                    )
                    return RunResult(
                        success=True, run_id=run_id,
                        state_before=SystemState.IDLE, state_after=None,
                        note=f"execution_time_guard: {now.strftime('%H:%M')} JST",
                    )

            # Skip on non-trading days (handles holidays, weekends). Bypass available via
            # Settings for workflow_dispatch testing (env BYPASS_TRADING_DAY_CHECK=true).
            if not is_tse_trading_day(today):
                if self._settings.bypass_trading_day_check:
                    logger.warning(
                        f"trading_day_check bypassed via Settings (today={today})"
                    )
                else:
                    logger.info(f"{today} is not a TSE trading day — skipping")
                    self._notifier.send(
                        f"[S-Quant] 📅 skip: {today} は TSE 非営業日 — "
                        "土日/祝日テストには bypass_trading_day_check=true を"
                    )
                    return RunResult(
                        success=True, run_id=run_id,
                        state_before=SystemState.IDLE, state_after=None,
                        note="non-trading day",
                    )

            # Idempotency guard
            if self._repo.has_run_today(today):
                logger.info(f"Already ran successfully today ({today}) — skipping")
                return RunResult(
                    success=True, run_id=run_id,
                    state_before=SystemState.IDLE, state_after=None,
                    note="already ran today",
                )

            # Check external connectivity
            self._check_prerequisites()

            # Load state
            portfolio = self._repo.load_portfolio()
            state_before = portfolio.state

            # Reconcile state (time-driven, safe even after missed runs)
            portfolio = self._reconcile(portfolio, today, run_id)

            # Circuit breaker check — 新規エントリーのみ停止し、保有ポジションの
            # 出口管理（HOLDING/SETTLING）は継続する（設計どおり。2026-07-10
            # 独立レビュー F-1 対応: 旧実装は dispatch 前に return しており、
            # 発動中はトレーリング更新・タイムストップ通知まで止まっていた）。
            cb_status = self._repo.load_circuit_breaker()
            if cb_module.is_tripped(cb_status):
                if portfolio.state in (SystemState.HOLDING, SystemState.SETTLING):
                    logger.warning(
                        "Circuit breaker tripped — new entries halted; "
                        "continuing exit management for held positions"
                    )
                    text, blocks = format_circuit_breaker(exit_management_active=True)
                    self._notifier.send(text, blocks)
                    # fall through to dispatch（出口評価は実行される）
                else:
                    # IDLE / SIGNAL_SENT: 新規シグナル生成・約定確認を停止。
                    # 未消化 pending が残っていればキャンセルして発注を防ぐ。
                    if portfolio.pending_signals and not self._settings.dry_run:
                        self._repo.cancel_pending_signal(None)
                        logger.warning(
                            "Circuit breaker tripped — cancelled "
                            f"{len(portfolio.pending_signals)} pending signal(s)"
                        )
                    logger.warning("Circuit breaker tripped — halting new entries")
                    text, blocks = format_circuit_breaker()
                    self._notifier.send(text, blocks)
                    self._repo.mark_run_complete(
                        RunRecord(run_id=run_id, run_date=today, status="success", note="cb_tripped")
                    )
                    return RunResult(
                        success=True, run_id=run_id,
                        state_before=state_before, state_after=portfolio.state,
                        note="circuit_breaker_tripped",
                    )

            # Dispatch to state-appropriate pipeline
            logger.info(f"State: {portfolio.state.value} | run_id={run_id}")
            new_portfolio = self._dispatch(portfolio, run_id)

            # Persist final state — always save so the portfolio sheet stays current
            # (idle no-op runs would otherwise leave the sheet permanently empty)
            if not self._settings.dry_run:
                self._repo.save_portfolio(
                    dataclasses.replace(new_portfolio, last_run_id=run_id)
                )
                self._repo.mark_run_complete(
                    RunRecord(run_id=run_id, run_date=today, status="success")
                )

            return RunResult(
                success=True, run_id=run_id,
                state_before=state_before, state_after=new_portfolio.state,
            )

        except DataQualityError as e:
            logger.error(f"Data quality abort: {e}")
            self._notifier.send_error("データ品質エラー — 取引中止", str(e))
            self._repo.mark_run_complete(
                RunRecord(run_id=run_id, run_date=today, status="error", note=str(e)[:200])
            )
            return RunResult(
                success=False, run_id=run_id,
                state_before=state_before or SystemState.IDLE, state_after=None,
                note=str(e),
            )

        except SQuantError as e:
            logger.error(f"S-Quant error: {e}")
            self._notifier.send_error("システムエラー", str(e))
            self._repo.mark_run_complete(
                RunRecord(run_id=run_id, run_date=today, status="error", note=str(e)[:200])
            )
            return RunResult(
                success=False, run_id=run_id,
                state_before=state_before or SystemState.IDLE, state_after=None,
                note=str(e),
            )

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Unexpected error: {e}\n{tb}")
            self._notifier.send_error("予期しないエラー", f"{type(e).__name__}: {e}\n\n{tb[:800]}")
            import contextlib
            with contextlib.suppress(Exception):
                self._repo.mark_run_complete(
                    RunRecord(run_id=run_id, run_date=today, status="error", note=str(e)[:200])
                )
            return RunResult(
                success=False, run_id=run_id,
                state_before=state_before or SystemState.IDLE, state_after=None,
                note=str(e),
            )

    def _check_prerequisites(self) -> None:
        """Abort early if external services are unreachable."""
        from squant.domain.exceptions import SheetsError

        if hasattr(self._repo, "_c") and hasattr(self._repo._c, "check_connectivity") and not self._repo._c.check_connectivity():  # type: ignore[union-attr]
            raise SheetsError("Google Sheets is unreachable — cannot read state safely")

        if hasattr(self._data, "check_connectivity") and not self._data.check_connectivity():  # type: ignore[union-attr]
            from squant.domain.exceptions import DataQualityError
            data_name = type(self._data).__name__
            raise DataQualityError(f"{data_name} is unreachable — check API credentials")

    def _reconcile(self, portfolio: PortfolioState, today: date, run_id: str) -> PortfolioState:
        """Apply time-driven state corrections (safe after GHA missed runs)."""
        if portfolio.state == SystemState.SETTLING and portfolio.settle_date:
            from squant.utils.jst import is_settlement_unlocked
            if is_settlement_unlocked(portfolio.settle_date, today):
                logger.info("Reconcile: SETTLING → IDLE (settle_date passed)")
                new = PortfolioState(
                    state=SystemState.IDLE,
                    cash_jpy=portfolio.cash_jpy,
                    last_run_id=run_id,
                    cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
                )
                if not self._settings.dry_run:
                    self._repo.save_portfolio(new)
                return new

        if portfolio.state == SystemState.HOLDING and portfolio.position:
            from squant.utils.jst import count_trading_days
            days = count_trading_days(portfolio.position.entry_date, today)
            if days >= 5:
                logger.warning(f"Reconcile: position {portfolio.position.ticker} hit time-stop during missed run")

        return portfolio

    def _dispatch(self, portfolio: PortfolioState, run_id: str) -> PortfolioState:
        if portfolio.state in (SystemState.IDLE, SystemState.SIGNAL_SENT):
            # SIGNAL_SENT: check operator confirmation across ALL pending signals.
            # After resolving pending signals we always return — the next idle
            # scan happens on the following run, never the same one.
            if portfolio.state == SystemState.SIGNAL_SENT:
                return self._process_pending_signals(portfolio, run_id)

            return self._idle.run(portfolio, run_id)

        elif portfolio.state == SystemState.HOLDING:
            return self._holding.run(portfolio, run_id)

        elif portfolio.state == SystemState.SETTLING:
            return self._settling.run(portfolio, run_id)

        else:
            logger.error(f"Unknown state: {portfolio.state}")
            return portfolio

    def _process_pending_signals(self, portfolio: PortfolioState, run_id: str) -> PortfolioState:
        """Resolve each pending signal: confirm fills, drop cancellations, time-out stale ones.

        Multi-position support: walks every PendingSignal returned by the repo,
        keeping pendings that are still awaiting operator confirmation, appending
        confirmed Positions to portfolio.positions, and ending with the correct
        aggregate state (HOLDING if any position, SIGNAL_SENT if any unresolved,
        IDLE otherwise).
        """
        from squant.domain.enums import ExecutionStatus

        pendings = self._repo.load_pending_signals()
        if not pendings:
            logger.warning("SIGNAL_SENT state but no pending signals in sheet — resetting to IDLE")
            new = PortfolioState(
                state=SystemState.IDLE,
                cash_jpy=portfolio.cash_jpy,
                positions=portfolio.positions,
                pending_signals=(),
                settle_dates=portfolio.settle_dates,
                last_run_id=run_id,
                cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            )
            if not self._settings.dry_run:
                self._repo.save_portfolio(new)
            return new

        today = self._clock.today_jst()
        current = portfolio
        remaining_pendings: list = []

        for pending in pendings:
            if pending.execution_status == ExecutionStatus.FILLED:
                current = self._confirm_entry(current, pending, run_id)
            elif pending.execution_status == ExecutionStatus.CANCELLED:
                logger.info(f"Operator cancelled signal {pending.signal.ticker}")
            elif pending.signal.generated_at.date() < today:
                logger.warning(
                    f"Signal {pending.signal.ticker} timed out — treating as cancelled"
                )
                self._notifier.send(
                    f"[S-Quant] オペレータ応答なし — {pending.signal.ticker} の注文は"
                    f"未確認のため発注なしとみなします"
                )
                if not self._settings.dry_run:
                    self._repo.cancel_pending_signal(pending.signal.ticker)
            else:
                # Still awaiting confirmation, keep
                remaining_pendings.append(pending)

        # Final state recomputation
        if current.positions:
            new_state = SystemState.HOLDING
        elif remaining_pendings:
            new_state = SystemState.SIGNAL_SENT
        elif current.settle_dates:
            new_state = SystemState.SETTLING
        else:
            new_state = SystemState.IDLE

        new = PortfolioState(
            state=new_state,
            cash_jpy=current.cash_jpy,
            positions=current.positions,
            pending_signals=tuple(remaining_pendings),
            settle_dates=current.settle_dates,
            last_run_id=run_id,
            cumulative_pnl_jpy=current.cumulative_pnl_jpy,
        )
        if not self._settings.dry_run and new != portfolio:
            self._repo.save_portfolio(new)
            # Persist the trimmed pending tab (drop confirmed/cancelled/timed-out)
            self._repo.save_pending_signals(tuple(remaining_pendings))
        return new

    def _confirm_entry(
        self, portfolio: PortfolioState, pending: "PendingSignal", run_id: str  # type: ignore[name-defined]  # noqa: F821
    ) -> PortfolioState:
        """Transition SIGNAL_SENT → HOLDING using operator-confirmed fill."""
        from squant.domain.models import Position
        from squant.domain.quantity_calculator import compute_stop_loss_price
        from squant.utils.jst import add_trading_days

        sig = pending.signal
        actual_price = pending.actual_entry_price or sig.reference_price
        actual_shares = pending.actual_shares or sig.shares

        today = self._clock.today_jst()
        time_stop = add_trading_days(today, 5)
        stop_loss = compute_stop_loss_price(actual_price, self._settings.stop_loss_rate)

        position = Position(
            ticker=sig.ticker,
            shares=actual_shares,
            entry_price=actual_price,
            intended_entry_price=sig.reference_price,
            entry_date=today,
            stop_loss_price=stop_loss,
            trailing_stop_price=stop_loss,
            highest_price_since_entry=actual_price,
            time_stop_date=time_stop,
        )

        cost = actual_price * actual_shares
        # Append to existing positions (multi-position support)
        new_positions = portfolio.positions + (position,)
        new_portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=portfolio.cash_jpy - cost,
            positions=new_positions,
            settle_dates=portfolio.settle_dates,
            last_run_id=run_id,
            cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
        )

        if not self._settings.dry_run:
            self._repo.save_portfolio(new_portfolio)

        logger.info(f"Entry confirmed: {sig.ticker} ×{actual_shares} @ ¥{actual_price}")
        self._notifier.send(
            f"[S-Quant] エントリー確認 — {sig.ticker} ×{actual_shares}株 @ ¥{actual_price}\n"
            f"損切ライン: ¥{int(stop_loss)} | タイムストップ: {time_stop}"
        )
        return new_portfolio
