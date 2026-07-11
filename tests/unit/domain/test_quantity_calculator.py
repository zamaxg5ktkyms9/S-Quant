"""Tests for quantity calculator (改訂版: 単元株100株単位、スプレッド0)."""

from decimal import Decimal

import pytest

from squant.config.constants import SHARES_PER_UNIT
from squant.domain.exceptions import InsufficientCapitalError
from squant.domain.quantity_calculator import (
    compute_cancel_threshold,
    compute_net_pnl,
    compute_quantity,
    compute_stop_loss_price,
    compute_take_profit_price,
)


class TestComputeQuantity:
    def test_basic_calculation_rounds_to_unit(self):
        # budget=100000, close=500, threshold=0.02
        # worst_case = 510, raw = floor(100000/510) = 196
        # 単元100株丸め → 100株
        qty = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("500"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty == 100
        assert qty % SHARES_PER_UNIT == 0

    def test_max_price_phase1(self):
        # close=900, worst_case=918, raw=floor(100000/918)=108 → 100株
        qty = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("900"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty == 100

    def test_min_price_phase1(self):
        # close=100, worst_case=102, raw=floor(100000/102)=980 → 900株
        qty = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("100"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty == 900

    def test_capital_never_exceeded_at_gap_up_threshold(self):
        """qty * prev_close * (1+threshold) <= budget always."""
        budget = Decimal("100000")
        threshold = Decimal("0.02")
        for price in [100, 200, 300, 400, 500, 600, 700, 800, 900]:
            prev_close = Decimal(str(price))
            qty = compute_quantity(
                available_cash=budget,
                prev_close=prev_close,
                gap_up_threshold=threshold,
                budget=budget,
            )
            worst_case_cost = prev_close * (1 + threshold) * qty
            assert worst_case_cost <= budget, (
                f"Over-budget at price={price}: cost={worst_case_cost} > {budget}"
            )

    def test_always_multiple_of_unit(self):
        for price in [100, 250, 500, 750, 900]:
            qty = compute_quantity(
                available_cash=Decimal("100000"),
                prev_close=Decimal(str(price)),
                gap_up_threshold=Decimal("0.02"),
                budget=Decimal("100000"),
            )
            assert qty % SHARES_PER_UNIT == 0

    def test_uses_min_of_cash_and_budget(self):
        # Cash ¥30,000 / 予算 ¥100,000 / 株価 ¥250 → 単元100株分(¥255×100=¥25,500)買える
        qty_limited = compute_quantity(
            available_cash=Decimal("30000"),
            prev_close=Decimal("250"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        qty_full = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("250"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty_limited == 100  # ¥30,000では1単元のみ
        assert qty_full == 300     # ¥100,000では3単元
        assert qty_limited < qty_full

    def test_raises_on_insufficient_capital_for_one_unit(self):
        # ¥900の銘柄を100株 = ¥90,000 が必要。予算¥5,000では1単元買えない
        with pytest.raises(InsufficientCapitalError, match="Insufficient capital"):
            compute_quantity(
                available_cash=Decimal("5000"),
                prev_close=Decimal("900"),
                gap_up_threshold=Decimal("0.02"),
                budget=Decimal("100000"),
            )


class TestComputeCancelThreshold:
    def test_basic(self):
        result = compute_cancel_threshold(Decimal("500"), Decimal("0.02"))
        assert result == Decimal("510.00")

    def test_yen_900_stock(self):
        result = compute_cancel_threshold(Decimal("900"), Decimal("0.02"))
        assert result == Decimal("918.00")


class TestComputeStopLossPrice:
    def test_2_5_percent_stop(self):
        result = compute_stop_loss_price(Decimal("500"), Decimal("0.025"))
        assert result == Decimal("487.500")

    def test_stop_below_entry(self):
        entry = Decimal("800")
        stop = compute_stop_loss_price(entry, Decimal("0.025"))
        assert stop < entry


class TestComputeTakeProfitPrice:
    def test_default_tp_disabled_returns_none(self):
        # 2026-07-05: 本番デフォルトは TP なし（TARGET_PROFIT_RATE=None）
        assert compute_take_profit_price(Decimal("500")) is None

    def test_explicit_rate_5pct(self):
        # rate を明示すれば従来どおり計算される（grid search 用の後方互換）
        tp = compute_take_profit_price(Decimal("500"), Decimal("0.05"))
        assert float(tp) == pytest.approx(500 * 1.05, rel=1e-6)

    def test_tp_above_entry(self):
        entry = Decimal("900")
        tp = compute_take_profit_price(entry, Decimal("0.05"))
        assert tp > entry

    def test_explicit_spread_rounds_higher(self):
        """スプレッドを明示すると TP は素の rate より高くなる（互換性）。"""
        entry = Decimal("500")
        tp_no_spread = compute_take_profit_price(entry, Decimal("0.05"), spread_rate=Decimal("0"))
        tp_with_spread = compute_take_profit_price(entry, Decimal("0.05"), spread_rate=Decimal("0.005"))
        assert tp_with_spread > tp_no_spread

    def test_explicit_spread_exact_value(self):
        """spread≠0 の TP 公式を厳密値で固定（grid search バックテスト経路）。

        mutation testing (V-3): spread 経路 entry*(1+spread)*(1+target)/(1-spread) の
        各係数を変えた変異（1→2, *→/, target 係数改変）は方向性アサートでは生き残る。
        厳密値でピン留めして塞ぐ。
        entry=500, target=0.05, spread=0.01 → 500*1.01*1.05/0.99 = 535.60606...
        """
        tp = compute_take_profit_price(Decimal("500"), Decimal("0.05"), spread_rate=Decimal("0.01"))
        assert tp is not None
        assert float(tp) == pytest.approx(500 * 1.01 * 1.05 / 0.99, rel=1e-12)


class TestComputeNetPnl:
    def test_profitable_trade_no_spread(self):
        # 単元株+ゼロ革命: (550-500)*100 = 5000
        pnl = compute_net_pnl(
            entry_price=Decimal("500"),
            exit_price=Decimal("550"),
            shares=100,
        )
        assert pnl == Decimal("5000")

    def test_losing_trade_is_negative(self):
        pnl = compute_net_pnl(
            entry_price=Decimal("500"),
            exit_price=Decimal("480"),
            shares=100,
        )
        assert float(pnl) < 0

    def test_breakeven_at_entry_is_zero_without_spread(self):
        """単元株+ゼロ革命では entry==exit で損益ゼロ。"""
        pnl = compute_net_pnl(
            entry_price=Decimal("500"),
            exit_price=Decimal("500"),
            shares=100,
        )
        assert pnl == Decimal("0")

    def test_with_spread_exact_value(self):
        """spread≠0 の純損益公式を厳密値で固定（バックテスト経路）。

        mutation testing (V-3): spread 経路 (exit*(1-spread) - entry*(1+spread))*shares の
        分岐条件・各係数の変異（== を !=、*→/、1±→2±、- を +、*shares を /shares 等）は
        spread=0 のテストだけでは全て生き残る。厳密値でピン留めして塞ぐ。
        entry=500, exit=550, spread=0.01, shares=100
        effective_entry=505.00, effective_exit=544.50 → (544.50-505.00)*100 = 3950.00
        """
        pnl = compute_net_pnl(
            entry_price=Decimal("500"),
            exit_price=Decimal("550"),
            shares=100,
            spread_rate=Decimal("0.01"),
        )
        assert pnl == Decimal("3950.00")

    def test_spread_reduces_profit_vs_no_spread(self):
        """スプレッドがあると純利益は spread=0 の場合より小さくなる（分岐の意味を固定）。"""
        no_spread = compute_net_pnl(Decimal("500"), Decimal("550"), 100, spread_rate=Decimal("0"))
        with_spread = compute_net_pnl(Decimal("500"), Decimal("550"), 100, spread_rate=Decimal("0.01"))
        assert with_spread < no_spread
