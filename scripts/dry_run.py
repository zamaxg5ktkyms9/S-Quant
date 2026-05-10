"""Local dry-run with optional date override.

Usage:
    python scripts/dry_run.py                    # use today's date
    python scripts/dry_run.py --date 2026-04-30  # simulate a past date
    python scripts/dry_run.py --state HOLDING    # override state for testing

The dry run uses real yfinance data but writes nothing to Google Sheets
and sends nothing to Slack (unless --notify flag is passed).
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import os

os.environ.setdefault("DRY_RUN", "true")

from squant.utils.logging import setup_logging

setup_logging("DEBUG")


def parse_args():
    p = argparse.ArgumentParser(description="S-Quant dry run")
    p.add_argument("--date", help="Simulate date (YYYY-MM-DD)", default=None)
    p.add_argument("--notify", action="store_true", help="Send real Slack notifications")
    p.add_argument("--verbose", action="store_true", help="DEBUG logging")
    return p.parse_args()


class FakeClock:
    def __init__(self, sim_date: date):
        self._date = sim_date

    def now_jst(self) -> datetime:
        from squant.utils.jst import JST
        return datetime.combine(self._date, datetime.min.time()).replace(tzinfo=JST)

    def today_jst(self) -> date:
        return self._date


class NoOpNotifier:
    def send(self, text: str, blocks=None) -> None:
        print(f"\n[SLACK] {text}")

    def send_error(self, title: str, detail: str) -> None:
        print(f"\n[SLACK ERROR] {title}: {detail}")


class NoOpStateRepository:
    """Stateless stub — reads nothing, writes nothing."""

    def load_portfolio(self):
        from decimal import Decimal

        from squant.domain.enums import SystemState
        from squant.domain.models import PortfolioState
        return PortfolioState(state=SystemState.IDLE, cash_jpy=Decimal("100000"))

    def save_portfolio(self, state): pass
    def append_trade(self, trade): pass
    def save_pending_signal(self, pending): pass
    def load_pending_signal(self): return None
    def confirm_pending_signal(self, *a, **kw): pass
    def cancel_pending_signal(self): pass
    def load_circuit_breaker(self):
        from decimal import Decimal

        from squant.domain.models import CircuitBreakerStatus
        return CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
    def save_circuit_breaker(self, status): pass
    def load_recent_sales(self): return []
    def append_recent_sale(self, sale): pass
    def has_run_today(self, today): return False
    def mark_run_complete(self, record): pass


def main():
    args = parse_args()
    if args.verbose:
        setup_logging("DEBUG")

    sim_date = date.fromisoformat(args.date) if args.date else date.today()
    print(f"\nS-Quant DRY RUN — simulating {sim_date}\n{'='*50}")

    from squant.application.daily_runner import DailyRunner
    from squant.application.pipelines.holding_pipeline import HoldingPipeline
    from squant.application.pipelines.idle_pipeline import IdlePipeline
    from squant.application.pipelines.settling_pipeline import SettlingPipeline
    from squant.application.universe_loader import load_earnings_blackouts, load_universe
    from squant.config.settings import get_settings
    from squant.infrastructure.data_validator import DataValidator
    from squant.infrastructure.yfinance_client import YFinanceClient

    class _DryRunYFinanceClient(YFinanceClient):
        """Skip canary check to avoid wasting a rate-limit slot during dry runs."""
        def check_connectivity(self) -> bool:
            return True

    settings = get_settings()
    clock = FakeClock(sim_date)
    notifier = NoOpNotifier() if not args.notify else None
    state_repo = NoOpStateRepository()
    validator = DataValidator()
    market_data = _DryRunYFinanceClient(validator=validator)

    if notifier is None:
        from squant.infrastructure.slack_notifier import SlackNotifier
        notifier = SlackNotifier(settings.slack_webhook_url)

    universe = load_universe()
    blackouts = load_earnings_blackouts()

    idle = IdlePipeline(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        validator=validator,
        clock=clock,
        settings=settings,
        universe=universe[:15],  # limit universe for speed in dry run
        blackouts=blackouts,
    )
    holding = HoldingPipeline(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        validator=validator,
        clock=clock,
        settings=settings,
    )
    settling = SettlingPipeline(
        state_repo=state_repo,
        notifier=notifier,
        clock=clock,
        settings=settings,
    )
    runner = DailyRunner(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        clock=clock,
        settings=settings,
        idle_pipeline=idle,
        holding_pipeline=holding,
        settling_pipeline=settling,
    )

    result = runner.run()
    print(f"\nResult: {'OK' if result.success else 'FAILED'} | {result.note}")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
