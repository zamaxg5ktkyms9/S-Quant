"""yfinance market data adapter.

Uses auto_adjust=False to get both raw Close and Adj Close:
- Adj Close → technical indicators (split/dividend continuous)
- Close      → entry price reference (actual yen amount)
"""

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from squant.infrastructure.data_validator import DataValidator
from squant.utils.logging import get_logger
from squant.utils.retry import with_retry

logger = get_logger(__name__)

# Canary ticker for connectivity check
_CANARY = "7203.T"  # Toyota


class YFinanceClient:
    def __init__(
        self,
        validator: DataValidator,
        cache_dir: Path | None = None,
    ) -> None:
        self._validator = validator
        self._cache_dir = cache_dir

    @with_retry(max_attempts=3, min_wait=30.0, max_wait=90.0)
    def _download(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        ticker_str = " ".join(tickers)
        df = yf.download(
            tickers=ticker_str,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        # yf.download swallows YFRateLimitError and returns empty — detect and raise
        # so the retry decorator can back off and retry
        if df.empty and tickers:
            raise RuntimeError(f"yfinance returned empty DataFrame for {len(tickers)} tickers — likely rate limited")
        return df

    def fetch_ohlcv(
        self,
        tickers: list[str],
        start: date,
        end: date,
        on_progress: Callable[[int, int], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (adj_close_df, volume_df) with tickers as columns."""
        raw = self._download(tickers, start, end)
        if raw.empty:
            logger.warning("yfinance returned empty DataFrame")
            return pd.DataFrame(), pd.DataFrame()

        # yfinance returns MultiIndex columns when multiple tickers
        if isinstance(raw.columns, pd.MultiIndex):
            adj_close = raw["Adj Close"].copy()
            volume = raw["Volume"].copy()
        else:
            # Single ticker — columns are flat
            ticker = tickers[0]
            adj_close = raw[["Adj Close"]].rename(columns={"Adj Close": ticker})
            volume = raw[["Volume"]].rename(columns={"Volume": ticker})

        return adj_close, volume

    def fetch_ohlcv_full(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        """Return MultiIndex (Date, Ticker) with Open/High/Low/Close/AdjClose/Volume."""
        raw = self._download(tickers, start, end)
        return raw

    def fetch_fundamentals(
        self,
        tickers: list[str],
        on_progress: Callable[[int, int], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> pd.DataFrame:
        """Fetch market cap, PBR, equity ratio for each ticker.

        Uses yfinance fast_info + info. Falls back gracefully on missing fields.
        Note: yfinance .info is rate-limited; prefer pre-cached fundamentals for bulk use.
        """
        records = []
        for ticker in tickers:
            try:
                info = self._fetch_ticker_info(ticker)
                records.append(
                    {
                        "ticker": ticker,
                        "market_cap_jpy": info.get("marketCap", 0) or 0,
                        "pbr": info.get("priceToBook", 0) or 0,
                        "equity_ratio": _compute_equity_ratio(info),
                        "avg_5d_trading_value_jpy": _compute_avg_trading_value(info),
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to fetch fundamentals for {ticker}: {e}")
                records.append(
                    {
                        "ticker": ticker,
                        "market_cap_jpy": 0,
                        "pbr": 0,
                        "equity_ratio": 0,
                        "avg_5d_trading_value_jpy": 0,
                    }
                )

        df = pd.DataFrame(records)
        if not df.empty:
            df = df.set_index("ticker")
        return df

    def check_connectivity(self) -> bool:
        """Skip pre-flight canary — a separate yf.download call burns rate-limit quota
        before the bulk fetch and has no retry. Actual connectivity failures are handled
        by the retry decorator on _download (3 attempts, 30-90 s backoff)."""
        return True

    @with_retry(max_attempts=2, min_wait=1.0, max_wait=5.0)
    def _fetch_ticker_info(self, ticker: str) -> dict:
        return yf.Ticker(ticker).info or {}


def _compute_equity_ratio(info: dict) -> float:
    total_assets = info.get("totalAssets", 0) or 0
    total_equity = info.get("totalStockholderEquity", 0) or 0
    if total_assets <= 0:
        return 0.0
    return total_equity / total_assets


def _compute_avg_trading_value(info: dict) -> float:
    avg_vol = info.get("averageVolume", 0) or 0
    price = info.get("regularMarketPrice", 0) or info.get("currentPrice", 0) or 0
    return float(avg_vol) * float(price)
