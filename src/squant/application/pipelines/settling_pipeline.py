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
        today = self._clock.today_jst()

        if portfolio.settle_date is None:
            logger.error("SETTLING state but no settle_date — resetting to IDLE")
            new_portfolio = PortfolioState(
                state=SystemState.IDLE,
                cash_jpy=portfolio.cash_jpy,
                last_run_id=run_id,
                cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            )
            if not self._settings.dry_run:
                self._repo.save_portfolio(new_portfolio)
            return new_portfolio

        # Time-driven: check if settlement date has been reached
        if is_settlement_unlocked(portfolio.settle_date, today):
            logger.info(
                f"T+2 settlement unlocked: settle_date={portfolio.settle_date}, today={today}"
            )
            new_portfolio = PortfolioState(
                state=SystemState.IDLE,
                cash_jpy=portfolio.cash_jpy,
                last_run_id=run_id,
                cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            )
            if not self._settings.dry_run:
                self._repo.save_portfolio(new_portfolio)

            text = "[S-Quant] T+2受渡完了 — IDLE状態に戻りました（資金解放）"
            self._notifier.send(text)
            return new_portfolio

        # Still settling — notify remaining wait
        settle_str = portfolio.settle_date.strftime("%Y-%m-%d")
        text, blocks = format_settling("（前回売却銘柄）", settle_str)
        self._notifier.send(text, blocks)
        logger.info(f"Still settling, unlock on {settle_str}")
        return portfolio
