"""Google Sheets state repository — maps domain models ↔ sheet rows."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from squant.config.constants import (
    SHEET_CIRCUIT_BREAKER,
    SHEET_PENDING_SIGNALS,
    SHEET_PORTFOLIO,
    SHEET_RECENT_SALES,
    SHEET_RUN_LOG,
    SHEET_TRADES,
)
from squant.domain.enums import ExecutionStatus, SystemState
from squant.domain.models import (
    CircuitBreakerStatus,
    PendingSignal,
    PortfolioState,
    Position,
    RecentSale,
    RunRecord,
    Signal,
    TradeRecord,
)
from squant.infrastructure.sheets_client import GoogleSheetsClient
from squant.utils.jst import now_jst
from squant.utils.logging import get_logger

logger = get_logger(__name__)

_DATE_FMT = "%Y-%m-%d"
_DT_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _d(s: str) -> date:
    return datetime.strptime(s, _DATE_FMT).date()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _str(v: Any) -> str:
    return "" if v is None else str(v)


# ── Schema definitions (column order) ─────────────────────────────────────────

_PORTFOLIO_HEADER = [
    "state", "cash_jpy", "ticker", "shares",
    "entry_price", "intended_entry_price", "entry_date",
    "stop_loss_price", "trailing_stop_price", "highest_price_since_entry",
    "time_stop_date", "settle_date", "last_run_id", "cumulative_pnl_jpy",
]

_TRADES_HEADER = [
    "run_id", "ticker", "side", "shares", "price",
    "executed_at", "pnl_jpy", "exit_reason",
]

_CB_HEADER = ["is_tripped", "cumulative_loss_jpy", "tripped_at"]

_RUN_LOG_HEADER = ["run_id", "run_date", "status", "note", "completed_at"]

_PENDING_HEADER = [
    "run_id", "ticker", "reference_price", "shares",
    "cancel_above_price", "stop_loss_price", "rsi",
    "reason", "generated_at", "execution_status",
    "actual_entry_price", "actual_shares", "confirmed_at",
]

_RECENT_SALES_HEADER = ["ticker", "sell_date", "settlement_date"]


class SheetsStateRepository:
    def __init__(self, client: GoogleSheetsClient) -> None:
        self._c = client

    # ── Portfolio ──────────────────────────────────────────────────────────────

    def load_portfolio(self) -> PortfolioState:
        rows = self._c.read_all(SHEET_PORTFOLIO)
        if len(rows) < 2 or not rows[1][0]:
            return PortfolioState(
                state=SystemState.IDLE,
                cash_jpy=Decimal("100000"),
            )
        r = dict(zip(_PORTFOLIO_HEADER, rows[1]))  # noqa: B905
        position = None
        if r.get("ticker"):
            position = Position(
                ticker=r["ticker"],
                shares=int(r["shares"] or 0),
                entry_price=Decimal(r["entry_price"] or "0"),
                intended_entry_price=Decimal(r["intended_entry_price"] or "0"),
                entry_date=_d(r["entry_date"]),
                stop_loss_price=Decimal(r["stop_loss_price"] or "0"),
                trailing_stop_price=Decimal(r["trailing_stop_price"] or "0"),
                highest_price_since_entry=Decimal(r["highest_price_since_entry"] or "0"),
                time_stop_date=_d(r["time_stop_date"]),
            )
        return PortfolioState(
            state=SystemState(r["state"]),
            cash_jpy=Decimal(r["cash_jpy"] or "100000"),
            position=position,
            settle_date=_d(r["settle_date"]) if r.get("settle_date") else None,
            last_run_id=r.get("last_run_id", ""),
            cumulative_pnl_jpy=Decimal(r.get("cumulative_pnl_jpy") or "0"),
        )

    def save_portfolio(self, state: PortfolioState) -> None:
        p = state.position
        row = [
            state.state.value,
            str(state.cash_jpy),
            p.ticker if p else "",
            str(p.shares) if p else "",
            str(p.entry_price) if p else "",
            str(p.intended_entry_price) if p else "",
            p.entry_date.strftime(_DATE_FMT) if p else "",
            str(p.stop_loss_price) if p else "",
            str(p.trailing_stop_price) if p else "",
            str(p.highest_price_since_entry) if p else "",
            p.time_stop_date.strftime(_DATE_FMT) if p else "",
            state.settle_date.strftime(_DATE_FMT) if state.settle_date else "",
            state.last_run_id,
            str(state.cumulative_pnl_jpy),
        ]
        rows = self._c.read_all(SHEET_PORTFOLIO)
        if len(rows) < 1 or rows[0] != _PORTFOLIO_HEADER:
            self._c.overwrite_sheet(SHEET_PORTFOLIO, [_PORTFOLIO_HEADER, row])
        else:
            self._c.update_row(SHEET_PORTFOLIO, 2, row)

    # ── Trades ─────────────────────────────────────────────────────────────────

    def append_trade(self, trade: TradeRecord) -> None:
        rows = self._c.read_all(SHEET_TRADES)
        if not rows or rows[0] != _TRADES_HEADER:
            self._c.overwrite_sheet(SHEET_TRADES, [_TRADES_HEADER])
        row = [
            trade.run_id,
            trade.ticker,
            trade.side.value,
            str(trade.shares),
            str(trade.price),
            trade.executed_at.isoformat(),
            str(trade.pnl_jpy) if trade.pnl_jpy is not None else "",
            trade.exit_reason.value if trade.exit_reason else "",
        ]
        self._c.append_row(SHEET_TRADES, row)

    # ── Pending signal ─────────────────────────────────────────────────────────

    def save_pending_signal(self, pending: PendingSignal) -> None:
        s = pending.signal
        row = [
            s.generated_at.isoformat(),  # reuse as run_id proxy
            s.ticker,
            str(s.reference_price),
            str(s.shares),
            str(s.cancel_above_price),
            str(s.stop_loss_price),
            str(round(s.rsi, 2)),
            s.reason,
            s.generated_at.isoformat(),
            pending.execution_status.value,
            str(pending.actual_entry_price) if pending.actual_entry_price else "",
            str(pending.actual_shares) if pending.actual_shares else "",
            pending.confirmed_at.isoformat() if pending.confirmed_at else "",
        ]
        self._c.overwrite_sheet(SHEET_PENDING_SIGNALS, [_PENDING_HEADER, row])

    def load_pending_signal(self) -> PendingSignal | None:
        rows = self._c.read_all(SHEET_PENDING_SIGNALS)
        if len(rows) < 2 or not rows[1][0]:
            return None
        r = dict(zip(_PENDING_HEADER, rows[1]))  # noqa: B905
        if not r.get("ticker"):
            return None
        signal = Signal(
            ticker=r["ticker"],
            reference_price=Decimal(r["reference_price"]),
            shares=int(r["shares"]),
            cancel_above_price=Decimal(r["cancel_above_price"]),
            stop_loss_price=Decimal(r["stop_loss_price"]),
            rsi=float(r["rsi"]),
            reason=r["reason"],
            generated_at=_dt(r["generated_at"]),
        )
        return PendingSignal(
            signal=signal,
            execution_status=ExecutionStatus(r.get("execution_status", "PENDING")),
            actual_entry_price=Decimal(r["actual_entry_price"]) if r.get("actual_entry_price") else None,
            actual_shares=int(r["actual_shares"]) if r.get("actual_shares") else None,
            confirmed_at=_dt(r["confirmed_at"]) if r.get("confirmed_at") else None,
        )

    def confirm_pending_signal(
        self, actual_price: float, actual_shares: int, confirmed_at: datetime
    ) -> None:
        rows = self._c.read_all(SHEET_PENDING_SIGNALS)
        if len(rows) < 2:
            return
        r = list(rows[1])
        col = _PENDING_HEADER
        r[col.index("execution_status")] = ExecutionStatus.FILLED.value
        r[col.index("actual_entry_price")] = str(actual_price)
        r[col.index("actual_shares")] = str(actual_shares)
        r[col.index("confirmed_at")] = confirmed_at.isoformat()
        self._c.update_row(SHEET_PENDING_SIGNALS, 2, r)

    def cancel_pending_signal(self) -> None:
        rows = self._c.read_all(SHEET_PENDING_SIGNALS)
        if len(rows) < 2:
            return
        r = list(rows[1])
        r[_PENDING_HEADER.index("execution_status")] = ExecutionStatus.CANCELLED.value
        self._c.update_row(SHEET_PENDING_SIGNALS, 2, r)

    # ── Circuit breaker ────────────────────────────────────────────────────────

    def load_circuit_breaker(self) -> CircuitBreakerStatus:
        rows = self._c.read_all(SHEET_CIRCUIT_BREAKER)
        if len(rows) < 2 or not rows[1][0]:
            return CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal("0"))
        r = dict(zip(_CB_HEADER, rows[1]))  # noqa: B905
        return CircuitBreakerStatus(
            is_tripped=r.get("is_tripped", "").lower() == "true",
            cumulative_loss_jpy=Decimal(r.get("cumulative_loss_jpy") or "0"),
            tripped_at=_dt(r["tripped_at"]) if r.get("tripped_at") else None,
        )

    def save_circuit_breaker(self, status: CircuitBreakerStatus) -> None:
        row = [
            str(status.is_tripped),
            str(status.cumulative_loss_jpy),
            status.tripped_at.isoformat() if status.tripped_at else "",
        ]
        self._c.overwrite_sheet(SHEET_CIRCUIT_BREAKER, [_CB_HEADER, row])

    # ── Recent sales (差金決済防止) ─────────────────────────────────────────────

    def load_recent_sales(self) -> list[RecentSale]:
        rows = self._c.read_all(SHEET_RECENT_SALES)
        if len(rows) < 2:
            return []
        sales = []
        for row in rows[1:]:
            if not row[0]:
                continue
            r = dict(zip(_RECENT_SALES_HEADER, row))  # noqa: B905
            sales.append(
                RecentSale(
                    ticker=r["ticker"],
                    sell_date=_d(r["sell_date"]),
                    settlement_date=_d(r["settlement_date"]),
                )
            )
        return sales

    def append_recent_sale(self, sale: RecentSale) -> None:
        rows = self._c.read_all(SHEET_RECENT_SALES)
        if not rows or rows[0] != _RECENT_SALES_HEADER:
            self._c.overwrite_sheet(SHEET_RECENT_SALES, [_RECENT_SALES_HEADER])
        self._c.append_row(
            SHEET_RECENT_SALES,
            [
                sale.ticker,
                sale.sell_date.strftime(_DATE_FMT),
                sale.settlement_date.strftime(_DATE_FMT),
            ],
        )

    # ── Run log (idempotency) ──────────────────────────────────────────────────

    def has_run_today(self, today: date) -> bool:
        rows = self._c.read_all(SHEET_RUN_LOG)
        for row in rows[1:]:
            if len(row) >= 2 and row[1] == today.strftime(_DATE_FMT) and row[2] == "success":
                return True
        return False

    def mark_run_complete(self, record: RunRecord) -> None:
        rows = self._c.read_all(SHEET_RUN_LOG)
        if not rows or rows[0] != _RUN_LOG_HEADER:
            self._c.overwrite_sheet(SHEET_RUN_LOG, [_RUN_LOG_HEADER])
        self._c.append_row(
            SHEET_RUN_LOG,
            [
                record.run_id,
                record.run_date.strftime(_DATE_FMT),
                record.status,
                record.note,
                now_jst().isoformat(),
            ],
        )
