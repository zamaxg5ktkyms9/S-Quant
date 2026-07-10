"""Operator CLI — pending シグナルの約定/見送りを正式記録する（改善提案 A-5）.

pending_signals タブの execution_status を FILLED / CANCELLED に更新する。
FILLED にしておくと当夜 20:30 の daily_run がポジション化（HOLDING 遷移・
現金控除・損切ライン計算・Slack 通知）まで自動で行う。

Usage:
    # 約定した場合（SBI の約定照会の値をそのまま渡す）
    .venv/bin/python scripts/confirm_entry.py --ticker 2201.T --price 2717.5 --shares 100

    # 見送った / 約定しなかった場合
    .venv/bin/python scripts/confirm_entry.py --ticker 2201.T --cancel

    # 書き込まずに検証だけ行う
    .venv/bin/python scripts/confirm_entry.py --ticker 2201.T --price 2717.5 --shares 100 --dry-run
"""
import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.domain.enums import ExecutionStatus  # noqa: E402
from squant.domain.models import PendingSignal  # noqa: E402

# Fat-finger guard: refuse fills further than this from the reference price
# unless --force is given (e.g. typing 27175 instead of 2717.5).
MAX_PRICE_DEVIATION = Decimal("0.05")


class ConfirmationError(Exception):
    """Validation failure — the sheet must not be written."""


def find_pending(
    pendings: tuple[PendingSignal, ...], ticker: str
) -> PendingSignal:
    """Locate the pending signal for ticker (accepts '2201' for '2201.T')."""
    candidates = {ticker, f"{ticker}.T"} if "." not in ticker else {ticker}
    matches = [p for p in pendings if p.signal.ticker in candidates]
    if not matches:
        known = ", ".join(p.signal.ticker for p in pendings) or "(なし)"
        raise ConfirmationError(
            f"{ticker} の pending シグナルが見つかりません。現在の pending: {known}\n"
            "夜のランで自動キャンセル済みの可能性があります。実際には約定していた"
            "場合は手動リカバリが必要です — Slack で Claude に報告してください。"
        )
    pending = matches[0]
    if pending.execution_status != ExecutionStatus.PENDING:
        raise ConfirmationError(
            f"{pending.signal.ticker} は既に {pending.execution_status.value} で記録済みです。"
            "訂正が必要な場合は Slack で Claude に報告してください。"
        )
    return pending


def validate_fill(
    pending: PendingSignal, price: Decimal, shares: int, force: bool
) -> list[str]:
    """Sanity-check the reported fill. Returns warnings; raises on hard errors."""
    s = pending.signal
    if price <= 0:
        raise ConfirmationError(f"約定価格が不正です: {price}")
    if shares <= 0:
        raise ConfirmationError(f"株数が不正です: {shares}")

    warnings: list[str] = []
    deviation = abs(price - s.reference_price) / s.reference_price
    if deviation > MAX_PRICE_DEVIATION:
        msg = (
            f"約定価格 ¥{price} が参考価格 ¥{s.reference_price} から "
            f"{deviation:.1%} 乖離しています（桁誤り？）"
        )
        if not force:
            raise ConfirmationError(msg + " — 正しければ --force を付けて再実行")
        warnings.append(msg)
    if price > s.cancel_above_price:
        msg = (
            f"約定価格 ¥{price} がキャンセル上限 ¥{s.cancel_above_price} を超えています"
            "（本来は発注見送りの価格帯）"
        )
        if not force:
            raise ConfirmationError(msg + " — 実約定が正であれば --force を付けて再実行")
        warnings.append(msg)
    if shares != s.shares:
        warnings.append(f"株数がシグナル指定 ×{s.shares} と異なります（報告値 ×{shares}）")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="pending シグナルの約定/見送りを記録する")
    parser.add_argument("--ticker", required=True, help="銘柄コード（例: 2201.T / 2201）")
    parser.add_argument("--price", type=str, help="実約定価格（円）")
    parser.add_argument("--shares", type=int, help="実約定株数")
    parser.add_argument("--cancel", action="store_true", help="見送り/不成立として記録")
    parser.add_argument("--force", action="store_true", help="価格サニティチェックを警告に格下げ")
    parser.add_argument("--dry-run", action="store_true", help="検証のみ・Sheets へ書き込まない")
    args = parser.parse_args()

    is_fill = args.price is not None or args.shares is not None
    if args.cancel == is_fill:
        parser.error("--price/--shares（約定）か --cancel（見送り）のどちらか一方を指定してください")
    if is_fill and (args.price is None or args.shares is None):
        parser.error("約定記録には --price と --shares の両方が必要です")

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
        pending = find_pending(repo.load_pending_signals(), args.ticker)
        ticker = pending.signal.ticker
        if is_fill:
            try:
                price = Decimal(args.price)
            except InvalidOperation:
                raise ConfirmationError(f"価格を数値として解釈できません: {args.price}") from None
            warnings = validate_fill(pending, price, args.shares, args.force)
            for w in warnings:
                print(f"WARNING: {w}")
            if args.dry_run:
                print(f"[dry-run] {ticker} ×{args.shares}株 @ ¥{price} を FILLED 記録します（未書込）")
                return 0
            repo.confirm_pending_signal(float(price), args.shares, now_jst(), ticker)
            summary = (
                f"[S-Quant] 約定記録 — {ticker} ×{args.shares}株 @ ¥{price}\n"
                "今夜のランでポジション反映（損切ライン・タイムストップ設定）されます。"
            )
        else:
            if args.dry_run:
                print(f"[dry-run] {ticker} を CANCELLED 記録します（未書込）")
                return 0
            repo.cancel_pending_signal(ticker)
            summary = f"[S-Quant] 見送り記録 — {ticker} は発注なし/不成立として記録しました。"
    except ConfirmationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(summary)
    SlackNotifier(settings.slack_webhook_url).send(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
