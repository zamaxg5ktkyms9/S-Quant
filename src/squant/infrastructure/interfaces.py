"""Protocol definitions — all external I/O hides behind these interfaces."""

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
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
        on_progress: Callable[[int, int], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (adj_close_df, volume_df) with ticker as columns, date as index."""
        ...

    def fetch_fundamentals(
        self,
        tickers: list[str],
        on_progress: Callable[[int, int], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> pd.DataFrame:
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
    # Pending signals (multi-position 2026-05-30):
    # - save_pending_signals: persist a tuple of pending signals (overwrites the tab)
    # - load_pending_signals: load all pending signals as a tuple
    # - confirm_pending_signal(ticker, ...): mark one signal as filled by ticker
    # - cancel_pending_signal(ticker=None): remove one ticker or all
    # The legacy single-signal methods are kept as thin wrappers for back-compat.
    def save_pending_signals(self, pendings: tuple[PendingSignal, ...]) -> None: ...
    def load_pending_signals(self) -> tuple[PendingSignal, ...]: ...
    def save_pending_signal(self, pending: PendingSignal) -> None: ...
    def load_pending_signal(self) -> PendingSignal | None: ...
    def confirm_pending_signal(
        self,
        actual_price: float,
        actual_shares: int,
        confirmed_at: datetime,
        ticker: str | None = None,
    ) -> None: ...
    def cancel_pending_signal(self, ticker: str | None = None) -> None: ...
    # Funnel log（ユニバース健全性の監視・2026-07-09）
    def append_funnel_log(
        self, run_date: date, universe: int, valid_tickers: int,
        screener_passed: int, signal_candidates: int, signals_sent: int,
    ) -> None: ...
    def load_recent_screener_counts(self, n: int = 20) -> list[int]: ...
    # Slippage log（想定 vs 実約定・2026-07-11 A-3）
    def append_slippage(
        self, *, log_date: date, ticker: str, side: str,
        intended_price: Decimal, actual_price: Decimal, shares: int,
        slippage_bps: Decimal, slippage_jpy: Decimal,
        run_id: str = "", note: str = "",
    ) -> None: ...
    def load_slippage_rows(self) -> list[dict[str, str]]: ...
    # Weekly snapshot（週次サマリー・2026-07-11 A-4）
    def append_weekly_snapshot(
        self, *, log_date: date, equity_jpy: Decimal, topix_close: Decimal,
        cumulative_pnl_jpy: Decimal, cb_net_loss_jpy: Decimal, note: str = "",
    ) -> None: ...
    def load_weekly_snapshots(self) -> list[dict[str, str]]: ...
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
