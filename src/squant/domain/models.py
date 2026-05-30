from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from squant.domain.enums import ExecutionStatus, ExitReason, OrderSide, SystemState


@dataclass(frozen=True)
class Position:
    ticker: str
    shares: int
    entry_price: Decimal                   # actual fill price (operator-confirmed)
    intended_entry_price: Decimal          # prev close at signal time
    entry_date: date
    stop_loss_price: Decimal               # fixed: entry_price * (1 - stop_loss_rate)
    trailing_stop_price: Decimal           # ratchets up with price
    highest_price_since_entry: Decimal
    time_stop_date: date                   # entry_date + TIME_STOP_TRADING_DAYS

    @property
    def cost_basis_jpy(self) -> Decimal:
        return self.entry_price * self.shares

    def effective_stop(self) -> Decimal:
        return max(self.stop_loss_price, self.trailing_stop_price)


@dataclass(frozen=True)
class PortfolioState:
    """Phase 1 multi-position portfolio (C strategy adopted 2026-05-29).

    state はその日「主に表現する」フェーズを示すヒント:
      - IDLE: positions / pending_signals / settle_dates すべて空
      - SIGNAL_SENT: pending_signals が1件以上
      - HOLDING: positions が1件以上
      - SETTLING: positions 空 / settle_dates 1件以上
    実運用では複数フェーズが同時併存しうる（例: holding 1件 + pending 1件）。
    state は dispatcher のヒント・Slack 表示用で、各 pipeline は毎日順次実行される。
    """
    state: SystemState
    cash_jpy: Decimal
    positions: tuple[Position, ...] = ()
    pending_signals: tuple["PendingSignal", ...] = ()
    settle_dates: tuple[date, ...] = ()
    last_run_id: str = ""
    cumulative_pnl_jpy: Decimal = Decimal("0")

    # ── 後方互換プロパティ ─────────────────────────────────────────
    @property
    def position(self) -> Position | None:
        """単一 Position アクセス（後方互換、保有が1件以下の場合のみ意味あり）。"""
        return self.positions[0] if self.positions else None

    @property
    def settle_date(self) -> date | None:
        """単一 settle_date アクセス（後方互換、settle_dates が1件以下のときの直近）。"""
        return self.settle_dates[0] if self.settle_dates else None

    # ── 集約・派生情報 ─────────────────────────────────────────────
    @property
    def open_slots(self) -> int:
        """残スロット数。max_positions は Settings から渡す前提で、ここでは未計算。
        呼び出し側で `settings.max_positions - portfolio.in_use_slots` を取る。"""
        return -(len(self.positions) + len(self.pending_signals))  # callerで反転

    @property
    def in_use_slots(self) -> int:
        """既保有 + 約定待ち pending の合計（スロットを消費しているもの）。"""
        return len(self.positions) + len(self.pending_signals)

    @property
    def held_tickers(self) -> set[str]:
        return {p.ticker for p in self.positions} | {ps.signal.ticker for ps in self.pending_signals}


@dataclass(frozen=True)
class Candidate:
    ticker: str
    close: Decimal
    rsi14: float
    volume_surge_ratio: float              # today_volume / 20d_avg_volume
    pbr: float
    market_cap_jpy: float

    def __lt__(self, other: "Candidate") -> bool:
        # Primary sort: RSI ascending (lower = more oversold = better)
        # Secondary: volume_surge_ratio descending
        # Tertiary: PBR ascending
        if self.rsi14 != other.rsi14:
            return self.rsi14 < other.rsi14
        if self.volume_surge_ratio != other.volume_surge_ratio:
            return self.volume_surge_ratio > other.volume_surge_ratio
        return self.pbr < other.pbr


@dataclass(frozen=True)
class Signal:
    ticker: str
    reference_price: Decimal               # prev close (quantity calc basis)
    shares: int
    cancel_above_price: Decimal            # reference_price * (1 + gap_up_threshold)
    stop_loss_price: Decimal               # reference_price * (1 - stop_loss_rate)
    rsi: float
    reason: str
    generated_at: datetime


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None
    note: str
    updated_trailing_stop: Decimal | None = None  # new trailing stop if updated
    exit_price: Decimal | None = None              # ザラ場約定モードでの想定約定価格


@dataclass
class TradeRecord:
    ticker: str
    side: OrderSide
    shares: int
    price: Decimal
    executed_at: datetime
    pnl_jpy: Decimal | None = None
    exit_reason: ExitReason | None = None
    run_id: str = ""


@dataclass(frozen=True)
class CircuitBreakerStatus:
    is_tripped: bool
    cumulative_loss_jpy: Decimal
    tripped_at: datetime | None = None


@dataclass(frozen=True)
class RecentSale:
    ticker: str
    sell_date: date
    settlement_date: date

    def is_settled(self, as_of: date) -> bool:
        return as_of >= self.settlement_date


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    run_date: date
    status: str                            # "success" | "error" | "no-op"
    note: str = ""


@dataclass(frozen=True)
class PendingSignal:
    signal: Signal
    execution_status: ExecutionStatus = ExecutionStatus.PENDING
    actual_entry_price: Decimal | None = None
    actual_shares: int | None = None
    confirmed_at: datetime | None = None
