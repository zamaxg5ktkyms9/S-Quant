"""Read-only operational status: print live Sheets state for the daily check.

Usage:
    python scripts/ops_status.py            # portfolio / CB / last runs / pending
    python scripts/ops_status.py --runs 10  # show more run_log rows

Reads the LIVE spreadsheet but never writes. Requires GCP_SA_KEY_JSON and
SPREADSHEET_ID in .env.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from squant.config.constants import (
    SHEET_CIRCUIT_BREAKER,
    SHEET_PENDING_SIGNALS,
    SHEET_PORTFOLIO,
    SHEET_RUN_LOG,
)
from squant.config.settings import get_settings
from squant.infrastructure.sheets_client import GoogleSheetsClient


def _print_tab(client: GoogleSheetsClient, tab: str, tail: int | None = None) -> None:
    rows = client.read_all(tab)
    if not rows:
        print(f"  (empty tab: {tab})")
        return
    header, body = rows[0], rows[1:]
    if tail is not None:
        body = body[-tail:]
    print(f"  {header}")
    if not body or (len(body) == 1 and not any(body[0])):
        print("  (no data rows)")
        return
    for r in body:
        print(f"  {r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="S-Quant 運用状態の読み取り専用ダンプ")
    parser.add_argument("--runs", type=int, default=5, help="run_log の表示行数 (default: 5)")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.gcp_sa_key_json or not settings.spreadsheet_id:
        print("ERROR: GCP_SA_KEY_JSON and SPREADSHEET_ID must be set in .env")
        return 1

    client = GoogleSheetsClient(settings.gcp_sa_key_json, settings.spreadsheet_id)

    print("=== portfolio ===")
    _print_tab(client, SHEET_PORTFOLIO)
    print("\n=== circuit_breaker ===")
    _print_tab(client, SHEET_CIRCUIT_BREAKER)
    print(f"\n=== run_log (last {args.runs}) ===")
    _print_tab(client, SHEET_RUN_LOG, tail=args.runs)
    print("\n=== pending_signals ===")
    _print_tab(client, SHEET_PENDING_SIGNALS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
