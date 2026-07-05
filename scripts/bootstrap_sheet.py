"""One-time setup: create Google Sheet tabs with correct headers.

Run once before first live use:
    python scripts/bootstrap_sheet.py --apply

Touches the LIVE spreadsheet. Without --apply it only prints what it would do.
"""

import argparse
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


def bootstrap(apply: bool):
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
            if not apply:
                print(f"  [dry] {tab_name}: would clear and write headers")
                continue
            ws.clear()
            ws.update([header], "A1")
            print(f"  ✓ {tab_name}: headers written")
        else:
            print(f"  — {tab_name}: already initialized")

    # Initialize portfolio tab with IDLE state (budget from settings, not hardcoded)
    from decimal import Decimal

    from squant.domain.enums import SystemState
    from squant.domain.models import PortfolioState
    from squant.infrastructure.sheets_repository import SheetsStateRepository

    budget = Decimal(settings.budget_jpy)
    repo = SheetsStateRepository(client)
    portfolio_rows = client.read_all(SHEET_PORTFOLIO)
    has_portfolio_row = len(portfolio_rows) >= 2 and bool(portfolio_rows[1][0])
    if has_portfolio_row:
        portfolio = repo.load_portfolio()
        print(f"  — portfolio: already initialized "
              f"(state={portfolio.state.value}, cash=¥{portfolio.cash_jpy:,}) — left untouched")
    elif not apply:
        print(f"  [dry] portfolio: would initialize with IDLE / ¥{budget:,}")
    else:
        repo.save_portfolio(PortfolioState(
            state=SystemState.IDLE,
            cash_jpy=budget,
        ))
        print(f"  ✓ portfolio: initialized with IDLE / ¥{budget:,}")

    # Initialize circuit breaker ONLY if the row is empty — never reset live CB state
    from squant.domain.models import CircuitBreakerStatus
    cb_rows = client.read_all(SHEET_CIRCUIT_BREAKER)
    has_cb_row = len(cb_rows) >= 2 and bool(cb_rows[1][0])
    if has_cb_row:
        print("  — circuit_breaker: already initialized — left untouched")
    elif not apply:
        print("  [dry] circuit_breaker: would initialize (is_tripped=False, loss=¥0)")
    else:
        repo.save_circuit_breaker(CircuitBreakerStatus(
            is_tripped=False,
            cumulative_loss_jpy=Decimal("0"),
        ))
        print("  ✓ circuit_breaker: initialized")

    if apply:
        print("\nBootstrap complete. Spreadsheet is ready for live trading.")
    else:
        print("\nDry preview only — rerun with --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Google Sheets の初期タブ・ヘッダーを作成する（本番シートに書き込む一回限りのセットアップ）"
    )
    parser.add_argument("--apply", action="store_true",
                        help="実際に書き込む。指定しない場合はプレビューのみ")
    args = parser.parse_args()
    bootstrap(apply=args.apply)
