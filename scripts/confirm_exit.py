"""Operator CLI — 売却の実約定価格を記録する（改善提案 A-3）.

システムの出口はモデル価格（出口シグナル時の終値）で trades タブに記録されるが、
実際の売却は翌朝の成行（タイムストップ）やザラ場の逆指値約定で行われ、実約定価格は
これまで Slack 報告のみで構造化記録がなかった。この CLI は trades タブの直近 SELL と
実約定を突き合わせ、slippage_log に「モデル出口 vs 実売却」を追記する。

**状態（portfolio / PnL / circuit_breaker）は書き換えない。記録のみ。**
モデルと実測の乖離集計は scripts/slippage_report.py。

Usage:
    .venv/bin/python scripts/confirm_exit.py --ticker 2201.T --price 2650
    .venv/bin/python scripts/confirm_exit.py --ticker 2201.T --price 2650 --shares 100 --dry-run
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.config.constants import SHEET_TRADES  # noqa: E402
from squant.domain.enums import OrderSide  # noqa: E402
from squant.domain.slippage import compute_slippage  # noqa: E402

JST = timezone(timedelta(hours=9))

# trades タブのスキーマ（sheets_repository._TRADES_HEADER と同値）
_TRADES_HEADER = [
    "run_id", "ticker", "side", "shares", "price",
    "executed_at", "pnl_jpy", "exit_reason",
]

# 直近 SELL がこれより古い場合は対象違いの疑いとして拒否（--force で格下げ）
MAX_TRADE_AGE_DAYS = 7


class ConfirmationError(Exception):
    """Validation failure — the sheet must not be written."""


def find_latest_sell(
    trade_rows: list[list[str]], ticker: str
) -> dict[str, str]:
    """trades タブの行リストから対象銘柄の最新 SELL を返す。"""
    candidates = {ticker, f"{ticker}.T"} if "." not in ticker else {ticker}
    matches = []
    for raw in trade_rows:
        if not raw or len(raw) < 5:
            continue
        r = dict(zip(_TRADES_HEADER, list(raw) + [""] * (len(_TRADES_HEADER) - len(raw))))  # noqa: B905
        if r["ticker"] in candidates and r["side"] == OrderSide.SELL.value:
            matches.append(r)
    if not matches:
        raise ConfirmationError(
            f"{ticker} の SELL 記録が trades タブに見つかりません。"
            "出口シグナルが出た後（夜のラン完了後）に実行してください。"
        )
    return matches[-1]


def validate_exit(
    trade: dict[str, str], price: Decimal, today: datetime, force: bool
) -> list[str]:
    """Sanity-check the reported sale. Returns warnings; raises on hard errors."""
    if price <= 0:
        raise ConfirmationError(f"売却価格が不正です: {price}")

    warnings: list[str] = []
    model_price = Decimal(trade["price"])
    deviation = abs(price - model_price) / model_price
    if deviation > Decimal("0.10"):
        msg = (
            f"実売却 ¥{price} がモデル出口 ¥{model_price} から {deviation:.1%} "
            "乖離しています（桁誤り？）"
        )
        if not force:
            raise ConfirmationError(msg + " — 正しければ --force を付けて再実行")
        warnings.append(msg)

    executed = datetime.fromisoformat(trade["executed_at"])
    age_days = (today - executed).days
    if age_days > MAX_TRADE_AGE_DAYS:
        msg = f"直近 SELL は {age_days} 日前（{executed.date()}）の記録です — 対象違い？"
        if not force:
            raise ConfirmationError(msg + " 正しければ --force を付けて再実行")
        warnings.append(msg)
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="売却の実約定価格を slippage_log に記録する")
    parser.add_argument("--ticker", required=True, help="銘柄コード（例: 2201.T / 2201）")
    parser.add_argument("--price", type=str, required=True, help="実売却価格（円）")
    parser.add_argument("--shares", type=int, help="実売却株数（省略時は trades の株数）")
    parser.add_argument("--force", action="store_true", help="サニティチェックを警告に格下げ")
    parser.add_argument("--dry-run", action="store_true", help="検証のみ・Sheets へ書き込まない")
    args = parser.parse_args()

    from squant.config.settings import Settings
    from squant.infrastructure.sheets_client import GoogleSheetsClient
    from squant.infrastructure.sheets_repository import SheetsStateRepository
    from squant.infrastructure.slack_notifier import SlackNotifier
    from squant.utils.jst import now_jst

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    repo = SheetsStateRepository(client)

    try:
        try:
            price = Decimal(args.price)
        except InvalidOperation:
            raise ConfirmationError(f"価格を数値として解釈できません: {args.price}") from None

        rows = client.read_all(SHEET_TRADES)
        trade = find_latest_sell(rows[1:] if rows else [], args.ticker)
        now = now_jst()
        warnings = validate_exit(trade, price, now, args.force)
        for w in warnings:
            print(f"WARNING: {w}")

        ticker = trade["ticker"]
        shares = args.shares or int(trade["shares"])
        model_price = Decimal(trade["price"])
        bps, jpy = compute_slippage(OrderSide.SELL, model_price, price, shares)

        summary = (
            f"[S-Quant] 売却記録 — {ticker} ×{shares}株 @ ¥{price}\n"
            f"モデル出口 ¥{model_price}（{trade['exit_reason']}）比 "
            f"スリッページ {bps:+.1f}bps（不利方向が正）/ ¥{jpy:+,}"
        )
        if args.dry_run:
            print(f"[dry-run] slippage_log 追記内容（未書込）:\n{summary}")
            return 0

        repo.append_slippage(
            log_date=now.date(), ticker=ticker, side=OrderSide.SELL.value,
            intended_price=model_price, actual_price=price, shares=shares,
            slippage_bps=bps, slippage_jpy=jpy,
            run_id=trade["run_id"], note=trade["exit_reason"],
        )
    except ConfirmationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(summary)
    SlackNotifier(settings.slack_webhook_url).send(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
