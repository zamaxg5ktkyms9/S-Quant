"""Unit tests for watchdog target-date resolution and run_log evaluation.

回帰対象: GHA スケジュール遅延で watchdog が日付をまたいで起動すると、
`now.date()` を素朴に使うと「まだ実行されていない翌日の 20:30 ラン」を探して
誤報していた（2026-07-14 02:00・2026-07-15 01:00 の false alert 実績）。
resolve_target_date がアンカー日を返すことでこれを防ぐ。
"""

import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from watchdog import evaluate_run_log, resolve_target_date

JST = timezone(timedelta(hours=9))

HEADER = ["run_id", "run_date", "status", "note", "completed_at"]


def _rows(*entries: tuple[str, str]) -> list[list[str]]:
    """entries = (run_date, status) の並び。run_log 形式の行列を組む。"""
    rows = [HEADER]
    for i, (run_date, status) in enumerate(entries):
        rows.append([f"id{i}", run_date, status, "", f"{run_date}T21:00:00+09:00"])
    return rows


# ── resolve_target_date ───────────────────────────────────────────────────────

class TestResolveTargetDate:
    def test_on_time_2345_returns_same_day(self):
        """定刻 23:45 JST（月曜・営業日）は当日を検証対象にする"""
        now = datetime(2026, 7, 13, 23, 45, tzinfo=JST)  # Mon
        assert resolve_target_date(now) == date(2026, 7, 13)

    def test_late_after_midnight_returns_previous_trading_day(self):
        """深夜 02:00 まで遅延した起動は前営業日を検証（7/14 誤報の回帰）"""
        now = datetime(2026, 7, 14, 2, 0, tzinfo=JST)  # Tue 02:00
        assert resolve_target_date(now) == date(2026, 7, 13)  # Mon の run を照合

    def test_late_0100_returns_previous_trading_day(self):
        """7/15 01:00 起動 → 7/14（火）の run を検証（7/15 誤報の回帰）"""
        now = datetime(2026, 7, 15, 1, 0, tzinfo=JST)
        assert resolve_target_date(now) == date(2026, 7, 14)

    def test_late_up_to_0545_still_previous_day(self):
        """遅延上限（〜05:45 JST）でも前営業日を正しく解決する"""
        now = datetime(2026, 7, 14, 5, 45, tzinfo=JST)
        assert resolve_target_date(now) == date(2026, 7, 13)

    def test_non_trading_anchor_returns_none(self):
        """アンカー日が非営業日（土曜）なら None（skip）"""
        now = datetime(2026, 7, 18, 23, 45, tzinfo=JST)  # Sat 23:45 → anchor Sat
        assert resolve_target_date(now) is None

    def test_holiday_anchor_returns_none(self):
        """アンカー日が祝日（海の日 7/20 月）なら None"""
        now = datetime(2026, 7, 20, 23, 45, tzinfo=JST)  # Marine Day (Mon)
        assert resolve_target_date(now) is None


# ── evaluate_run_log ──────────────────────────────────────────────────────────

class TestEvaluateRunLog:
    def test_success_present_is_ok(self):
        rows = _rows(("2026-07-13", "success"))
        ok, msg = evaluate_run_log(rows, date(2026, 7, 13))
        assert ok is True
        assert msg == ""

    def test_missing_row_alerts(self):
        rows = _rows(("2026-07-10", "success"))
        ok, msg = evaluate_run_log(rows, date(2026, 7, 13))
        assert ok is False
        assert "未実行" in msg
        assert "2026-07-13" in msg

    def test_failed_status_alerts(self):
        rows = _rows(("2026-07-13", "failed"))
        ok, msg = evaluate_run_log(rows, date(2026, 7, 13))
        assert ok is False
        assert "success になっていません" in msg
        assert "failed" in msg

    def test_success_among_multiple_rows(self):
        rows = _rows(
            ("2026-07-13", "failed"),
            ("2026-07-13", "success"),
        )
        ok, _ = evaluate_run_log(rows, date(2026, 7, 13))
        assert ok is True

    def test_empty_run_log_alerts(self):
        ok, msg = evaluate_run_log([HEADER], date(2026, 7, 13))
        assert ok is False
        assert "未実行" in msg
