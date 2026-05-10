"""Protocol definitions — all external I/O hides behind these interfaces."""

from datetime import date, datetime
from typing import Protocol

import pandas as pd

from squant.domain.models import (
    CircuitBreakerStatus,
    PendingSignal,
    PortfolioState,
    RecentSale,
    RunRecord,
    TradeRecord,
)


class IMarketDataClient(Protocol):
    def fetch_ohlcv(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (adj_close_df, volume_df) with ticker as columns, date as index."""
        ...

    def fetch_fundamentals(self, tickers: list[str]) -> pd.DataFrame:
        """Return DataFrame indexed by ticker with columns:
        market_cap_jpy, pbr, equity_ratio, avg_5d_trading_value_jpy.
        """
        ...

    def fetch_ohlcv_full(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Return MultiIndex (Date, Ticker) DataFrame with Open/High/Low/Close/Volume columns."""
        ...


class IStateRepository(Protocol):
    def load_portfolio(self) -> PortfolioState: ...
    def save_portfolio(self, state: PortfolioState) -> None: ...
    def append_trade(self, trade: TradeRecord) -> None: ...
    def save_pending_signal(self, pending: PendingSignal) -> None: ...
    def load_pending_signal(self) -> PendingSignal | None: ...
    def confirm_pending_signal(
        self,
        actual_price: float,
        actual_shares: int,
        confirmed_at: datetime,
    ) -> None: ...
    def cancel_pending_signal(self) -> None: ...
    def load_circuit_breaker(self) -> CircuitBreakerStatus: ...
    def save_circuit_breaker(self, status: CircuitBreakerStatus) -> None: ...
    def load_recent_sales(self) -> list[RecentSale]: ...
    def append_recent_sale(self, sale: RecentSale) -> None: ...
    def has_run_today(self, today: date) -> bool: ...
    def mark_run_complete(self, record: RunRecord) -> None: ...


class INotifier(Protocol):
    def send(self, text: str, blocks: list[dict] | None = None) -> None: ...
    def send_error(self, title: str, detail: str) -> None: ...


class IClock(Protocol):
    def now_jst(self) -> datetime: ...
    def today_jst(self) -> date: ...
