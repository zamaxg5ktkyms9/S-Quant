"""Tests for safe SystemState parsing in SheetsStateRepository."""

from unittest.mock import MagicMock

import pytest

from squant.domain.enums import SystemState
from squant.infrastructure.sheets_repository import SheetsStateRepository

_HEADER = [
    "state", "cash_jpy", "ticker", "shares",
    "entry_price", "intended_entry_price", "entry_date",
    "stop_loss_price", "trailing_stop_price", "highest_price_since_entry",
    "time_stop_date", "settle_date", "last_run_id", "cumulative_pnl_jpy",
]


def _repo_with_state(state_str: str) -> SheetsStateRepository:
    client = MagicMock()
    client.read_all.return_value = [
        _HEADER,
        [state_str, "100000", "", "", "", "", "", "", "", "", "", "", "run1", "0"],
    ]
    return SheetsStateRepository(client)


class TestSafeStateLoading:
    def test_idle_state_parsed_correctly(self):
        repo = _repo_with_state("IDLE")
        assert repo.load_portfolio().state == SystemState.IDLE

    def test_holding_state_parsed_correctly(self):
        repo = _repo_with_state("HOLDING")
        assert repo.load_portfolio().state == SystemState.HOLDING

    def test_signal_sent_state_parsed_correctly(self):
        repo = _repo_with_state("SIGNAL_SENT")
        assert repo.load_portfolio().state == SystemState.SIGNAL_SENT

    def test_settling_state_parsed_correctly(self):
        repo = _repo_with_state("SETTLING")
        assert repo.load_portfolio().state == SystemState.SETTLING

    def test_unknown_state_defaults_to_idle(self):
        repo = _repo_with_state("UNKNOWN_GARBAGE")
        assert repo.load_portfolio().state == SystemState.IDLE

    def test_empty_state_defaults_to_idle(self):
        repo = _repo_with_state("")
        assert repo.load_portfolio().state == SystemState.IDLE

    def test_typo_state_defaults_to_idle(self):
        repo = _repo_with_state("holding")  # lowercase — not in StrEnum
        assert repo.load_portfolio().state == SystemState.IDLE

    def test_special_chars_state_defaults_to_idle(self):
        repo = _repo_with_state("!@#BROKEN!@#")
        assert repo.load_portfolio().state == SystemState.IDLE

    def test_empty_sheet_returns_idle_portfolio(self):
        """Sheet with no data row → safe IDLE default."""
        client = MagicMock()
        client.read_all.return_value = [_HEADER]
        repo = SheetsStateRepository(client)
        portfolio = repo.load_portfolio()
        assert portfolio.state == SystemState.IDLE
