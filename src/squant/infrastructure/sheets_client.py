"""Low-level Google Sheets wrapper via gspread."""

import json

import gspread
from google.oauth2.service_account import Credentials

from squant.utils.logging import get_logger
from squant.utils.retry import with_retry

logger = get_logger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class GoogleSheetsClient:
    def __init__(self, sa_key_json: str, spreadsheet_id: str) -> None:
        self._spreadsheet_id = spreadsheet_id
        creds = Credentials.from_service_account_info(
            json.loads(sa_key_json), scopes=_SCOPES
        )
        self._gc = gspread.authorize(creds)
        self._sh: gspread.Spreadsheet | None = None

    def _get_spreadsheet(self) -> gspread.Spreadsheet:
        if self._sh is None:
            self._sh = self._gc.open_by_key(self._spreadsheet_id)
        return self._sh

    @with_retry(max_attempts=3, min_wait=1.0, max_wait=10.0)
    def get_or_create_sheet(self, title: str) -> gspread.Worksheet:
        sh = self._get_spreadsheet()
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            logger.info(f"Creating sheet tab: {title}")
            return sh.add_worksheet(title=title, rows=1000, cols=30)

    @with_retry(max_attempts=3, min_wait=1.0, max_wait=10.0)
    def read_all(self, sheet_title: str) -> list[list[str]]:
        ws = self.get_or_create_sheet(sheet_title)
        return ws.get_all_values()

    @with_retry(max_attempts=3, min_wait=1.0, max_wait=10.0)
    def update_row(self, sheet_title: str, row_index: int, values: list) -> None:
        """Update a single row (1-indexed, header is row 1)."""
        ws = self.get_or_create_sheet(sheet_title)
        cell_range = f"A{row_index}"
        ws.update(cell_range, [values])

    @with_retry(max_attempts=3, min_wait=1.0, max_wait=10.0)
    def append_row(self, sheet_title: str, values: list) -> None:
        ws = self.get_or_create_sheet(sheet_title)
        ws.append_row(values, value_input_option="USER_ENTERED")

    @with_retry(max_attempts=3, min_wait=1.0, max_wait=10.0)
    def overwrite_sheet(self, sheet_title: str, rows: list[list]) -> None:
        """Replace all content of a sheet (header + data rows)."""
        ws = self.get_or_create_sheet(sheet_title)
        ws.clear()
        if rows:
            ws.update("A1", rows)

    def check_connectivity(self) -> bool:
        try:
            self._get_spreadsheet()
            return True
        except Exception as e:
            logger.error(f"Sheets connectivity check failed: {e}")
            return False
