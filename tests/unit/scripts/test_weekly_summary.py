"""Unit tests for A-4 weekly summary helpers."""

import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from weekly_summary import build_summary, trades_in_week, weekly_returns

TODAY = date(2026, 7, 18)
SINCE = date(2026, 7, 11)


def _sell_row(executed_at: str, pnl: str = "-6750") -> list[str]:
    return ["abc12345", "2201.T", "SELL", "100", "2650", executed_at, pnl, "TIME_STOP"]


def _snapshot(
    d: str = "2026-07-11",
    equity: str = "600000",
    topix: str = "420.0",
    note: str = "",
) -> dict[str, str]:
    return {
        "date": d, "equity_jpy": equity, "topix_close": topix,
        "cumulative_pnl_jpy": "0", "cb_net_loss_jpy": "0", "note": note,
    }


# ── trades_in_week ─────────────────────────────────────────────────────────────

class TestTradesInWeek:
    def test_recent_trade_included(self):
        rows = [_sell_row("2026-07-16T20:35:00+09:00")]
        assert len(trades_in_week(rows, SINCE)) == 1

    def test_old_trade_excluded(self):
        rows = [_sell_row("2026-07-01T20:35:00+09:00")]
        assert trades_in_week(rows, SINCE) == []

    def test_boundary_date_included(self):
        rows = [_sell_row("2026-07-11T20:35:00+09:00")]
        assert len(trades_in_week(rows, SINCE)) == 1

    def test_short_rows_skipped(self):
        assert trades_in_week([[], ["x"]], SINCE) == []


# ── weekly_returns ─────────────────────────────────────────────────────────────

class TestWeeklyReturns:
    def test_no_snapshots_returns_none(self):
        assert weekly_returns([], Decimal("600000"), Decimal("420")) is None

    def test_computes_both_returns(self):
        # equity 600000→612000 = +2%, topix 420→424.2 = +1%
        port, tpx = weekly_returns(
            [_snapshot()], Decimal("612000"), Decimal("424.2")
        )
        assert port == Decimal("2.00")
        assert tpx == Decimal("1.00")

    def test_uses_last_snapshot(self):
        snaps = [_snapshot(d="2026-07-04", equity="500000"), _snapshot()]
        port, _ = weekly_returns(snaps, Decimal("606000"), Decimal("420"))
        assert port == Decimal("1.00")

    def test_invalid_previous_values_return_none(self):
        assert weekly_returns(
            [_snapshot(equity="0")], Decimal("600000"), Decimal("420")
        ) is None
        assert weekly_returns(
            [_snapshot(equity="broken")], Decimal("600000"), Decimal("420")
        ) is None


# ── build_summary ──────────────────────────────────────────────────────────────

def _summary(**overrides) -> str:
    kwargs = {
        "today": TODAY,
        "week_trades": [],
        "holdings": [("2201.T", 100, Decimal("2717.5"), Decimal("2750"))],
        "cash_jpy": Decimal("328250"),
        "equity_jpy": Decimal("603250"),
        "cumulative_pnl_jpy": Decimal("0"),
        "cb_net_loss_jpy": Decimal("0"),
        "topix_close": Decimal("422.1"),
        "returns": (Decimal("0.54"), Decimal("-0.50")),
        "screener_counts": [3, 4, 2, 5, 3],
    }
    kwargs.update(overrides)
    return build_summary(**kwargs)


class TestBuildSummary:
    def test_contains_all_sections(self):
        msg = _summary()
        assert "週次サマリー" in msg
        assert "2201.T ×100株" in msg
        assert "評価額*: ¥603,250" in msg
        assert "CB 余裕*: ¥90,000 / ¥90,000" in msg
        assert "超過 +1.04pt" in msg
        assert "3 → 4 → 2 → 5 → 3" in msg

    def test_no_holdings_shows_idle(self):
        msg = _summary(holdings=[], equity_jpy=Decimal("600000"))
        assert "なし（IDLE）" in msg

    def test_first_snapshot_message(self):
        msg = _summary(returns=None)
        assert "比較は来週から" in msg

    def test_week_trades_listed_with_pnl(self):
        msg = _summary(week_trades=[
            dict(zip(
                ["run_id", "ticker", "side", "shares", "price",
                 "executed_at", "pnl_jpy", "exit_reason"],
                _sell_row("2026-07-16T20:35:00+09:00"),
                strict=True,
            ))
        ])
        assert "TIME_STOP" in msg
        assert "¥-6,750" in msg

    def test_cb_margin_reflects_net_loss(self):
        msg = _summary(cb_net_loss_jpy=Decimal("30000"))
        assert "CB 余裕*: ¥60,000" in msg

    def test_prev_note_disclaimer(self):
        msg = _summary(prev_note="7/12 に¥100k入金")
        assert "参考値" in msg
