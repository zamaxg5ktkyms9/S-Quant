"""Operator CLI — ザラ場で約定した出口を帳簿に反映する（手動出口の正式記録）.

夜ランの出口判定は終値ベースのため、SBI の逆指値がザラ場で約定しても終値が
ストップ上で引けた日は出口を検知できない（実例: 2026-07-10 の 2201.T、
安値 ¥2,649.0 ≤ トリガー ¥2,649.56 で約定、終値 ¥2,656 で HOLDING のまま）。
V-1 パリティ照合が「モデル EXIT vs 本番 HOLD」alert でこのケースを検出する。

この CLI はオーナーの実約定報告（チャット/Slack 経由）を受けて、
holding_pipeline の出口処理と同じ帳簿遷移を**実約定価格で**実行する:
  1. trades に SELL 行を追記（price = 実約定価格、pnl も実額）
  2. recent_sales に追記（差金決済ガード）
  3. circuit_breaker をネット純損失で更新
  4. portfolio を SETTLING へ（現金 += 実約定額、T+2 settle_date 設定）
  5. slippage_log に「モデル想定出口 vs 実約定」を記録（adverse-positive）

confirm_exit.py（記録のみ・状態不変）との違い: こちらは夜ランが出口を検知
**できなかった**ケース用で、帳簿本体を書き換える。既に trades に SELL 行が
ある出口には使わない（二重計上になる — confirm_exit.py を使う）。

Usage:
    python scripts/record_manual_exit.py --ticker 2201.T --price 2663 --date 2026-07-10
    python scripts/record_manual_exit.py --ticker 2201.T --price 2663 --date 2026-07-10 --apply
"""
import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.domain import circuit_breaker as cb_module  # noqa: E402
from squant.domain.enums import ExitReason, OrderSide, SystemState  # noqa: E402
from squant.domain.models import (  # noqa: E402
    CircuitBreakerStatus,
    PortfolioState,
    Position,
    RecentSale,
    TradeRecord,
)
from squant.utils.jst import calculate_settlement_date, is_tse_trading_day  # noqa: E402

JST = timezone(timedelta(hours=9))

# 実約定価格がエントリー価格から±この比率を超えたら拒否（桁誤り・銘柄違いガード）
PRICE_SANITY_RATIO = Decimal("0.15")


class ManualExitError(Exception):
    """Validation failure — the sheet must not be written."""


@dataclass
class ManualExit:
    trade: TradeRecord
    recent_sale: RecentSale
    new_portfolio: PortfolioState
    position: Position
    pnl_jpy: Decimal
    settle_date: date


def build_manual_exit(
    portfolio: PortfolioState,
    *,
    ticker: str,
    price: Decimal,
    sale_date: date,
    reason: ExitReason,
    run_id: str = "manual",
) -> ManualExit:
    """実約定に基づく出口の帳簿遷移を組み立てる（純関数・ガードつき）。"""
    position = next((p for p in portfolio.positions if p.ticker == ticker), None)
    if position is None:
        held = ", ".join(p.ticker for p in portfolio.positions) or "(なし)"
        raise ManualExitError(
            f"{ticker} は保有していません（保有: {held}）。"
            "銘柄コードを確認してください。"
        )

    if price <= 0:
        raise ManualExitError(f"約定価格が不正です: {price}")
    deviation = abs(price - position.entry_price) / position.entry_price
    if deviation > PRICE_SANITY_RATIO:
        raise ManualExitError(
            f"約定価格 ¥{price} がエントリー ¥{position.entry_price} から "
            f"{deviation * 100:.1f}% 乖離しています（ガード {PRICE_SANITY_RATIO * 100:.0f}%）。"
            "桁誤り・銘柄違いの疑い。正しければ価格を再確認してください。"
        )

    if not is_tse_trading_day(sale_date):
        raise ManualExitError(f"{sale_date} は TSE 営業日ではありません。")
    if sale_date < position.entry_date:
        raise ManualExitError(
            f"売却日 {sale_date} がエントリー日 {position.entry_date} より前です。"
        )
    if sale_date > datetime.now(JST).date():
        raise ManualExitError(f"売却日 {sale_date} が未来日です。")

    pnl = (price - position.entry_price) * position.shares
    settle_date = calculate_settlement_date(sale_date)

    trade = TradeRecord(
        ticker=ticker,
        side=OrderSide.SELL,
        shares=position.shares,
        price=price,
        # ザラ場約定の正確な時刻は不明のため引け時刻で代表させる
        executed_at=datetime.combine(sale_date, time(15, 0), tzinfo=JST),
        pnl_jpy=pnl,
        exit_reason=reason,
        run_id=run_id,
    )
    recent_sale = RecentSale(
        ticker=ticker, sell_date=sale_date, settlement_date=settle_date,
    )

    remaining = tuple(p for p in portfolio.positions if p.ticker != ticker)
    new_state = SystemState.HOLDING if remaining else SystemState.SETTLING
    new_portfolio = PortfolioState(
        state=new_state,
        cash_jpy=portfolio.cash_jpy + price * position.shares,
        positions=remaining,
        pending_signals=portfolio.pending_signals,
        settle_dates=portfolio.settle_dates + (settle_date,),
        last_run_id=run_id,
        cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy + pnl,
    )
    return ManualExit(
        trade=trade, recent_sale=recent_sale, new_portfolio=new_portfolio,
        position=position, pnl_jpy=pnl, settle_date=settle_date,
    )


def default_intended_price(position: Position) -> Decimal:
    """slippage の「モデル想定出口」既定値 = 実効ストップ（ハード/トレーリングの高い方）。"""
    return max(position.stop_loss_price, position.trailing_stop_price)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ザラ場約定した出口を帳簿に反映する（既定は dry-run プレビュー）"
    )
    parser.add_argument("--ticker", required=True, help="例: 2201.T")
    parser.add_argument("--price", required=True, help="実約定価格（円）")
    parser.add_argument("--date", required=True, help="売却日 YYYY-MM-DD")
    parser.add_argument(
        "--reason", default="STOP_LOSS",
        choices=[r.value for r in ExitReason],
        help="出口理由 (default: STOP_LOSS)",
    )
    parser.add_argument(
        "--intended", default=None,
        help="slippage 記録用のモデル想定出口価格（省略時: 実効ストップ）",
    )
    parser.add_argument("--apply", action="store_true",
                        help="実際に Sheets へ書き込む（省略時はプレビューのみ）")
    args = parser.parse_args()

    try:
        price = Decimal(args.price)
        sale_date = date.fromisoformat(args.date)
    except (InvalidOperation, ValueError) as e:
        print(f"ERROR: 引数が不正です: {e}", file=sys.stderr)
        return 1

    from squant.config.constants import SHEET_TRADES
    from squant.config.settings import Settings
    from squant.domain.slippage import compute_slippage
    from squant.infrastructure.sheets_client import GoogleSheetsClient
    from squant.infrastructure.sheets_repository import SheetsStateRepository
    from squant.infrastructure.slack_notifier import SlackNotifier

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    repo = SheetsStateRepository(client)
    portfolio = repo.load_portfolio()

    # 二重計上ガード: 当該銘柄の SELL 行が売却日に既にあれば拒否
    trades_rows = client.read_all(SHEET_TRADES)
    for raw in trades_rows[1:] if trades_rows else []:
        if (len(raw) >= 6 and raw[1] == args.ticker and raw[2] == "SELL"
                and str(raw[5])[:10] == sale_date.isoformat()):
            print(
                f"ERROR: {args.ticker} の {sale_date} SELL は trades に記録済みです。"
                "実約定価格の記録は confirm_exit.py を使ってください。",
                file=sys.stderr,
            )
            return 1

    try:
        result = build_manual_exit(
            portfolio,
            ticker=args.ticker,
            price=price,
            sale_date=sale_date,
            reason=ExitReason(args.reason),
        )
    except ManualExitError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    cb = repo.load_circuit_breaker()
    new_cb: CircuitBreakerStatus = cb_module.update_after_trade(cb, result.trade)
    intended = Decimal(args.intended) if args.intended else default_intended_price(result.position)
    slip_bps, slip_jpy = compute_slippage(
        OrderSide.SELL, intended, price, result.position.shares
    )

    pnl_sign = "+" if result.pnl_jpy >= 0 else ""
    print(f"=== 手動出口の帳簿反映{'' if args.apply else '（プレビュー・未書き込み）'} ===")
    print(f"  {args.ticker} ×{result.position.shares}株 "
          f"@¥{price}（entry ¥{result.position.entry_price}） {args.reason}")
    print(f"  実現損益: {pnl_sign}¥{int(result.pnl_jpy):,}")
    print(f"  現金: ¥{int(portfolio.cash_jpy):,} → ¥{int(result.new_portfolio.cash_jpy):,}")
    print(f"  状態: {portfolio.state.value} → {result.new_portfolio.state.value} "
          f"(settle {result.settle_date})")
    print(f"  CB 純損失: ¥{int(cb.cumulative_loss_jpy):,} → ¥{int(new_cb.cumulative_loss_jpy):,} "
          f"(tripped={new_cb.is_tripped})")
    print(f"  slippage: intended ¥{intended} → actual ¥{price} "
          f"({slip_bps} bps / ¥{slip_jpy}、負=有利)")

    if not args.apply:
        print("\n--apply を付けると上記を Sheets に書き込みます。")
        return 0

    repo.append_trade(result.trade)
    repo.append_recent_sale(result.recent_sale)
    repo.save_circuit_breaker(new_cb)
    repo.save_portfolio(result.new_portfolio)
    repo.append_slippage(
        log_date=sale_date, ticker=args.ticker, side=OrderSide.SELL.value,
        intended_price=intended, actual_price=price,
        shares=result.position.shares, slippage_bps=slip_bps, slippage_jpy=slip_jpy,
        run_id="manual", note="intraday stop fill (recorded via record_manual_exit)",
    )
    print("書き込み完了。verify_ledger で恒等式を確認してください。")

    SlackNotifier(settings.slack_webhook_url).send(
        f"[S-Quant] 手動出口を帳簿反映 — {args.ticker} ×{result.position.shares}株 "
        f"@¥{price}（{args.reason}、売却日 {sale_date}）\n"
        f"実現損益 {pnl_sign}¥{int(result.pnl_jpy):,} | "
        f"現金 ¥{int(result.new_portfolio.cash_jpy):,} | "
        f"CB 純損失 ¥{int(new_cb.cumulative_loss_jpy):,}/¥90,000 | "
        f"T+2 受渡 {result.settle_date}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
