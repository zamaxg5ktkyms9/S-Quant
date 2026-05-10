"""One-time setup: create Google Sheet tabs with correct headers.

Run once before first live use:
    python scripts/bootstrap_sheet.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from squant.config.constants import (
    SHEET_CIRCUIT_BREAKER,
    SHEET_PENDING_SIGNALS,
    SHEET_PORTFOLIO,
    SHEET_RECENT_SALES,
    SHEET_RUN_LOG,
    SHEET_TRADES,
)
from squant.config.settings import get_settings
from squant.infrastructure.sheets_client import GoogleSheetsClient
from squant.infrastructure.sheets_repository import (
    _CB_HEADER,
    _PENDING_HEADER,
    _PORTFOLIO_HEADER,
    _RECENT_SALES_HEADER,
    _RUN_LOG_HEADER,
    _TRADES_HEADER,
)


def bootstrap():
    settings = get_settings()

    if not settings.gcp_sa_key_json or not settings.spreadsheet_id:
        print("ERROR: GCP_SA_KEY_JSON and SPREADSHEET_ID must be set in .env")
        sys.exit(1)

    client = GoogleSheetsClient(settings.gcp_sa_key_json, settings.spreadsheet_id)

    tabs = [
        (SHEET_PORTFOLIO, _PORTFOLIO_HEADER),
        (SHEET_TRADES, _TRADES_HEADER),
        (SHEET_CIRCUIT_BREAKER, _CB_HEADER),
        (SHEET_RUN_LOG, _RUN_LOG_HEADER),
        (SHEET_PENDING_SIGNALS, _PENDING_HEADER),
        (SHEET_RECENT_SALES, _RECENT_SALES_HEADER),
    ]

    for tab_name, header in tabs:
        ws = client.get_or_create_sheet(tab_name)
        existing = ws.get_all_values()
        if not existing or existing[0] != header:
            ws.clear()
            ws.update([header], "A1")
            print(f"  ✓ {tab_name}: headers written")
        else:
            print(f"  — {tab_name}: already initialized")

    # Initialize portfolio tab with IDLE state
    from decimal import Decimal

    from squant.domain.enums import SystemState
    from squant.domain.models import PortfolioState
    from squant.infrastructure.sheets_repository import SheetsStateRepository

    repo = SheetsStateRepository(client)
    portfolio = repo.load_portfolio()
    if portfolio.state == SystemState.IDLE and portfolio.cash_jpy == Decimal("100000"):
        print("  — portfolio: already initialized")
    else:
        repo.save_portfolio(PortfolioState(
            state=SystemState.IDLE,
            cash_jpy=Decimal("100000"),
        ))
        print("  ✓ portfolio: initialized with IDLE / ¥100,000")

    # Initialize circuit breaker
    from squant.domain.models import CircuitBreakerStatus
    repo.save_circuit_breaker(CircuitBreakerStatus(
        is_tripped=False,
        cumulative_loss_jpy=Decimal("0"),
    ))
    print("  ✓ circuit_breaker: initialized")

    print("\nBootstrap complete. Spreadsheet is ready for live trading.")


if __name__ == "__main__":
    bootstrap()
