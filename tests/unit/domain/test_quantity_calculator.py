"""Tests for quantity calculator."""

from decimal import Decimal

import pytest

from squant.domain.exceptions import InsufficientCapitalError
from squant.domain.quantity_calculator import (
    compute_cancel_threshold,
    compute_quantity,
    compute_stop_loss_price,
)


class TestComputeQuantity:
    def test_basic_calculation(self):
        # budget=100000, close=500, threshold=0.02
        # worst_case = 500 * 1.02 = 510
        # qty = floor(100000 / 510) = 196
        qty = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("500"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty == 196

    def test_max_price_universe(self):
        # close=900 (upper bound of universe)
        # worst_case = 900 * 1.02 = 918
        # qty = floor(100000 / 918) = 108
        qty = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("900"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty == 108

    def test_min_price_universe(self):
        # close=100 (lower bound)
        # worst_case = 102
        # qty = floor(100000 / 102) = 980
        qty = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("100"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty == 980

    def test_capital_never_exceeded_at_gap_up_threshold(self):
        """Prove: qty * prev_close * (1+threshold) <= budget always holds."""
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

    def test_uses_min_of_cash_and_budget(self):
        # Cash of ¥50,000 should limit position even if budget is ¥100,000
        qty_limited = compute_quantity(
            available_cash=Decimal("50000"),
            prev_close=Decimal("500"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        qty_full = compute_quantity(
            available_cash=Decimal("100000"),
            prev_close=Decimal("500"),
            gap_up_threshold=Decimal("0.02"),
            budget=Decimal("100000"),
        )
        assert qty_limited < qty_full

    def test_raises_on_insufficient_capital(self):
        with pytest.raises(InsufficientCapitalError):
            compute_quantity(
                available_cash=Decimal("500"),
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
