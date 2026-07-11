"""Property-based tests (Hypothesis) for 資金系不変条件 — V-3 検証強化パッケージ.

mutation testing で数値を1つ変えれば通ってしまう穴を、具体例ではなく
「どんな入力でも成り立つべき不変条件」で塞ぐ。ここで検証するのは資金保全に
直結する3つの性質:

1. compute_quantity は予算を超過する株数を決して返さない。
2. evaluate_exit が返す trailing stop は単調非減少（入力の trailing を下回らない）。
3. circuit_breaker.update_after_trade で勝ちトレードは純損失を増やさない。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from squant.config.constants import GAP_UP_CANCEL_THRESHOLD, SHARES_PER_UNIT
from squant.domain.circuit_breaker import update_after_trade
from squant.domain.enums import ExitReason, OrderSide
from squant.domain.exceptions import InsufficientCapitalError
from squant.domain.models import CircuitBreakerStatus, Position, TradeRecord
from squant.domain.position_manager import evaluate_exit
from squant.domain.quantity_calculator import compute_quantity

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 5, 11, 20, 0, tzinfo=JST)


# ── 不変条件1: 予算超過の株数を返さない ────────────────────────────────

@given(
    prev_close=st.integers(min_value=1, max_value=100_000),
    cash=st.integers(min_value=0, max_value=10_000_000),
    budget=st.integers(min_value=1, max_value=10_000_000),
)
def test_quantity_never_exceeds_budget(prev_close: int, cash: int, budget: int):
    """最悪ケース執行コスト（prev_close×(1+gap)×qty）が min(cash, budget) を超えない。

    超過するくらいなら InsufficientCapitalError を投げるのが正しい挙動。
    さらに qty は常に単元（100株）の倍数・正の値であること。
    """
    pc = Decimal(prev_close)
    c = Decimal(cash)
    b = Decimal(budget)
    gap = GAP_UP_CANCEL_THRESHOLD
    try:
        qty = compute_quantity(available_cash=c, prev_close=pc, gap_up_threshold=gap, budget=b)
    except InsufficientCapitalError:
        return  # 1単元も買えない場合は例外が正しい

    effective_budget = min(c, b)
    worst_case_cost = pc * (1 + gap) * qty
    assert worst_case_cost <= effective_budget, (
        f"over-budget: cost={worst_case_cost} > budget={effective_budget} "
        f"(prev_close={pc}, qty={qty})"
    )
    assert qty > 0
    assert qty % SHARES_PER_UNIT == 0


# ── 不変条件2: trailing stop は単調非減少（下がらない）──────────────────

def _make_position(entry: int, trailing: int, highest: int) -> Position:
    ep = Decimal(entry)
    return Position(
        ticker="1234.T",
        shares=100,
        entry_price=ep,
        intended_entry_price=ep,
        entry_date=date(2026, 5, 11),
        stop_loss_price=ep * Decimal("0.975"),
        trailing_stop_price=Decimal(trailing),
        highest_price_since_entry=Decimal(max(highest, entry)),
        time_stop_date=date(2026, 5, 18),
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    entry=st.integers(min_value=200, max_value=2000),
    trailing_below=st.integers(min_value=1, max_value=50),  # trailing = entry×(1 - x%) 相当
    prices=st.lists(
        st.integers(min_value=100, max_value=3000), min_size=20, max_size=40
    ),
)
def test_trailing_stop_never_decreases(entry: int, trailing_below: int, prices: list[int]):
    """evaluate_exit が返す updated_trailing_stop は、入力ポジションの
    trailing_stop_price を決して下回らない（ラチェットは上方向のみ）。
    """
    trailing = int(entry * (1 - trailing_below / 100))
    trailing = max(trailing, 1)
    pos = _make_position(entry, trailing, highest=max(prices))

    idx = pd.bdate_range(end="2026-05-12", periods=len(prices))
    close = pd.Series([float(p) for p in prices], index=idx, dtype=float)
    high = close + 5.0
    low = close - 5.0

    decision = evaluate_exit(pos, date(2026, 5, 12), Decimal(prices[-1]), high, low, close)
    assert decision.updated_trailing_stop is not None
    assert decision.updated_trailing_stop >= pos.trailing_stop_price, (
        f"trailing dropped: {decision.updated_trailing_stop} < {pos.trailing_stop_price}"
    )
    # 出口理由がトレーリングなら約定価格は必ずトレーリング水準以上でストップ以上
    if decision.reason == ExitReason.TRAILING_STOP and decision.exit_price is not None:
        assert decision.exit_price >= pos.stop_loss_price


# ── 不変条件3: 勝ちトレードは純損失を増やさない ────────────────────────

def _win_trade(pnl: int) -> TradeRecord:
    return TradeRecord(
        ticker="1234.T",
        side=OrderSide.SELL,
        shares=100,
        price=Decimal("500"),
        executed_at=NOW,
        pnl_jpy=Decimal(pnl),
    )


@given(
    old_cumulative=st.integers(min_value=-500_000, max_value=500_000),
    win_pnl=st.integers(min_value=0, max_value=500_000),
    threshold=st.integers(min_value=10_000, max_value=200_000),
)
def test_winning_trade_never_increases_net_loss(
    old_cumulative: int, win_pnl: int, threshold: int
):
    """pnl_jpy >= 0（勝ち or 損益ゼロ）のトレードは純累積損失を増やさない。
    かつ、閾値未満から始まる限り勝ちトレード単独ではサーキットブレーカーを発動させない。
    """
    status = CircuitBreakerStatus(is_tripped=False, cumulative_loss_jpy=Decimal(old_cumulative))
    new = update_after_trade(status, _win_trade(win_pnl), threshold=Decimal(threshold))

    assert new.cumulative_loss_jpy <= status.cumulative_loss_jpy
    if old_cumulative < threshold:
        assert new.is_tripped is False
