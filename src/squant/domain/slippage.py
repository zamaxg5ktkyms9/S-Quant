"""Slippage computation — 想定価格 vs 実約定の差分（改善提案 A-3）.

符号規約: **adverse-positive**（不利方向が正）。
- BUY:  実約定が想定より高い → 正（高く買わされた）
- SELL: 実約定が想定より安い → 正（安く売らされた）

正準約定モデル（backtest_report §8.17 gap-aware）の妥当性を実測で突き合わせる
ための一次データ。集計は scripts/slippage_report.py。
"""
from decimal import Decimal

from squant.domain.enums import OrderSide


def compute_slippage(
    side: OrderSide,
    intended_price: Decimal,
    actual_price: Decimal,
    shares: int,
) -> tuple[Decimal, Decimal]:
    """Return (slippage_bps, slippage_jpy), both adverse-positive.

    slippage_jpy is the total cash impact for the lot (per-share diff × shares).
    """
    if intended_price <= 0:
        raise ValueError(f"intended_price must be positive: {intended_price}")
    diff = actual_price - intended_price
    if side == OrderSide.SELL:
        diff = -diff
    bps = diff / intended_price * Decimal("10000")
    return bps.quantize(Decimal("0.1")), (diff * shares).quantize(Decimal("1"))
