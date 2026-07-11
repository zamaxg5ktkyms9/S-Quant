"""Unit tests for V-2 ledger invariant checks."""

import sys
from decimal import Decimal

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from verify_ledger import check_ledger, sum_trades_pnl


def _check(**overrides) -> list[str]:
    # 現行本番の実数（2201.T ×100 @2717.5、初期¥600k、実現損益0）を基準形とする
    kwargs = {
        "cash_jpy": Decimal("328250.0"),
        "held_cost_jpy": Decimal("271750.0"),
        "cumulative_pnl_jpy": Decimal("0"),
        "trades_pnl_sum": Decimal("0"),
        "initial_capital_jpy": Decimal("600000"),
    }
    kwargs.update(overrides)
    return check_ledger(**kwargs)


class TestCheckLedger:
    def test_live_production_state_holds(self):
        """現行本番状態（HOLDING 中）は違反ゼロ"""
        assert _check() == []

    def test_idle_state_holds(self):
        assert _check(
            cash_jpy=Decimal("600000"), held_cost_jpy=Decimal("0")
        ) == []

    def test_after_losing_trade_holds(self):
        """損失トレード後: 現金 = 初期 + 損益"""
        assert _check(
            cash_jpy=Decimal("593250"), held_cost_jpy=Decimal("0"),
            cumulative_pnl_jpy=Decimal("-6750"), trades_pnl_sum=Decimal("-6750"),
        ) == []

    def test_pnl_mismatch_detected(self):
        """表示用累積と trades 台帳の乖離 → 違反[1]"""
        v = _check(cumulative_pnl_jpy=Decimal("100"))
        assert any("[1]" in x for x in v)

    def test_cash_leak_detected(self):
        """記録なしの現金減（例: 記録漏れの発注）→ 違反[2]"""
        v = _check(cash_jpy=Decimal("300000"))
        assert any("[2]" in x for x in v)

    def test_unrecorded_deposit_detected(self):
        """BUDGET_JPY 未反映の入金 → 違反[2]"""
        v = _check(cash_jpy=Decimal("428250.0"))
        assert any("[2]" in x for x in v)
        assert any("BUDGET_JPY" in x for x in v)


class TestSumTradesPnl:
    def test_sums_and_skips_blank(self):
        rows = [
            ["r1", "2201.T", "SELL", "100", "2650", "2026-07-13T20:35:00+09:00", "-6750", "STOP_LOSS"],
            ["r2", "9999.T", "BUY", "100", "500", "2026-07-14T20:35:00+09:00", "", ""],
            ["r3", "8888.T", "SELL", "100", "550", "2026-07-20T20:35:00+09:00", "5000", "TRAILING_STOP"],
        ]
        assert sum_trades_pnl(rows) == Decimal("-1750")

    def test_empty(self):
        assert sum_trades_pnl([]) == Decimal("0")

    def test_short_rows_skipped(self):
        assert sum_trades_pnl([[], ["only", "two"]]) == Decimal("0")
