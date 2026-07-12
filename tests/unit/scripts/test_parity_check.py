"""Unit tests for V-1 daily parity check (model vs production behavior)."""

import sys
from datetime import date
from decimal import Decimal

import pandas as pd

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from parity_check import (
    ModelExitOutcome,
    ParityRow,
    _rows_to_dicts,
    compare_entry,
    compare_exit_still_held,
    compare_exit_traded,
    compare_scan_counts,
    compare_signal_tickers,
    downgrade_alerts_for_low_coverage,
    replay_exit_model,
    resolve_target_date,
)

from squant.domain.models import Position

# June 2026 has no JP holidays — all weekdays are TSE trading days.
_HISTORY_DAYS = [
    date(2026, 6, d) for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19)
]
_ENTRY = date(2026, 6, 22)


def _build_ohlc(rows: dict[date, tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows: {date: (open, high, low, close)} → fetch_ohlcv_full 形式の DataFrame."""
    idx = pd.to_datetime(sorted(rows))
    data = {
        "Adj Close": [rows[d.date()][3] for d in idx],
        "Open": [rows[d.date()][0] for d in idx],
        "High": [rows[d.date()][1] for d in idx],
        "Low": [rows[d.date()][2] for d in idx],
    }
    return pd.DataFrame(data, index=idx)


def _base_rows() -> dict[date, tuple[float, float, float, float]]:
    """フラットな15日履歴（TR=10 → ATR≈10）+ エントリー日。"""
    rows = dict.fromkeys(_HISTORY_DAYS, (1000.0, 1005.0, 995.0, 1000.0))
    rows[_ENTRY] = (1000.0, 1005.0, 995.0, 1000.0)
    return rows


def _replay(rows, through, entry_price="1000"):
    return replay_exit_model(
        ticker="9999.T",
        shares=100,
        entry_price=Decimal(entry_price),
        entry_date=_ENTRY,
        stop_loss_rate=Decimal("0.025"),
        ohlc=_build_ohlc(rows),
        through=through,
    )


class TestReplayExitModel:
    def test_hold_with_trailing_ratchet(self):
        """上昇継続 → 保有継続、トレーリングは初期ストップから切り上がる"""
        rows = _base_rows()
        rows[date(2026, 6, 23)] = (1005, 1010, 1000, 1008)
        rows[date(2026, 6, 24)] = (1010, 1020, 1008, 1018)
        rows[date(2026, 6, 25)] = (1020, 1030, 1018, 1028)
        rows[date(2026, 6, 26)] = (1030, 1040, 1028, 1038)
        out = _replay(rows, date(2026, 6, 26))
        assert out.exited is False
        assert out.days_replayed == 4
        assert out.highest == Decimal("1040")
        # 初期ストップ 975 からラチェット済み（highest 1040 - 2.5×ATR≈10）
        assert out.trailing_stop > Decimal("975")
        assert out.trailing_stop < Decimal("1040")

    def test_time_stop_on_fifth_trading_day(self):
        rows = _base_rows()
        rows[date(2026, 6, 23)] = (1005, 1010, 1000, 1008)
        rows[date(2026, 6, 24)] = (1010, 1020, 1008, 1018)
        rows[date(2026, 6, 25)] = (1020, 1030, 1018, 1028)
        rows[date(2026, 6, 26)] = (1030, 1040, 1028, 1038)
        rows[date(2026, 6, 29)] = (1038, 1043, 1033, 1038)  # 5営業日目・ストップ非到達
        out = _replay(rows, date(2026, 6, 29))
        assert out.exited is True
        assert out.exit_date == date(2026, 6, 29)
        assert out.reason == "TIME_STOP"
        assert out.exit_price == Decimal("1038")  # ザラ場モードは終値で決済

    def test_hard_stop_gap_down_fills_at_open(self):
        """寄付がハードストップ(975)割れ → F-2 ギャップ考慮で寄付価格約定"""
        rows = _base_rows()
        rows[date(2026, 6, 23)] = (940, 950, 930, 945)
        out = _replay(rows, date(2026, 6, 23))
        assert out.exited is True
        assert out.reason == "STOP_LOSS"
        assert out.exit_price == Decimal("940")  # ストップ価格975ではなく寄付940

    def test_trailing_stop_exit_at_stop_price(self):
        """ザラ場安値がトレーリング到達（寄付は上） → トレーリング価格で約定"""
        rows = _base_rows()
        rows[date(2026, 6, 23)] = (1005, 1010, 1000, 1008)
        rows[date(2026, 6, 24)] = (1010, 1020, 1008, 1018)
        rows[date(2026, 6, 25)] = (1020, 1030, 1018, 1028)
        rows[date(2026, 6, 26)] = (1030, 1040, 1028, 1038)
        rows[date(2026, 6, 29)] = (1030, 1035, 990, 995)
        out = _replay(rows, date(2026, 6, 29))
        assert out.exited is True
        assert out.exit_date == date(2026, 6, 29)
        assert out.reason == "TRAILING_STOP"
        # 約定はトレーリング価格（990 < price < 1040）。寄付1030は上なので調整なし
        assert Decimal("990") < out.exit_price < Decimal("1040")

    def test_missing_day_rows_are_skipped(self):
        rows = _base_rows()
        rows[date(2026, 6, 23)] = (1005, 1010, 1000, 1008)
        # 6/24 欠損
        rows[date(2026, 6, 25)] = (1020, 1030, 1018, 1028)
        out = _replay(rows, date(2026, 6, 25))
        assert out.exited is False
        assert out.days_replayed == 2

    def test_no_data_after_entry(self):
        out = _replay(_base_rows(), date(2026, 6, 26))
        assert out.exited is False
        assert out.days_replayed == 0


def _position(**overrides) -> Position:
    kwargs = {
        "ticker": "9999.T",
        "shares": 100,
        "entry_price": Decimal("1000"),
        "intended_entry_price": Decimal("995"),
        "entry_date": _ENTRY,
        "stop_loss_price": Decimal("975"),
        "trailing_stop_price": Decimal("1010"),
        "highest_price_since_entry": Decimal("1038"),
        "time_stop_date": date(2026, 6, 29),
    }
    kwargs.update(overrides)
    return Position(**kwargs)


_D = date(2026, 6, 26)


class TestCompareExitStillHeld:
    def test_model_exit_but_prod_holds_is_alert(self):
        model = ModelExitOutcome(
            exited=True, exit_date=_D, reason="TRAILING_STOP",
            exit_price=Decimal("1010"), days_replayed=4,
        )
        rows = compare_exit_still_held(_D, _position(), model)
        assert [r.severity for r in rows] == ["alert"]
        assert "SBI" in rows[0].note

    def test_both_hold_small_drift_is_info(self):
        model = ModelExitOutcome(
            exited=False, trailing_stop=Decimal("1015"),
            highest=Decimal("1040"), days_replayed=4,
        )
        rows = compare_exit_still_held(_D, _position(), model)
        by_item = {r.item: r for r in rows}
        assert by_item["9999.T.decision"].severity == "ok"
        # drift 5/1010 ≈ 0.5% < 1% → info
        assert by_item["9999.T.trailing_stop"].severity == "info"
        assert by_item["9999.T.highest"].severity == "info"

    def test_both_hold_large_drift_is_alert(self):
        model = ModelExitOutcome(
            exited=False, trailing_stop=Decimal("1030"),
            highest=Decimal("1038"), days_replayed=4,
        )
        rows = compare_exit_still_held(_D, _position(), model)
        by_item = {r.item: r for r in rows}
        # drift 20/1010 ≈ 2% > 1% → alert
        assert by_item["9999.T.trailing_stop"].severity == "alert"
        assert by_item["9999.T.highest"].severity == "ok"

    def test_exact_match_is_ok(self):
        model = ModelExitOutcome(
            exited=False, trailing_stop=Decimal("1010"),
            highest=Decimal("1038"), days_replayed=4,
        )
        assert all(r.severity == "ok" for r in compare_exit_still_held(_D, _position(), model))

    def test_no_replay_data_is_info(self):
        model = ModelExitOutcome(exited=False, days_replayed=0)
        rows = compare_exit_still_held(_D, _position(), model)
        assert [r.severity for r in rows] == ["info"]


def _trade(**overrides) -> dict:
    row = {
        "run_id": "abc", "ticker": "9999.T", "side": "SELL", "shares": "100",
        "price": "1005", "executed_at": f"{_D}T22:00:00+09:00",
        "pnl_jpy": "500", "exit_reason": "TRAILING_STOP",
    }
    row.update(overrides)
    return row


class TestCompareExitTraded:
    def test_matching_exit_is_ok_with_price_info(self):
        model = ModelExitOutcome(
            exited=True, exit_date=_D, reason="TRAILING_STOP",
            exit_price=Decimal("1010"), days_replayed=4,
        )
        rows = compare_exit_traded(_D, _trade(), model)
        by_item = {r.item: r for r in rows}
        assert by_item["9999.T.decision"].severity == "ok"
        assert by_item["9999.T.exit_price"].severity == "info"  # 1010 vs 1005

    def test_reason_mismatch_is_alert(self):
        model = ModelExitOutcome(
            exited=True, exit_date=_D, reason="STOP_LOSS",
            exit_price=Decimal("975"), days_replayed=4,
        )
        rows = compare_exit_traded(_D, _trade(), model)
        assert rows[0].severity == "alert"

    def test_earlier_model_exit_is_alert(self):
        model = ModelExitOutcome(
            exited=True, exit_date=date(2026, 6, 24), reason="TRAILING_STOP",
            exit_price=Decimal("1010"), days_replayed=2,
        )
        assert compare_exit_traded(_D, _trade(), model)[0].severity == "alert"

    def test_model_holds_but_prod_exited_is_alert(self):
        model = ModelExitOutcome(exited=False, days_replayed=4)
        rows = compare_exit_traded(_D, _trade(), model)
        assert [r.severity for r in rows] == ["alert"]


class TestCompareEntry:
    def _pos(self):
        # intended 995 → gap-up cancel > 1014.9、gap-down skip ≤ 970.125
        return _position(entry_price=Decimal("1000"), intended_entry_price=Decimal("995"))

    def test_normal_entry_price_diff_is_info(self):
        rows = compare_entry(_D, self._pos(), Decimal("998"),
                             Decimal("0.02"), Decimal("0.025"))
        assert rows[0].severity == "info"

    def test_exact_open_fill_is_ok(self):
        rows = compare_entry(_D, self._pos(), Decimal("1000"),
                             Decimal("0.02"), Decimal("0.025"))
        assert rows[0].severity == "ok"

    def test_gap_up_should_have_cancelled_is_alert(self):
        rows = compare_entry(_D, self._pos(), Decimal("1020"),
                             Decimal("0.02"), Decimal("0.025"))
        assert rows[0].severity == "alert"
        assert "gap-up" in rows[0].model

    def test_gap_down_should_have_skipped_is_alert(self):
        rows = compare_entry(_D, self._pos(), Decimal("965"),
                             Decimal("0.02"), Decimal("0.025"))
        assert rows[0].severity == "alert"
        assert "gap-down" in rows[0].model

    def test_missing_open_is_info(self):
        rows = compare_entry(_D, self._pos(), None, Decimal("0.02"), Decimal("0.025"))
        assert rows[0].severity == "info"


class TestCompareScanCounts:
    _MODEL = {
        "valid_tickers": 271, "screener_passed": 7,
        "signal_candidates": 1, "signals_sent": 1,
    }

    def test_all_match_is_ok(self):
        funnel = {
            "run_date": str(_D), "universe": "282", "valid_tickers": "271",
            "screener_passed": "7", "signal_candidates": "1", "signals_sent": "1",
        }
        rows = compare_scan_counts(_D, funnel, self._MODEL)
        assert len(rows) == 4
        assert all(r.severity == "ok" for r in rows)

    def test_mismatch_is_alert(self):
        funnel = {
            "run_date": str(_D), "universe": "282", "valid_tickers": "271",
            "screener_passed": "9", "signal_candidates": "1", "signals_sent": "1",
        }
        rows = compare_scan_counts(_D, funnel, self._MODEL)
        by_item = {r.item: r for r in rows}
        assert by_item["screener_passed"].severity == "alert"
        assert by_item["valid_tickers"].severity == "ok"

    def test_no_funnel_row_is_info(self):
        rows = compare_scan_counts(_D, None, self._MODEL)
        assert all(r.severity == "info" for r in rows)


class TestCompareSignalTickers:
    def test_both_empty_no_rows(self):
        assert compare_signal_tickers(_D, [], []) == []

    def test_matching_set_and_price_is_ok(self):
        pending = [{"ticker": "1111.T", "reference_price": "500"}]
        rows = compare_signal_tickers(_D, pending, [("1111.T", Decimal("500"))])
        assert all(r.severity == "ok" for r in rows)

    def test_price_mismatch_is_alert(self):
        pending = [{"ticker": "1111.T", "reference_price": "500"}]
        rows = compare_signal_tickers(_D, pending, [("1111.T", Decimal("501"))])
        by_item = {r.item: r for r in rows}
        assert by_item["signal_tickers"].severity == "ok"
        assert by_item["1111.T.reference_price"].severity == "alert"

    def test_ticker_set_mismatch_is_alert(self):
        pending = [{"ticker": "1111.T", "reference_price": "500"}]
        rows = compare_signal_tickers(_D, pending, [("2222.T", Decimal("500"))])
        assert rows[0].severity == "alert"

    def test_model_signal_without_actual_is_alert(self):
        rows = compare_signal_tickers(_D, [], [("2222.T", Decimal("500"))])
        assert rows[0].severity == "alert"


class TestDowngradeAlertsForLowCoverage:
    def _rows(self):
        return [
            ParityRow(_D, "scan", "valid_tickers", "204", "271", "alert", "不一致"),
            ParityRow(_D, "scan", "screener_passed", "7", "7", "ok"),
        ]

    def test_low_coverage_downgrades_alerts_to_info(self):
        rows = downgrade_alerts_for_low_coverage(self._rows(), coverage=0.75)
        assert rows[0].severity == "info"
        assert "判定保留" in rows[0].note
        assert rows[1].severity == "ok"  # ok はそのまま

    def test_sufficient_coverage_keeps_alerts(self):
        rows = downgrade_alerts_for_low_coverage(self._rows(), coverage=0.95)
        assert rows[0].severity == "alert"


class TestResolveTargetDate:
    def test_latest_success(self):
        rows = [
            {"run_date": "2026-07-09", "status": "success", "note": ""},
            {"run_date": "2026-07-10", "status": "success", "note": "cb_tripped"},
            {"run_date": "2026-07-11", "status": "error", "note": "boom"},
        ]
        assert resolve_target_date(rows) == (date(2026, 7, 10), "cb_tripped")

    def test_no_success_returns_none(self):
        assert resolve_target_date([{"run_date": "2026-07-10", "status": "error"}]) is None
        assert resolve_target_date([]) is None


class TestRowsToDicts:
    def test_pads_short_rows_and_skips_blanks(self):
        rows = _rows_to_dicts(
            [["a", "b", "c"], ["1", "2"], ["", "x", "y"], ["3", "4", "5"]],
            ["a", "b", "c"],
        )
        assert rows == [{"a": "1", "b": "2", "c": ""}, {"a": "3", "b": "4", "c": "5"}]
