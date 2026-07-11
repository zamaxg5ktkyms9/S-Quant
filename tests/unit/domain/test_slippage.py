"""Unit tests for domain.slippage — adverse-positive sign convention."""

from decimal import Decimal

import pytest

from squant.domain.enums import OrderSide
from squant.domain.slippage import compute_slippage


class TestComputeSlippage:
    def test_buy_paying_more_is_adverse_positive(self):
        """BUY: 想定より高く買った → 正"""
        bps, jpy = compute_slippage(OrderSide.BUY, Decimal("1000"), Decimal("1010"), 100)
        assert bps == Decimal("100.0")  # +1% = +100bps
        assert jpy == Decimal("1000")   # +10円 × 100株

    def test_buy_paying_less_is_favorable_negative(self):
        """BUY: 想定より安く買えた → 負（2201.T 実例: 2733.5 → 2717.5）"""
        bps, jpy = compute_slippage(
            OrderSide.BUY, Decimal("2733.5"), Decimal("2717.5"), 100
        )
        assert bps < 0
        assert jpy == Decimal("-1600")

    def test_sell_receiving_less_is_adverse_positive(self):
        """SELL: 想定より安く売った → 正"""
        bps, jpy = compute_slippage(OrderSide.SELL, Decimal("1000"), Decimal("990"), 100)
        assert bps == Decimal("100.0")
        assert jpy == Decimal("1000")

    def test_sell_receiving_more_is_favorable_negative(self):
        bps, jpy = compute_slippage(OrderSide.SELL, Decimal("1000"), Decimal("1010"), 100)
        assert bps == Decimal("-100.0")
        assert jpy == Decimal("-1000")

    def test_zero_slippage(self):
        bps, jpy = compute_slippage(OrderSide.BUY, Decimal("500"), Decimal("500"), 100)
        assert bps == Decimal("0.0")
        assert jpy == Decimal("0")

    def test_nonpositive_intended_raises(self):
        with pytest.raises(ValueError, match="intended_price must be positive"):
            compute_slippage(OrderSide.BUY, Decimal("0"), Decimal("100"), 100)

    def test_negative_intended_raises(self):
        with pytest.raises(ValueError, match="intended_price must be positive"):
            compute_slippage(OrderSide.BUY, Decimal("-5"), Decimal("100"), 100)

    def test_intended_price_of_one_yen_does_not_raise(self):
        """intended_price=¥1 は正の値なので例外を投げない（境界 <= 0 の反対側）。

        mutation testing (V-3): ガード条件 `intended_price <= 0` を `<= 1` に変える変異は
        ¥1 での非例外を確認しないと生き残る。
        """
        bps, jpy = compute_slippage(OrderSide.BUY, Decimal("1"), Decimal("1"), 100)
        assert bps == Decimal("0.0")
        assert jpy == Decimal("0")
