"""Pending-signal fill reminder — 約定報告の催促（改善提案 A-5）.

平日 15:30 JST（大引け後）に別 workflow から実行され、前営業日以前に生成された
PENDING の pending_signal が残っていれば Slack で約定報告を催促する。

当夜 20:30 の daily_run は日付の古い PENDING を「オペレータ応答なし＝発注なし」
として自動キャンセルするため（daily_runner._process_pending_signals）、この
リマインダーが記録の最終チャンスになる。実績: 2026-07-09 の 2201.T は約定報告が
チャット経由の手動記録になり、催促と正式 CLI の必要性が実証された。
"""
import sys
from datetime import date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.domain.enums import ExecutionStatus  # noqa: E402
from squant.domain.models import PendingSignal  # noqa: E402
from squant.utils.jst import is_tse_trading_day, now_jst  # noqa: E402

JST = timezone(timedelta(hours=9))


def find_stale_pendings(
    pendings: tuple[PendingSignal, ...], today: date
) -> list[PendingSignal]:
    """前営業日以前に生成され、いまだ PENDING のシグナルを抽出する。

    当日生成分（夜のランで出たばかりのもの）は翌日の確認対象なので含めない。
    """
    return [
        p for p in pendings
        if p.execution_status == ExecutionStatus.PENDING
        and p.signal.generated_at.date() < today
    ]


def build_reminder(stale: list[PendingSignal]) -> str:
    lines = [
        ":alarm_clock: *[S-Quant] 約定報告のお願い* — 未確認のシグナルが"
        f" {len(stale)} 件あります。",
        "",
    ]
    for p in stale:
        s = p.signal
        lines.append(
            f"• *{s.ticker}* ×{s.shares}株 @ 参考 ¥{s.reference_price}"
            f"（{s.generated_at.strftime('%m/%d %H:%M')} 発報）"
        )
    example = stale[0].signal
    lines += [
        "",
        "*今夜 20:30 のランまでに未記録の場合、「発注なし」として自動キャンセルされます。*",
        "記録方法（いずれか）:",
        "1. この Slack に約定価格・株数（または見送り）を返信 → Claude が記録します",
        "2. CLI で直接記録:",
        f"```# 約定した場合\n.venv/bin/python scripts/confirm_entry.py "
        f"--ticker {example.ticker} --price <約定価格> --shares <株数>\n"
        f"# 見送った/約定しなかった場合\n.venv/bin/python scripts/confirm_entry.py "
        f"--ticker {example.ticker} --cancel```",
    ]
    return "\n".join(lines)


def main() -> int:
    today = now_jst().date()
    if not is_tse_trading_day(today):
        print(f"{today} is not a TSE trading day — reminder skip")
        return 0

    from squant.config.settings import Settings
    from squant.infrastructure.sheets_client import GoogleSheetsClient
    from squant.infrastructure.sheets_repository import SheetsStateRepository
    from squant.infrastructure.slack_notifier import SlackNotifier

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    repo = SheetsStateRepository(client)

    stale = find_stale_pendings(repo.load_pending_signals(), today)
    if not stale:
        print(f"OK: no stale pending signals as of {today}")
        return 0

    msg = build_reminder(stale)
    print(msg)
    SlackNotifier(settings.slack_webhook_url).send(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
