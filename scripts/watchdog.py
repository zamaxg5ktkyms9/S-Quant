"""Daily-run watchdog — 「沈黙 = 正常」の排除（改善提案 A1）。

23:45 JST に別 workflow から実行され、検証対象の東証営業日の run_log に
status=success の行があるかを照合する。無ければ Slack にアラートを送り、
exit 1 で workflow を赤にする（GHA 上でも欠測が見える）。

沈黙モード障害（daily_run の cron 自体がスキップされた日は Slack 通知も
飛ばない）への対策。実績: 2026-06-01 に cron が丸ごとスキップ。

**検証対象日の解決（2026-07-15 修正）**:
GHA のスケジュール遅延で watchdog 自身が日付をまたいで起動すると（実績:
7/14 02:00・7/15 01:00 起動）、素朴に `now.date()` を対象にすると「まだ
実行されていない翌日の 20:30 ラン」を探して誤報する。対策として現在時刻を
`WATCHDOG_ANCHOR_HOURS` 時間だけ戻したアンカー日を対象にする。23:45 の
定刻起動なら当日、深夜（〜05:45 JST）の遅延起動なら前営業日を検証する。
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.utils.jst import is_tse_trading_day  # noqa: E402

JST = timezone(timedelta(hours=9))
SHEET_RUN_LOG = "run_log"

# watchdog 定刻は 23:45 JST。GHA 遅延で日付をまたいで起動した場合でも、
# 検証対象を「本来照合すべき営業日」に固定するために現在時刻を戻す時間幅。
# 23:45 - 6h = 17:45（同日）/ 深夜 05:45 - 6h = 23:45（前日）まで正しく解決する。
# daily_run cron 遅延の実績最大は約4時間（operator_guide §3.3）。
WATCHDOG_ANCHOR_HOURS = 6


def resolve_target_date(now: datetime) -> date | None:
    """検証対象の東証営業日を返す（純関数）。営業日でなければ None（=skip）。

    現在時刻を WATCHDOG_ANCHOR_HOURS だけ戻したアンカー日を対象にする。
    これにより 23:45 の定刻起動は当日を、深夜まで遅延した起動は前営業日を
    検証し、`now.date()` を使った場合の「未来ランを探す」誤報を防ぐ。
    """
    target = (now - timedelta(hours=WATCHDOG_ANCHOR_HOURS)).date()
    if not is_tse_trading_day(target):
        return None
    return target


def evaluate_run_log(rows: list[list[str]], target: date) -> tuple[bool, str]:
    """run_log 行と対象日から (ok, alert_message) を返す（純関数・I/O なし）。

    ok=True のとき alert_message は空文字。ok=False のとき Slack へ送る本文。
    """
    header = rows[0] if rows else []
    try:
        date_col = header.index("run_date")
        status_col = header.index("status")
    except ValueError:
        # ヘッダが想定外でも欠測扱いにせず、既定の列位置で照合を試みる。
        date_col, status_col = 1, 2

    target_str = target.isoformat()
    todays = [r for r in rows[1:] if len(r) > status_col and r[date_col] == target_str]
    successes = [r for r in todays if r[status_col] == "success"]

    if successes:
        return True, ""

    if todays:
        statuses = ", ".join(r[status_col] for r in todays)
        msg = (f":rotating_light: [S-Quant watchdog] {target_str} の daily_run が"
               f" success になっていません（status: {statuses}）。"
               f"GitHub → Actions → Daily Trading Run のログ確認が必要です。")
    else:
        msg = (f":rotating_light: [S-Quant watchdog] {target_str} の daily_run が"
               f"未実行です（run_log に当日行なし）。GHA cron のスキップ/遅延の可能性。"
               f"GitHub → Actions → Daily Trading Run を確認してください。")
    return False, msg


def main() -> int:
    now = datetime.now(JST)
    target = resolve_target_date(now)

    if target is None:
        anchored = (now - timedelta(hours=WATCHDOG_ANCHOR_HOURS)).date()
        print(f"{anchored} is not a TSE trading day — watchdog skip")
        return 0

    from squant.config.settings import Settings
    from squant.infrastructure.sheets_client import GoogleSheetsClient
    from squant.infrastructure.slack_notifier import SlackNotifier

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    rows = client.read_all(SHEET_RUN_LOG)

    ok, msg = evaluate_run_log(rows, target)
    if ok:
        print(f"OK: run_log has success for {target.isoformat()}")
        return 0

    print(msg)
    SlackNotifier(settings.slack_webhook_url).send(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
