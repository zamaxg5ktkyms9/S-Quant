"""Daily-run watchdog — 「沈黙 = 正常」の排除（改善提案 A1）。

23:45 JST に別 workflow から実行され、当日（東証営業日）の run_log に
status=success の行があるかを照合する。無ければ Slack にアラートを送り、
exit 1 で workflow を赤にする（GHA 上でも欠測が見える）。

沈黙モード障害（daily_run の cron 自体がスキップされた日は Slack 通知も
飛ばない）への対策。実績: 2026-06-01 に cron が丸ごとスキップ。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.config.settings import Settings  # noqa: E402
from squant.infrastructure.sheets_client import GoogleSheetsClient  # noqa: E402
from squant.infrastructure.slack_notifier import SlackNotifier  # noqa: E402
from squant.utils.jst import is_tse_trading_day  # noqa: E402

JST = timezone(timedelta(hours=9))
SHEET_RUN_LOG = "run_log"


def main() -> int:
    now = datetime.now(JST)
    today = now.date()

    if not is_tse_trading_day(today):
        print(f"{today} is not a TSE trading day — watchdog skip")
        return 0

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    rows = client.read_all(SHEET_RUN_LOG)
    header = rows[0] if rows else []
    try:
        date_col = header.index("run_date")
        status_col = header.index("status")
    except ValueError:
        print("run_log header unexpected — alerting")
        date_col, status_col = 1, 2

    today_str = today.isoformat()
    todays = [r for r in rows[1:] if len(r) > status_col and r[date_col] == today_str]
    successes = [r for r in todays if r[status_col] == "success"]

    if successes:
        print(f"OK: run_log has success for {today_str}")
        return 0

    notifier = SlackNotifier(settings.slack_webhook_url)
    if todays:
        statuses = ", ".join(r[status_col] for r in todays)
        msg = (f":rotating_light: [S-Quant watchdog] {today_str} の daily_run が"
               f" success になっていません（status: {statuses}）。"
               f"GitHub → Actions → Daily Trading Run のログ確認が必要です。")
    else:
        msg = (f":rotating_light: [S-Quant watchdog] {today_str} の daily_run が"
               f"未実行です（run_log に当日行なし）。GHA cron のスキップ/遅延の可能性。"
               f"GitHub → Actions → Daily Trading Run を確認してください。")
    print(msg)
    notifier.send(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
