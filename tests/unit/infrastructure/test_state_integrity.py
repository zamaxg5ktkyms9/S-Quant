"""Tests for safe SystemState parsing in SheetsStateRepository."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from squant.domain.enums import SystemState
from squant.domain.models import PortfolioState, Position
from squant.infrastructure.sheets_repository import (
    SheetsStateRepository,
    _deserialize_positions,
    _deserialize_settle_dates,
    _serialize_positions,
    _serialize_settle_dates,
)

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


# ── Multi-position persistence (2026-05-30) ────────────────────────────────

def _make_position(ticker: str, shares: int = 100, price: int = 500) -> Position:
    return Position(
        ticker=ticker,
        shares=shares,
        entry_price=Decimal(price),
        intended_entry_price=Decimal(price),
        entry_date=date(2026, 5, 20),
        stop_loss_price=Decimal(int(price * 0.975)),
        trailing_stop_price=Decimal(int(price * 0.975)),
        highest_price_since_entry=Decimal(price),
        time_stop_date=date(2026, 5, 27),
    )


class TestPositionsSerialization:
    def test_empty_tuple_roundtrip(self):
        assert _serialize_positions(()) == ""
        assert _deserialize_positions("") == ()

    def test_single_position_roundtrip(self):
        positions = (_make_position("7203.T"),)
        wire = _serialize_positions(positions)
        back = _deserialize_positions(wire)
        assert back == positions

    def test_multi_position_roundtrip(self):
        positions = (_make_position("7203.T"), _make_position("6758.T", price=2500))
        wire = _serialize_positions(positions)
        back = _deserialize_positions(wire)
        assert back == positions

    def test_malformed_json_returns_empty(self):
        assert _deserialize_positions("not json") == ()

    def test_missing_key_skips_entry(self):
        bad = '[{"ticker":"X","shares":1}]'  # missing many required fields
        assert _deserialize_positions(bad) == ()


class TestSettleDatesSerialization:
    def test_empty_roundtrip(self):
        assert _serialize_settle_dates(()) == ""
        assert _deserialize_settle_dates("") == ()

    def test_multi_settle_dates_roundtrip(self):
        dates = (date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22))
        wire = _serialize_settle_dates(dates)
        back = _deserialize_settle_dates(wire)
        assert back == dates

    def test_invalid_date_dropped(self):
        assert _deserialize_settle_dates("2026-05-20, garbage, 2026-05-21") == (
            date(2026, 5, 20),
            date(2026, 5, 21),
        )


class TestMultiPositionPortfolioLoad:
    def test_load_portfolio_with_positions_json(self):
        """When positions_json column is present, it wins over the display columns."""
        positions = (_make_position("7203.T"), _make_position("6758.T", price=2500))
        settle_dates = (date(2026, 5, 27), date(2026, 5, 28))

        new_header = _HEADER + ["positions_json", "settle_dates_csv"]
        row = ["HOLDING", "200000"] + [""] * 12 + [
            _serialize_positions(positions),
            _serialize_settle_dates(settle_dates),
        ]
        client = MagicMock()
        client.read_all.return_value = [new_header, row]
        repo = SheetsStateRepository(client)
        portfolio = repo.load_portfolio()
        assert portfolio.state == SystemState.HOLDING
        assert portfolio.positions == positions
        assert portfolio.settle_dates == settle_dates

    def test_load_portfolio_back_compat_single_column(self):
        """Old sheets without positions_json still work via display columns."""
        client = MagicMock()
        client.read_all.return_value = [
            _HEADER,
            [
                "HOLDING", "200000", "7203.T", "100",
                "500", "500", "2026-05-20",
                "487", "487", "500",
                "2026-05-27", "", "run1", "0",
            ],
        ]
        repo = SheetsStateRepository(client)
        portfolio = repo.load_portfolio()
        assert portfolio.state == SystemState.HOLDING
        assert len(portfolio.positions) == 1
        assert portfolio.positions[0].ticker == "7203.T"

    def test_save_portfolio_writes_positions_json(self):
        """save_portfolio populates positions_json / settle_dates_csv columns."""
        positions = (_make_position("7203.T"), _make_position("6758.T", price=2500))
        client = MagicMock()
        # Sheet already has the correct header + one data row → update_row path
        client.read_all.return_value = [
            _HEADER + ["positions_json", "settle_dates_csv"],
            [""] * 16,
        ]
        repo = SheetsStateRepository(client)
        portfolio = PortfolioState(
            state=SystemState.HOLDING,
            cash_jpy=Decimal("100000"),
            positions=positions,
            settle_dates=(date(2026, 5, 27),),
        )
        repo.save_portfolio(portfolio)
        client.update_row.assert_called_once()
        data = client.update_row.call_args[0][2]
        # Last two columns must round-trip to the same positions / settle_dates
        assert _deserialize_positions(data[-2]) == positions
        assert _deserialize_settle_dates(data[-1]) == (date(2026, 5, 27),)
        # Display columns reflect the first held position
        assert data[2] == "7203.T"
