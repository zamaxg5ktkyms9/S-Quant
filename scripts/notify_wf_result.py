"""C-WF 結果を Slack に通知する一時スクリプト（結果ファイル待機 + 通知）。

WF が完了するまで結果 JSON の出現を待ち、出現したら主要メトリクスを Slack へ送る。
WF が abort された場合（ログに ETAInflationAbort / WallClockExceeded が出る場合）も
通知する。
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from squant.infrastructure.slack_notifier import SlackNotifier

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULT_FILE = REPO_ROOT / "docs" / "backtests" / "walkforward_single_C_W1.json"
LOG_FILE = Path("/tmp/squant_cwf.log")
MAX_WAIT_SECONDS = 8000  # WF max-wall-clock 7200s + 余裕
POLL_INTERVAL = 30


def _format_msg(report: dict) -> str:
    windows = report.get("windows") or []
    if not windows:
        return "[S-Quant] C-WF 結果: windows なし（実行失敗の可能性）"
    w = windows[0]
    is_m = w["in_sample"]["metrics"]
    oos_m = w["out_of_sample"]["metrics"]
    is_p = w["in_sample"]["period"]
    oos_p = w["out_of_sample"]["period"]
    best = w["in_sample"]["best_params"]
    robust = w.get("robust", False)
    verdict = report.get("verdict", "")

    lines = [
        "*[S-Quant] C-WF 単窓 (MA クロス) 完了*",
        f"IS: {is_p} → OOS: {oos_p}",
        f"Best params: TP={best.get('target_profit')} ATR={best.get('atr_mult')} "
        f"RSI≤{best.get('rsi_upper')} TS={best.get('time_stop')}",
        "",
        "*IS (2022)*",
        f"  trades={is_m['trades']}  monthly={is_m['monthly_pnl_pct']:+.2f}%  "
        f"PF={(is_m.get('profit_factor') or 0):.2f}  DD={is_m['max_dd_pct']:+.1f}%",
        "*OOS (2023)*",
        f"  trades={oos_m['trades']}  monthly={oos_m['monthly_pnl_pct']:+.2f}%  "
        f"PF={(oos_m.get('profit_factor') or 0):.2f}  DD={oos_m['max_dd_pct']:+.1f}%",
        "",
        f"*Robust 判定*: {'✅ PASS' if robust else '❌ FAIL'}",
        f"*Verdict*: {verdict}",
        "",
        "比較ベンチマーク（同じ OOS=2023）:",
        "  A1 単一銘柄: monthly -1.27%, PF 0.43, DD -15.1%",
        "  B 2銘柄分散¥200k: monthly -0.66%, PF 0.61, DD -8.9%",
    ]
    return "\n".join(lines)


def _format_abort_msg(log_tail: str) -> str:
    return (
        "*[S-Quant] C-WF abort or error*\n"
        "WF が結果ファイルを出力せず終了しました。\n\n"
        "Log tail:\n```\n" + log_tail[-1500:] + "\n```"
    )


def main() -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(1)
    notifier = SlackNotifier(webhook_url=webhook, dry_run=False)

    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        if RESULT_FILE.exists():
            try:
                report = json.loads(RESULT_FILE.read_text())
            except Exception as e:
                notifier.send(f"[S-Quant] C-WF 結果ファイル読み込みエラー: {e}")
                sys.exit(1)
            notifier.send(_format_msg(report))
            print("Sent C-WF result to Slack", flush=True)
            return

        # log にabortの兆候があれば送って終了
        # 注: 「全体タイムアウト: 7200s」(設定表示) は誤検知するので含めない。
        # 「❌ 全体タイムアウト」「❌ ETA 異常膨張」など実際のエラー出力を捕捉する。
        if LOG_FILE.exists():
            log = LOG_FILE.read_text()
            if (
                "❌ ETA 異常膨張" in log
                or "❌ 全体タイムアウト" in log
                or "全窓で結果が得られませんでした" in log
                or "ETAInflationAbort: " in log  # raise 時のメッセージ
                or "WallClockExceeded: " in log
            ):
                notifier.send(_format_abort_msg(log))
                print("Sent abort notice to Slack", flush=True)
                return

        time.sleep(POLL_INTERVAL)

    # タイムアウト
    log_tail = LOG_FILE.read_text() if LOG_FILE.exists() else "(log file missing)"
    notifier.send(
        f"[S-Quant] C-WF 待機タイムアウト ({MAX_WAIT_SECONDS}s 経過)\n"
        f"Log tail:\n```\n{log_tail[-1000:]}\n```"
    )


if __name__ == "__main__":
    main()
