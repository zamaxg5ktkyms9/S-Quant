"""Tests for the 20:00 JST execution time guard in DailyRunner."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from squant.application.daily_runner import DailyRunner
from squant.config.settings import Settings

JST = timezone(timedelta(hours=9))
_TRADING_DAY = date(2026, 5, 11)  # Monday
_WEEKEND_DAY = date(2026, 5, 9)   # Saturday


class _FakeClock:
    def __init__(self, hour: int, minute: int = 0, day: date = _TRADING_DAY) -> None:
        self._hour = hour
        self._minute = minute
        self._day = day

    def now_jst(self) -> datetime:
        return datetime(self._day.year, self._day.month, self._day.day,
                        self._hour, self._minute, 0, tzinfo=JST)

    def today_jst(self) -> date:
        return self._day


def _make_runner(
    hour: int,
    minute: int = 0,
    day: date = _TRADING_DAY,
    bypass_execution_time_guard: bool = False,
    bypass_trading_day_check: bool = False,
) -> DailyRunner:
    settings = Settings(
        gcp_sa_key_json="{}",
        spreadsheet_id="test-id",
        slack_webhook_url="",
        bypass_execution_time_guard=bypass_execution_time_guard,
        bypass_trading_day_check=bypass_trading_day_check,
    )
    repo = MagicMock()
    repo.has_run_today.return_value = True  # idempotency guard: skip the rest of the pipeline
    data = MagicMock()
    notifier = MagicMock()
    return DailyRunner(
        state_repo=repo,
        market_data=data,
        notifier=notifier,
        clock=_FakeClock(hour, minute, day),
        settings=settings,
        idle_pipeline=MagicMock(),
        holding_pipeline=MagicMock(),
        settling_pipeline=MagicMock(),
    )


class TestExecutionTimeGuard:
    def test_skips_at_19_59_jst(self):
        runner = _make_runner(hour=19, minute=59)
        result = runner.run()
        assert result.success is True
        assert "execution_time_guard" in result.note
        runner._repo.has_run_today.assert_not_called()

    def test_skips_at_midnight(self):
        runner = _make_runner(hour=0)
        result = runner.run()
        assert result.success is True
        assert "execution_time_guard" in result.note

    def test_skips_at_12_00_jst(self):
        runner = _make_runner(hour=12)
        result = runner.run()
        assert result.success is True
        assert "execution_time_guard" in result.note

    def test_proceeds_at_20_00_jst(self):
        """At 20:00 guard passes — idempotency guard fires instead."""
        runner = _make_runner(hour=20, minute=0)
        result = runner.run()
        # Guard did NOT fire → has_run_today was consulted
        runner._repo.has_run_today.assert_called_once()
        assert "execution_time_guard" not in result.note

    def test_proceeds_at_20_15_jst(self):
        """20:15 is the GitHub Actions trigger time — must pass."""
        runner = _make_runner(hour=20, minute=15)
        result = runner.run()
        runner._repo.has_run_today.assert_called_once()
        assert "execution_time_guard" not in result.note

    def test_proceeds_at_23_jst(self):
        runner = _make_runner(hour=23)
        result = runner.run()
        runner._repo.has_run_today.assert_called_once()
        assert "execution_time_guard" not in result.note


class TestGuardBypass:
    """workflow_dispatch can set BYPASS_* env to skip guards for full-pipeline testing."""

    def test_execution_guard_bypassed_when_settings_true(self):
        """19:59 JST + bypass=True → guard is logged but skipped; pipeline proceeds."""
        runner = _make_runner(hour=19, minute=59, bypass_execution_time_guard=True)
        result = runner.run()
        # Guard bypassed → has_run_today was consulted (= we got past the guard)
        runner._repo.has_run_today.assert_called_once()
        assert "execution_time_guard" not in result.note

    def test_trading_day_check_bypassed_when_settings_true(self):
        """Saturday + bypass=True → non-trading-day skip is logged but bypassed."""
        runner = _make_runner(
            hour=20, minute=15, day=_WEEKEND_DAY, bypass_trading_day_check=True,
        )
        result = runner.run()
        runner._repo.has_run_today.assert_called_once()
        assert result.note != "non-trading day"

    def test_trading_day_check_skips_when_bypass_false(self):
        """Saturday + bypass=False → still skipped as before (regression)."""
        runner = _make_runner(hour=20, minute=15, day=_WEEKEND_DAY)
        result = runner.run()
        assert result.note == "non-trading day"
        runner._repo.has_run_today.assert_not_called()
