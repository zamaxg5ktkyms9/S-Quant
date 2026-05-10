"""JST-aware date/time utilities and TSE business day calculations."""

from datetime import date, datetime, timedelta, timezone

import jpholiday

JST = timezone(timedelta(hours=9), "JST")

# TSE year-end / new-year closures not covered by jpholiday
_TSE_EXTRA_MONTH_DAY = frozenset([(12, 31), (1, 2), (1, 3)])


def now_jst() -> datetime:
    return datetime.now(tz=JST)


def today_jst() -> date:
    return now_jst().date()


def is_tse_trading_day(d: date) -> bool:
    """Return True if d is a TSE trading day (weekday, not holiday, not year-end)."""
    if d.weekday() >= 5:
        return False
    if jpholiday.is_holiday(d):
        return False
    return (d.month, d.day) not in _TSE_EXTRA_MONTH_DAY


def add_trading_days(start: date, n: int) -> date:
    """Return the date that is n TSE trading days after start (start not counted)."""
    if n < 0:
        raise ValueError("n must be >= 0")
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if is_tse_trading_day(d):
            added += 1
    return d


def count_trading_days(start: date, end: date) -> int:
    """Count TSE trading days in the half-open interval (start, end]."""
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_tse_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def calculate_settlement_date(sell_date: date) -> date:
    """Return the T+2 settlement date for a sell executed on sell_date.

    Funds become available for purchase on the settlement date's session.
    A buy signal generated at 20:00 on day X for execution at X+1 open is
    valid when: settlement_date <= next_trading_day(X).
    """
    if not is_tse_trading_day(sell_date):
        raise ValueError(f"{sell_date} is not a TSE trading day")
    return add_trading_days(sell_date, 2)


def next_trading_day(d: date) -> date:
    """Return the next TSE trading day after d."""
    return add_trading_days(d, 1)


def prev_trading_day(d: date) -> date:
    """Return the most recent TSE trading day before d."""
    candidate = d - timedelta(days=1)
    while not is_tse_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def is_settlement_unlocked(settlement_date: date, as_of: date) -> bool:
    """True when T+2 funds are available for a new purchase executed on as_of."""
    next_exec = next_trading_day(as_of)
    return settlement_date <= next_exec
