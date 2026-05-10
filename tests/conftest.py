"""Shared fixtures for all tests."""

from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

JST = timezone(timedelta(hours=9))


# ── Date helpers ───────────────────────────────────────────────────────────────

@pytest.fixture
def trading_day() -> date:
    """A known TSE trading day: 2026-05-11 (Monday)."""
    return date(2026, 5, 11)


@pytest.fixture
def friday() -> date:
    return date(2026, 5, 8)


# ── OHLCV fixtures ─────────────────────────────────────────────────────────────

def make_close_series(
    n: int = 100,
    start_price: float = 500.0,
    trend: float = 0.0,
    seed: int = 42,
) -> pd.Series:
    rng = np.random.default_rng(seed)
    returns = rng.normal(trend, 0.01, n)
    prices = start_price * np.exp(np.cumsum(returns))
    idx = pd.date_range(end="2026-05-11", periods=n, freq="B")
    return pd.Series(prices, index=idx)


def make_volume_series(n: int = 100, avg: float = 1_000_000.0, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    vols = rng.lognormal(mean=np.log(avg), sigma=0.3, size=n)
    idx = pd.date_range(end="2026-05-11", periods=n, freq="B")
    return pd.Series(vols, index=idx)


@pytest.fixture
def close_series_100() -> pd.Series:
    return make_close_series(100)


@pytest.fixture
def close_series_bullish() -> pd.Series:
    """Uptrending series that should trigger buy signal conditions."""
    # Strong uptrend then pullback (RSI should drop below 45)
    rng = np.random.default_rng(7)
    n = 100
    prices = [500.0]
    for i in range(n - 1):
        if i < 70:
            change = rng.normal(0.003, 0.008)   # uptrend
        else:
            change = rng.normal(-0.008, 0.008)  # pullback
        prices.append(prices[-1] * (1 + change))
    idx = pd.date_range(end="2026-05-11", periods=n, freq="B")
    return pd.Series(prices, index=idx)


# ── Position fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def sample_position():
    from squant.domain.models import Position
    return Position(
        ticker="1234.T",
        shares=100,
        entry_price=Decimal("500"),
        intended_entry_price=Decimal("498"),
        entry_date=date(2026, 5, 5),
        stop_loss_price=Decimal("487.50"),   # 500 * 0.975
        trailing_stop_price=Decimal("487.50"),
        highest_price_since_entry=Decimal("520"),
        time_stop_date=date(2026, 5, 12),    # entry + 5 trading days
    )


# ── Clock stub ─────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, d: date) -> None:
        self._date = d

    def now_jst(self) -> datetime:
        return datetime.combine(self._date, datetime.min.time()).replace(tzinfo=JST)

    def today_jst(self) -> date:
        return self._date


@pytest.fixture
def fake_clock(trading_day):
    return FakeClock(trading_day)
