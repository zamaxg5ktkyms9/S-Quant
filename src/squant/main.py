"""Composition root — wire concrete adapters and run the daily pipeline."""

import sys

from squant.application.daily_runner import DailyRunner
from squant.application.pipelines.holding_pipeline import HoldingPipeline
from squant.application.pipelines.idle_pipeline import IdlePipeline
from squant.application.pipelines.settling_pipeline import SettlingPipeline
from squant.application.universe_loader import load_earnings_blackouts, load_universe
from squant.config.settings import get_settings
from squant.infrastructure.clock import SystemClock
from squant.infrastructure.data_validator import DataValidator
from squant.infrastructure.jquants_client import JQuantsClient
from squant.infrastructure.sheets_client import GoogleSheetsClient
from squant.infrastructure.sheets_repository import SheetsStateRepository
from squant.infrastructure.slack_notifier import SlackNotifier
from squant.infrastructure.yfinance_client import YFinanceClient
from squant.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)


def main() -> int:
    settings = get_settings()

    if settings.dry_run:
        logger.info("=== DRY RUN MODE — no writes to Sheets, no actual signals ===")

    clock = SystemClock()
    validator = DataValidator()

    if settings.jquants_api_key:
        logger.info(f"Using J-Quants v2 as market data source (rpm={settings.jquants_rpm})")
        market_data = JQuantsClient(
            api_key=settings.jquants_api_key,
            requests_per_minute=settings.jquants_rpm,
        )
    else:
        logger.warning("JQUANTS_API_KEY not set — falling back to yfinance (unreliable on cloud)")
        market_data = YFinanceClient(validator=validator)

    if not settings.gcp_sa_key_json or not settings.spreadsheet_id:
        logger.error("GCP_SA_KEY_JSON or SPREADSHEET_ID is not set — cannot connect to Sheets")
        return 1

    sheets_client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    state_repo = SheetsStateRepository(sheets_client)
    notifier = SlackNotifier(settings.slack_webhook_url, dry_run=settings.dry_run)

    universe = load_universe()
    blackouts = load_earnings_blackouts(as_of=clock.today_jst())
    logger.info(f"Universe: {len(universe)} tickers | Blackouts: {len(blackouts)} events")

    idle_pipeline = IdlePipeline(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        validator=validator,
        clock=clock,
        settings=settings,
        universe=universe,
        blackouts=blackouts,
    )
    holding_pipeline = HoldingPipeline(
        state_repo=state_repo,
        market_data=market_data,
        notifier=notifier,
        validator=validator,
        clock=clock,
        settings=settings,
    )
    settling_pipeline = SettlingPipeline(
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
        idle_pipeline=idle_pipeline,
        holding_pipeline=holding_pipeline,
        settling_pipeline=settling_pipeline,
    )

    result = runner.run()

    if result.success:
        logger.info(
            f"Run complete | id={result.run_id} "
            f"state={result.state_before} → {result.state_after} | {result.note}"
        )
        return 0
    else:
        logger.error(f"Run failed | id={result.run_id} | {result.note}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
