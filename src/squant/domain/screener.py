"""Universe filtering — pure functions, no I/O."""

from datetime import date
from decimal import Decimal

import pandas as pd

from squant.config.constants import (
    EARNINGS_BLACKOUT_DAYS,
    EQUITY_RATIO_MIN,
    LIQUIDITY_MIN_JPY,
    MARKET_CAP_MIN_JPY,
    PBR_MAX,
    PBR_MIN,
    PRICE_MAX,
    PRICE_MIN,
)
from squant.utils.jst import count_trading_days


def _is_in_blackout(ticker: str, as_of: date, blackouts: set[tuple[str, date]]) -> bool:
    """True if as_of is within EARNINGS_BLACKOUT_DAYS of any blackout date for ticker."""
    for t, blackout_date in blackouts:
        if t != ticker:
            continue
        days = count_trading_days(
            min(as_of, blackout_date) - __import__("datetime").timedelta(days=1),
            max(as_of, blackout_date),
        )
        if days <= EARNINGS_BLACKOUT_DAYS:
            return True
    return False


def apply_fundamental_filters(
    universe: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
    blackouts: set[tuple[str, date]],
) -> pd.DataFrame:
    """Return filtered DataFrame with columns: ticker, close, market_cap, pbr, equity_ratio.

    ohlcv: MultiIndex (Date, Ticker) or wide with ticker columns — caller normalises.
    fundamentals: indexed by ticker with columns: market_cap_jpy, pbr, equity_ratio,
                  avg_5d_trading_value_jpy.
    """
    counts = {
        "no_fundamentals": 0,
        "market_cap": 0,
        "liquidity": 0,
        "pbr": 0,
        "equity_ratio": 0,
        "price": 0,
        "blackout": 0,
    }
    results = []
    for ticker in universe:
        if ticker not in fundamentals.index:
            counts["no_fundamentals"] += 1
            continue

        fund = fundamentals.loc[ticker]

        if fund.get("market_cap_jpy", 0) < MARKET_CAP_MIN_JPY:
            counts["market_cap"] += 1
            continue

        if fund.get("avg_5d_trading_value_jpy", 0) < LIQUIDITY_MIN_JPY:
            counts["liquidity"] += 1
            continue

        pbr = fund.get("pbr", 0)
        if not (PBR_MIN <= pbr <= PBR_MAX):
            counts["pbr"] += 1
            continue

        if fund.get("equity_ratio", 0) < EQUITY_RATIO_MIN:
            counts["equity_ratio"] += 1
            continue

        if ticker not in ohlcv.columns:
            counts["price"] += 1
            continue
        close_series = ohlcv[ticker].dropna()
        if close_series.empty:
            counts["price"] += 1
            continue
        last_close = Decimal(str(close_series.iloc[-1]))
        if not (PRICE_MIN <= last_close <= PRICE_MAX):
            counts["price"] += 1
            continue

        if _is_in_blackout(ticker, as_of, blackouts):
            counts["blackout"] += 1
            continue

        results.append(
            {
                "ticker": ticker,
                "close": float(last_close),
                "market_cap_jpy": float(fund.get("market_cap_jpy", 0)),
                "pbr": float(pbr),
                "equity_ratio": float(fund.get("equity_ratio", 0)),
                "avg_5d_trading_value_jpy": float(fund.get("avg_5d_trading_value_jpy", 0)),
            }
        )

    df = pd.DataFrame(results)
    df.attrs["filter_counts"] = counts
    return df


def exclude_recent_sales(candidates_df: pd.DataFrame, forbidden_tickers: set[str]) -> pd.DataFrame:
    """Remove tickers subject to 差金決済 prohibition."""
    return candidates_df[~candidates_df["ticker"].isin(forbidden_tickers)].reset_index(drop=True)
