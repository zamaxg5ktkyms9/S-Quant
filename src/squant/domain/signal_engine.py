"""Buy signal detection — pure functions over OHLCV DataFrames."""

from datetime import date
from decimal import Decimal

import pandas as pd

from squant.config.constants import (
    MA_LONG,
    MA_SHORT,
    RSI_BUY_THRESHOLD,
    RSI_PERIOD,
    VOLATILITY_WINDOW,
)
from squant.domain.indicators import rolling_std, rsi, sma, volume_surge_ratio
from squant.domain.models import Candidate


def detect_signals(
    filtered_tickers: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
) -> list[Candidate]:
    """Apply 4 buy conditions; return all passing Candidates.

    ohlcv: DataFrame with ticker as column, date as index, values are adjusted close.
    """
    candidates: list[Candidate] = []
    dropped = {"no_data": 0, "cond1_trend": 0, "cond2_rsi": 0, "cond3_volatility": 0, "cond4_ma_vol": 0}

    for ticker in filtered_tickers:
        if ticker not in ohlcv.columns:
            dropped["no_data"] += 1
            continue

        close = ohlcv[ticker].dropna()
        if len(close) < MA_LONG + 10:
            dropped["no_data"] += 1
            continue

        # Condition 1: close > 75-day MA (long-term uptrend)
        ma_long = sma(close, MA_LONG)
        if pd.isna(ma_long.iloc[-1]) or close.iloc[-1] <= ma_long.iloc[-1]:
            dropped["cond1_trend"] += 1
            continue

        # Condition 2: RSI(14) < 45 (pullback)
        rsi_series = rsi(close, RSI_PERIOD)
        last_rsi = rsi_series.iloc[-1]
        if pd.isna(last_rsi) or last_rsi >= RSI_BUY_THRESHOLD:
            dropped["cond2_rsi"] += 1
            continue

        # Condition 3: 20-day std below historical mean (volatility contraction)
        std_series = rolling_std(close, VOLATILITY_WINDOW)
        last_std = std_series.iloc[-1]
        hist_mean_std = std_series.iloc[:-1].mean()
        if pd.isna(last_std) or pd.isna(hist_mean_std) or last_std > hist_mean_std:
            dropped["cond3_volatility"] += 1
            continue

        # Condition 4: close > 5-day MA AND today volume > yesterday volume
        ma_short = sma(close, MA_SHORT)
        if pd.isna(ma_short.iloc[-1]) or close.iloc[-1] <= ma_short.iloc[-1]:
            dropped["cond4_ma_vol"] += 1
            continue

        vol_col = f"{ticker}_vol"
        if vol_col in ohlcv.columns:
            vol = ohlcv[vol_col]
        elif hasattr(ohlcv, "volume") and ticker in ohlcv.volume.columns:
            vol = ohlcv.volume[ticker]
        else:
            dropped["cond4_ma_vol"] += 1
            continue

        vol_clean = vol.dropna()
        if len(vol_clean) < 2 or vol_clean.iloc[-1] <= vol_clean.iloc[-2]:
            dropped["cond4_ma_vol"] += 1
            continue

        # Compute volume surge ratio for ranking
        vol_surge = volume_surge_ratio(vol_clean, window=20)
        last_surge = float(vol_surge.iloc[-1]) if not pd.isna(vol_surge.iloc[-1]) else 0.0

        fund = fundamentals.loc[ticker] if ticker in fundamentals.index else None
        pbr = float(fund.get("pbr", 99.0)) if fund is not None else 99.0
        mcap = float(fund.get("market_cap_jpy", 0)) if fund is not None else 0.0

        candidates.append(
            Candidate(
                ticker=ticker,
                close=Decimal(str(round(close.iloc[-1], 1))),
                rsi14=float(last_rsi),
                volume_surge_ratio=last_surge,
                pbr=pbr,
                market_cap_jpy=mcap,
            )
        )

    import logging as _logging
    _logging.getLogger(__name__).info(
        f"Signal filter counts (dropped): "
        f"no_data={dropped['no_data']} "
        f"cond1_trend={dropped['cond1_trend']} "
        f"cond2_rsi={dropped['cond2_rsi']} "
        f"cond3_volatility={dropped['cond3_volatility']} "
        f"cond4_ma_vol={dropped['cond4_ma_vol']} "
        f"passed={len(candidates)}"
    )
    return candidates
