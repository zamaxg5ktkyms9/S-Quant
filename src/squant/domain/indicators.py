"""Pure technical indicator functions over pandas Series/DataFrames."""

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # avg_loss == 0 with valid avg_gain → RSI = 100 (no losses; fully bullish)
    mask_zero_loss = (avg_loss == 0) & avg_gain.notna()
    return result.where(~mask_zero_loss, 100.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def rolling_std(series: pd.Series, window: int = 20) -> pd.Series:
    return series.rolling(window=window, min_periods=window).std()


def volume_surge_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
    """today_volume / window-day average volume."""
    avg = volume.shift(1).rolling(window=window, min_periods=window).mean()
    return volume / avg.replace(0, np.nan)
