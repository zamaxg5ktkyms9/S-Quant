"""SETTLING state pipeline: check T+2 unlock and transition to IDLE."""

from squant.config.settings import Settings
from squant.domain.enums import SystemState
from squant.domain.models import PortfolioState
from squant.infrastructure.interfaces import IClock, INotifier, IStateRepository
from squant.presentation.slack_formatter import format_settling
from squant.utils.jst import is_settlement_unlocked
from squant.utils.logging import get_logger

logger = get_logger(__name__)


class SettlingPipeline:
    def __init__(
        self,
        state_repo: IStateRepository,
        notifier: INotifier,
        clock: IClock,
        settings: Settings,
    ) -> None:
        self._repo = state_repo
        self._notifier = notifier
        self._clock = clock
        self._settings = settings

    def run(self, portfolio: PortfolioState, run_id: str) -> PortfolioState:
        """Process each pending settlement independently.

        Multi-position support (2026-05-30): settle_dates is a tuple. Each
        date is checked, unlocked ones are removed, and the state is
        recomputed based on what remains (positions vs settle_dates).
        """
        today = self._clock.today_jst()

        if not portfolio.settle_dates:
            logger.error("SETTLING state but no settle_dates — resetting to IDLE")
            new_portfolio = PortfolioState(
                state=SystemState.IDLE,
                cash_jpy=portfolio.cash_jpy,
                positions=portfolio.positions,
                pending_signals=portfolio.pending_signals,
                last_run_id=run_id,
                cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            )
            if not self._settings.dry_run:
                self._repo.save_portfolio(new_portfolio)
            return new_portfolio

        # Per-settle-date evaluation: keep only those still locked
        still_locked: list = []
        unlocked: list = []
        for d in portfolio.settle_dates:
            if is_settlement_unlocked(d, today):
                unlocked.append(d)
            else:
                still_locked.append(d)

        for d in unlocked:
            logger.info(f"T+2 settlement unlocked: settle_date={d}, today={today}")

        # Determine new state based on what remains
        if still_locked:
            # Some still settling; if no positions either, stay SETTLING
            new_state = SystemState.HOLDING if portfolio.positions else SystemState.SETTLING
        else:
            new_state = SystemState.HOLDING if portfolio.positions else SystemState.IDLE

        new_portfolio = PortfolioState(
            state=new_state,
            cash_jpy=portfolio.cash_jpy,
            positions=portfolio.positions,
            pending_signals=portfolio.pending_signals,
            settle_dates=tuple(still_locked),
            last_run_id=run_id,
            cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
        )

        if not self._settings.dry_run and (unlocked or new_state != portfolio.state):
            self._repo.save_portfolio(new_portfolio)

        if unlocked and not still_locked:
            text = "[S-Quant] T+2受渡完了 — 全銘柄分の資金解放（IDLEへ）"
            self._notifier.send(text)
        elif unlocked:
            text = (
                f"[S-Quant] T+2受渡（部分） — {len(unlocked)}件解放、"
                f"残{len(still_locked)}件未決済"
            )
            self._notifier.send(text)
        elif still_locked:
            settle_str = still_locked[0].strftime("%Y-%m-%d")
            text, blocks = format_settling("（前回売却銘柄）", settle_str)
            self._notifier.send(text, blocks)
            logger.info(f"Still settling: next unlock on {settle_str} ({len(still_locked)} dates)")

        return new_portfolio
