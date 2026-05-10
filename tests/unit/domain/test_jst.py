"""Tests for T+2 settlement and TSE business day calculations."""

from datetime import date

import pytest

from squant.utils.jst import (
    add_trading_days,
    calculate_settlement_date,
    count_trading_days,
    is_settlement_unlocked,
    is_tse_trading_day,
    next_trading_day,
)


class TestIsTseTradingDay:
    def test_regular_monday(self):
        assert is_tse_trading_day(date(2026, 5, 11)) is True

    def test_saturday_is_not_trading(self):
        assert is_tse_trading_day(date(2026, 5, 9)) is False

    def test_sunday_is_not_trading(self):
        assert is_tse_trading_day(date(2026, 5, 10)) is False

    def test_jan1_new_year_is_not_trading(self):
        assert is_tse_trading_day(date(2026, 1, 1)) is False

    def test_dec31_year_end_is_not_trading(self):
        assert is_tse_trading_day(date(2025, 12, 31)) is False

    def test_jan2_new_year_is_not_trading(self):
        assert is_tse_trading_day(date(2026, 1, 2)) is False

    def test_jan3_new_year_is_not_trading(self):
        assert is_tse_trading_day(date(2026, 1, 3)) is False

    def test_jan4_is_trading(self):
        # 2026-01-04 is a Sunday, so 2026-01-05 (Monday) should be trading
        assert is_tse_trading_day(date(2026, 1, 5)) is True

    def test_mountain_day_2026(self):
        # 山の日 is August 11
        assert is_tse_trading_day(date(2026, 8, 11)) is False


class TestAddTradingDays:
    def test_add_zero(self):
        d = date(2026, 5, 11)
        assert add_trading_days(d, 0) == d

    def test_add_one_from_monday(self):
        # Monday → Tuesday
        assert add_trading_days(date(2026, 5, 11), 1) == date(2026, 5, 12)

    def test_add_one_skips_weekend(self):
        # Friday → Monday
        assert add_trading_days(date(2026, 5, 8), 1) == date(2026, 5, 11)

    def test_add_two_from_friday(self):
        # Friday + 2 = Tuesday (skip Saturday, Sunday)
        assert add_trading_days(date(2026, 5, 8), 2) == date(2026, 5, 12)


class TestCalculateSettlementDate:
    def test_monday_sell_settles_wednesday(self):
        sell = date(2026, 5, 11)  # Monday
        assert calculate_settlement_date(sell) == date(2026, 5, 13)  # Wednesday

    def test_friday_sell_settles_tuesday(self):
        # Friday → skip weekend → Monday, Tuesday
        sell = date(2026, 5, 8)   # Friday
        assert calculate_settlement_date(sell) == date(2026, 5, 12)  # Tuesday

    def test_thursday_sell_settles_monday(self):
        # Thursday + 2 trading days = Monday (skip weekend)
        sell = date(2026, 5, 7)   # Thursday
        assert calculate_settlement_date(sell) == date(2026, 5, 11)  # Monday

    def test_non_trading_day_raises(self):
        with pytest.raises(ValueError, match="not a TSE trading day"):
            calculate_settlement_date(date(2026, 5, 9))  # Saturday


class TestCountTradingDays:
    def test_count_mon_to_fri(self):
        assert count_trading_days(date(2026, 5, 11), date(2026, 5, 15)) == 4

    def test_count_across_weekend(self):
        # Mon → next Mon: 5 trading days
        assert count_trading_days(date(2026, 5, 11), date(2026, 5, 18)) == 5

    def test_same_day_is_zero(self):
        d = date(2026, 5, 11)
        assert count_trading_days(d, d) == 0

    def test_end_before_start_is_zero(self):
        assert count_trading_days(date(2026, 5, 12), date(2026, 5, 11)) == 0


class TestIsSettlementUnlocked:
    def test_unlocked_when_settle_date_is_next_trading_day(self):
        # today=Monday, settle=Tuesday, next_exec=Tuesday → unlocked
        assert is_settlement_unlocked(date(2026, 5, 12), date(2026, 5, 11)) is True

    def test_locked_when_settle_date_is_after_next_exec(self):
        # today=Monday, settle=Wednesday, next_exec=Tuesday → locked
        assert is_settlement_unlocked(date(2026, 5, 13), date(2026, 5, 11)) is False

    def test_unlocked_when_settle_date_is_today(self):
        # settle date has already passed
        assert is_settlement_unlocked(date(2026, 5, 11), date(2026, 5, 11)) is True
