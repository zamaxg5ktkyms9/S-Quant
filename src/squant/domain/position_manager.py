"""Position exit rule evaluation — pure business logic, no I/O.

- トレーリングストップ基準は「直近高値 - ATR_TRAILING_MULTIPLIER×ATR」（ラチェット成立）
- ザラ場約定モード（intraday_high/low を渡せば逆指値・指値の発動を再現）
- TP は TARGET_PROFIT_RATE=None で無効（2026-07-05 の本番デフォルト）
"""

from datetime import date
from decimal import Decimal

import pandas as pd

from squant.config.constants import (
    ATR_PERIOD,
    ATR_TRAILING_MULTIPLIER,
    GAP_UP_CANCEL_THRESHOLD,
    TIME_STOP_TRADING_DAYS,
)
from squant.domain.enums import ExitReason
from squant.domain.indicators import atr
from squant.domain.models import ExitDecision, Position
from squant.domain.quantity_calculator import compute_take_profit_price
from squant.utils.jst import count_trading_days


def evaluate_exit(
    position: Position,
    today: date,
    latest_close: Decimal,
    high_series: pd.Series,
    low_series: pd.Series,
    close_series: pd.Series,
    intraday_high: Decimal | None = None,
    intraday_low: Decimal | None = None,
) -> ExitDecision:
    """ポジションの出口判定。

    ザラ場モード（intraday_high/low を渡す）:
      - 当日high/lowがストップ・利確価格を到達したかでOCO発動を判定
      - exit_price フィールドに約定価格（ストップ/利確価格そのもの）を返す
    終値モード（intraday_*=None）:
      - 旧来通り終値で判定。exit_price は None（呼び出し側で latest_close 使用）

    Exit priority:
      1. Hard stop-loss (ザラ場発動 or 終値到達)
      2. Trailing stop (上記と同じ)
      3. Take-profit（TARGET_PROFIT_RATE=None の場合はスキップ = 本番デフォルト）
      4. Time stop (5営業日経過時、終値で決済前提)
    """
    days_held = count_trading_days(position.entry_date, today)

    # --- トレーリングストップ更新（直近高値 - 2.5×ATR）---
    today_high_for_update = intraday_high if intraday_high is not None else latest_close
    new_highest = max(position.highest_price_since_entry, today_high_for_update)
    updated_trailing = _compute_trailing_stop(
        position, new_highest, high_series, low_series, close_series
    )
    effective_stop = max(position.stop_loss_price, updated_trailing)
    tp_price = compute_take_profit_price(position.entry_price)

    intraday_mode = intraday_high is not None and intraday_low is not None

    # --- 1. ハードストップロス（最優先） ---
    if intraday_mode and intraday_low <= position.stop_loss_price:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.STOP_LOSS,
            note=f"Intraday low ¥{intraday_low} ≤ stop ¥{position.stop_loss_price}",
            updated_trailing_stop=updated_trailing,
            exit_price=position.stop_loss_price,
        )
    if not intraday_mode and latest_close <= position.stop_loss_price:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.STOP_LOSS,
            note=f"Close ¥{latest_close} ≤ stop ¥{position.stop_loss_price}",
            updated_trailing_stop=updated_trailing,
        )

    # --- 2. トレーリングストップ ---
    if intraday_mode and intraday_low <= effective_stop:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.TRAILING_STOP,
            note=f"Intraday low ¥{intraday_low} ≤ trailing ¥{effective_stop}",
            updated_trailing_stop=updated_trailing,
            exit_price=effective_stop,
        )
    if not intraday_mode and latest_close <= effective_stop:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.TRAILING_STOP,
            note=f"Close ¥{latest_close} ≤ trailing ¥{effective_stop}",
            updated_trailing_stop=updated_trailing,
        )

    # --- 3. 利確（tp_price=None なら利確なし = 本番デフォルト） ---
    if tp_price is not None:
        if intraday_mode and intraday_high >= tp_price:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TAKE_PROFIT,
                note=f"Intraday high ¥{intraday_high} ≥ TP ¥{round(tp_price, 1)}",
                updated_trailing_stop=updated_trailing,
                exit_price=tp_price,
            )
        if not intraday_mode and latest_close >= tp_price:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TAKE_PROFIT,
                note=f"Close ¥{latest_close} ≥ TP ¥{round(tp_price, 1)}",
                updated_trailing_stop=updated_trailing,
            )

    # --- 4. タイムストップ（5営業日経過）---
    # 設計上は「5日目の引け後通知→翌朝成行」だが、バックテスト簡略化として
    # 5日目の終値で約定とみなす（ザラ場モードでも終値ベース）。
    if days_held >= TIME_STOP_TRADING_DAYS:
        return ExitDecision(
            should_exit=True,
            reason=ExitReason.TIME_STOP,
            note=f"Held {days_held} trading days (limit {TIME_STOP_TRADING_DAYS})",
            updated_trailing_stop=updated_trailing,
            exit_price=latest_close if intraday_mode else None,
        )

    return ExitDecision(
        should_exit=False,
        reason=None,
        note=f"HOLD ({days_held} days)",
        updated_trailing_stop=updated_trailing,
    )


def _compute_trailing_stop(
    position: Position,
    highest_price: Decimal,
    high_series: pd.Series,
    low_series: pd.Series,
    close_series: pd.Series,
) -> Decimal:
    """トレーリングストップ = max(現在値, 直近高値 - 乗数×ATR)。下方にはラチェットしない。"""
    atr_series = atr(high_series, low_series, close_series, period=ATR_PERIOD)
    if atr_series.empty or pd.isna(atr_series.iloc[-1]):
        return position.trailing_stop_price

    latest_atr = Decimal(str(round(float(atr_series.iloc[-1]), 2)))
    new_stop = highest_price - ATR_TRAILING_MULTIPLIER * latest_atr
    return max(position.trailing_stop_price, new_stop)


def should_cancel_gap_up(
    intended_price: Decimal,
    actual_open: Decimal,
    threshold: Decimal = GAP_UP_CANCEL_THRESHOLD,
) -> bool:
    """True if the opening gap exceeds the cancellation threshold."""
    return actual_open > intended_price * (1 + threshold)
