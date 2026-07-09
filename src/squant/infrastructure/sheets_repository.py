"""Google Sheets state repository — maps domain models ↔ sheet rows."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from squant.config.constants import (
    SHEET_CIRCUIT_BREAKER,
    SHEET_FUNNEL_LOG,
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


def _parse_state_safe(raw: str) -> SystemState:
    """Parse SystemState; on unknown/empty value log critical error and return IDLE."""
    if not raw:
        logger.warning("Empty system state in Sheets — defaulting to IDLE (manual review needed)")
        return SystemState.IDLE
    try:
        return SystemState(raw)
    except ValueError:
        logger.error(
            f"Unknown system state '{raw}' in Sheets — defaulting to IDLE. "
            "Manual review of the portfolio sheet is required."
        )
        return SystemState.IDLE
_DT_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _d(s: str) -> date:
    return datetime.strptime(s, _DATE_FMT).date()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _str(v: Any) -> str:
    return "" if v is None else str(v)


# ── Schema definitions (column order) ─────────────────────────────────────────

_PORTFOLIO_HEADER = [
    # Single-position display columns (first held position) — kept for at-a-glance
    # readability; multi-position truth is in positions_json / settle_dates_csv.
    "state", "cash_jpy", "ticker", "shares",
    "entry_price", "intended_entry_price", "entry_date",
    "stop_loss_price", "trailing_stop_price", "highest_price_since_entry",
    "time_stop_date", "settle_date", "last_run_id", "cumulative_pnl_jpy",
    # Multi-position persistence (2026-05-30):
    "positions_json", "settle_dates_csv",
]


def _serialize_positions(positions: tuple[Position, ...]) -> str:
    """Encode a tuple of Position into a single JSON string for the positions_json column."""
    if not positions:
        return ""
    items = [
        {
            "ticker": p.ticker,
            "shares": p.shares,
            "entry_price": str(p.entry_price),
            "intended_entry_price": str(p.intended_entry_price),
            "entry_date": p.entry_date.strftime(_DATE_FMT),
            "stop_loss_price": str(p.stop_loss_price),
            "trailing_stop_price": str(p.trailing_stop_price),
            "highest_price_since_entry": str(p.highest_price_since_entry),
            "time_stop_date": p.time_stop_date.strftime(_DATE_FMT),
        }
        for p in positions
    ]
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _deserialize_positions(raw: str) -> tuple[Position, ...]:
    if not raw:
        return ()
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.error(f"positions_json malformed — skipping: {raw[:200]}")
        return ()
    out: list[Position] = []
    for item in items:
        try:
            out.append(Position(
                ticker=item["ticker"],
                shares=int(item["shares"]),
                entry_price=Decimal(item["entry_price"]),
                intended_entry_price=Decimal(item["intended_entry_price"]),
                entry_date=_d(item["entry_date"]),
                stop_loss_price=Decimal(item["stop_loss_price"]),
                trailing_stop_price=Decimal(item["trailing_stop_price"]),
                highest_price_since_entry=Decimal(item["highest_price_since_entry"]),
                time_stop_date=_d(item["time_stop_date"]),
            ))
        except (KeyError, ValueError) as e:
            logger.error(f"positions_json item invalid — skipping one entry: {e}")
    return tuple(out)


def _serialize_settle_dates(settle_dates: tuple[date, ...]) -> str:
    return ",".join(d.strftime(_DATE_FMT) for d in settle_dates)


def _deserialize_settle_dates(raw: str) -> tuple[date, ...]:
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    out: list[date] = []
    for p in parts:
        try:
            out.append(_d(p))
        except ValueError:
            logger.warning(f"settle_dates_csv invalid entry: {p}")
    return tuple(out)

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
                cash_jpy=Decimal("600000"),  # 2026-07-05 最終増額後の既定値
            )
        # Pad to header length so dict-zip works even with the new JSON columns absent
        raw = list(rows[1]) + [""] * (len(_PORTFOLIO_HEADER) - len(rows[1]))
        r = dict(zip(_PORTFOLIO_HEADER, raw))  # noqa: B905

        # Prefer the multi-position JSON column. If it's missing or malformed, fall
        # back to the single-position display columns (back-compat with old sheets).
        positions = _deserialize_positions(r.get("positions_json", ""))
        if not positions and r.get("ticker"):
            positions = (Position(
                ticker=r["ticker"],
                shares=int(r["shares"] or 0),
                entry_price=Decimal(r["entry_price"] or "0"),
                intended_entry_price=Decimal(r["intended_entry_price"] or "0"),
                entry_date=_d(r["entry_date"]),
                stop_loss_price=Decimal(r["stop_loss_price"] or "0"),
                trailing_stop_price=Decimal(r["trailing_stop_price"] or "0"),
                highest_price_since_entry=Decimal(r["highest_price_since_entry"] or "0"),
                time_stop_date=_d(r["time_stop_date"]),
            ),)

        settle_dates = _deserialize_settle_dates(r.get("settle_dates_csv", ""))
        if not settle_dates and r.get("settle_date"):
            settle_dates = (_d(r["settle_date"]),)

        return PortfolioState(
            state=_parse_state_safe(r.get("state", "")),
            cash_jpy=Decimal(r["cash_jpy"] or "600000"),
            positions=positions,
            settle_dates=settle_dates,
            last_run_id=r.get("last_run_id", ""),
            cumulative_pnl_jpy=Decimal(r.get("cumulative_pnl_jpy") or "0"),
        )

    def save_portfolio(self, state: PortfolioState) -> None:
        # Display columns reflect the first held position (if any) for at-a-glance
        # readability; the authoritative storage is positions_json / settle_dates_csv.
        p = state.positions[0] if state.positions else None
        first_settle = state.settle_dates[0] if state.settle_dates else None
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
            first_settle.strftime(_DATE_FMT) if first_settle else "",
            state.last_run_id,
            str(state.cumulative_pnl_jpy),
            _serialize_positions(state.positions),
            _serialize_settle_dates(state.settle_dates),
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

    # ── Pending signals (multi-position 2026-05-30) ────────────────────────────

    @staticmethod
    def _row_from_pending(pending: PendingSignal) -> list[str]:
        s = pending.signal
        return [
            s.generated_at.isoformat(),
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

    @staticmethod
    def _pending_from_row(r: dict[str, str]) -> PendingSignal | None:
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

    def save_pending_signals(self, pendings: tuple[PendingSignal, ...]) -> None:
        rows = [_PENDING_HEADER]
        for p in pendings:
            rows.append(self._row_from_pending(p))
        self._c.overwrite_sheet(SHEET_PENDING_SIGNALS, rows)

    def load_pending_signals(self) -> tuple[PendingSignal, ...]:
        rows = self._c.read_all(SHEET_PENDING_SIGNALS)
        if len(rows) < 2:
            return ()
        out: list[PendingSignal] = []
        for raw in rows[1:]:
            if not raw or not raw[0]:
                continue
            r = dict(zip(_PENDING_HEADER, raw))  # noqa: B905
            ps = self._pending_from_row(r)
            if ps is not None:
                out.append(ps)
        return tuple(out)

    # Back-compat single-signal methods
    def save_pending_signal(self, pending: PendingSignal) -> None:
        self.save_pending_signals((pending,))

    def load_pending_signal(self) -> PendingSignal | None:
        items = self.load_pending_signals()
        return items[0] if items else None

    def confirm_pending_signal(
        self,
        actual_price: float,
        actual_shares: int,
        confirmed_at: datetime,
        ticker: str | None = None,
    ) -> None:
        rows = self._c.read_all(SHEET_PENDING_SIGNALS)
        if len(rows) < 2:
            return
        col = _PENDING_HEADER
        ticker_col = col.index("ticker")
        # Find the target row (by ticker, or the first valid one if not specified)
        target_idx = None
        for idx, raw in enumerate(rows[1:], start=2):
            if not raw or len(raw) <= ticker_col or not raw[ticker_col]:
                continue
            if ticker is None or raw[ticker_col] == ticker:
                target_idx = idx
                break
        if target_idx is None:
            return
        r = list(rows[target_idx - 1])
        # Ensure row is wide enough
        while len(r) < len(col):
            r.append("")
        r[col.index("execution_status")] = ExecutionStatus.FILLED.value
        r[col.index("actual_entry_price")] = str(actual_price)
        r[col.index("actual_shares")] = str(actual_shares)
        r[col.index("confirmed_at")] = confirmed_at.isoformat()
        self._c.update_row(SHEET_PENDING_SIGNALS, target_idx, r)

    def cancel_pending_signal(self, ticker: str | None = None) -> None:
        rows = self._c.read_all(SHEET_PENDING_SIGNALS)
        if len(rows) < 2:
            return
        col = _PENDING_HEADER
        ticker_col = col.index("ticker")
        status_col = col.index("execution_status")
        # If ticker is None, cancel all. Otherwise cancel only the matching row.
        for idx, raw in enumerate(rows[1:], start=2):
            if not raw or len(raw) <= ticker_col or not raw[ticker_col]:
                continue
            if ticker is None or raw[ticker_col] == ticker:
                r = list(raw)
                while len(r) < len(col):
                    r.append("")
                r[status_col] = ExecutionStatus.CANCELLED.value
                self._c.update_row(SHEET_PENDING_SIGNALS, idx, r)
                if ticker is not None:
                    return

    # ── Funnel log（ユニバース健全性の監視・改善提案 A2）────────────────────────

    _FUNNEL_HEADER = [
        "run_date", "universe", "valid_tickers", "screener_passed",
        "signal_candidates", "signals_sent",
    ]

    def append_funnel_log(
        self, run_date: date, universe: int, valid_tickers: int,
        screener_passed: int, signal_candidates: int, signals_sent: int,
    ) -> None:
        """日次スクリーニングファネルを funnel_log タブに追記する。"""
        ws = self._c.get_or_create_sheet(SHEET_FUNNEL_LOG)
        rows = self._c.read_all(SHEET_FUNNEL_LOG)
        if not rows:
            ws.append_row(self._FUNNEL_HEADER)
        self._c.append_row(SHEET_FUNNEL_LOG, [
            run_date.strftime(_DATE_FMT), str(universe), str(valid_tickers),
            str(screener_passed), str(signal_candidates), str(signals_sent),
        ])

    def load_recent_screener_counts(self, n: int = 20) -> list[int]:
        """直近 n 営業日分のスクリーニング通過数（新しい順ではなく記録順）。"""
        rows = self._c.read_all(SHEET_FUNNEL_LOG)
        if len(rows) < 2:
            return []
        col = self._FUNNEL_HEADER.index("screener_passed")
        counts: list[int] = []
        for r in rows[1:]:
            if len(r) > col and r[col].strip().isdigit():
                counts.append(int(r[col]))
        return counts[-n:]

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
