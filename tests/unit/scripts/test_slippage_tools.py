"""Unit tests for A-3 CLI helpers: confirm_exit / slippage_report."""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

import pytest
from confirm_exit import ConfirmationError, find_latest_sell, validate_exit
from slippage_report import build_report

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 17, 21, 0, tzinfo=JST)


def _sell_row(
    ticker: str = "2201.T",
    price: str = "2650",
    executed_at: str = "2026-07-16T20:35:00+09:00",
    run_id: str = "abc12345",
) -> list[str]:
    return [run_id, ticker, "SELL", "100", price, executed_at, "-6750", "TIME_STOP"]


def _slip_row(
    d: str = "2026-07-13",
    ticker: str = "2201.T",
    side: str = "BUY",
    bps: str = "-58.5",
    jpy: str = "-1600",
) -> dict[str, str]:
    return {
        "date": d, "ticker": ticker, "side": side,
        "intended_price": "2733.5", "actual_price": "2717.5",
        "shares": "100", "slippage_bps": bps, "slippage_jpy": jpy,
        "run_id": "abc12345", "note": "",
    }


# ── confirm_exit.find_latest_sell ─────────────────────────────────────────────

class TestFindLatestSell:
    def test_finds_sell(self):
        trade = find_latest_sell([_sell_row()], "2201.T")
        assert trade["price"] == "2650"
        assert trade["exit_reason"] == "TIME_STOP"

    def test_bare_code_matches(self):
        trade = find_latest_sell([_sell_row()], "2201")
        assert trade["ticker"] == "2201.T"

    def test_picks_latest_of_multiple(self):
        rows = [
            _sell_row(price="2600", executed_at="2026-07-01T20:35:00+09:00"),
            _sell_row(price="2650", executed_at="2026-07-16T20:35:00+09:00"),
        ]
        assert find_latest_sell(rows, "2201.T")["price"] == "2650"

    def test_buy_rows_ignored(self):
        buy = ["r1", "2201.T", "BUY", "100", "2717.5", "2026-07-13T20:35:00+09:00", "", ""]
        with pytest.raises(ConfirmationError, match="見つかりません"):
            find_latest_sell([buy], "2201.T")

    def test_no_rows_raises(self):
        with pytest.raises(ConfirmationError, match="見つかりません"):
            find_latest_sell([], "2201.T")


# ── confirm_exit.validate_exit ────────────────────────────────────────────────

class TestValidateExit:
    def test_clean_sale(self):
        assert validate_exit(find_latest_sell([_sell_row()], "2201.T"),
                             Decimal("2655"), NOW, force=False) == []

    def test_zero_price_rejected(self):
        with pytest.raises(ConfirmationError, match="価格が不正"):
            validate_exit(find_latest_sell([_sell_row()], "2201.T"),
                          Decimal("0"), NOW, force=False)

    def test_fat_finger_rejected_without_force(self):
        with pytest.raises(ConfirmationError, match="乖離"):
            validate_exit(find_latest_sell([_sell_row()], "2201.T"),
                          Decimal("26500"), NOW, force=False)

    def test_fat_finger_downgraded_with_force(self):
        warnings = validate_exit(find_latest_sell([_sell_row()], "2201.T"),
                                 Decimal("26500"), NOW, force=True)
        assert any("乖離" in w for w in warnings)

    def test_stale_trade_rejected_without_force(self):
        old = _sell_row(executed_at="2026-06-01T20:35:00+09:00")
        with pytest.raises(ConfirmationError, match="日前"):
            validate_exit(find_latest_sell([old], "2201.T"),
                          Decimal("2655"), NOW, force=False)


# ── slippage_report.build_report ──────────────────────────────────────────────

class TestBuildReport:
    def test_no_rows_returns_none(self):
        assert build_report([], date(2026, 7, 10), date(2026, 7, 17)) is None

    def test_old_rows_only_returns_none(self):
        rows = [_slip_row(d="2026-06-01")]
        assert build_report(rows, date(2026, 7, 10), date(2026, 7, 17)) is None

    def test_recent_row_included(self):
        report = build_report([_slip_row()], date(2026, 7, 10), date(2026, 7, 17))
        assert report is not None
        assert "2201.T" in report
        assert "-58.5bps" in report

    def test_aggregates_both_sides(self):
        rows = [
            _slip_row(side="BUY", bps="-58.5", jpy="-1600"),
            _slip_row(d="2026-07-16", side="SELL", bps="20.0", jpy="530"),
        ]
        report = build_report(rows, date(2026, 7, 10), date(2026, 7, 17))
        assert "今週 BUY: 1件" in report
        assert "今週 SELL: 1件" in report

    def test_cumulative_includes_old_rows(self):
        rows = [
            _slip_row(d="2026-06-01", bps="10.0", jpy="300"),
            _slip_row(d="2026-07-13", bps="30.0", jpy="900"),
        ]
        report = build_report(rows, date(2026, 7, 10), date(2026, 7, 17))
        assert "累積 BUY: 2件" in report
        assert "今週 BUY: 1件" in report
