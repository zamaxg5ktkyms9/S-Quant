"""WF 結果を Slack に通知する一時スクリプト（完了後に削除）。

環境変数 / CLI で監視対象ファイルを指定できる。
"""

import argparse
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
MAX_WAIT_SECONDS = 43200  # 12時間
POLL_INTERVAL = 60


def _format_msg(report: dict, label: str) -> str:
    windows = report.get("windows") or []
    if not windows:
        return f"[S-Quant] {label} 結果: windows なし（実行失敗の可能性）"
    w = windows[0]
    is_m = w["in_sample"]["metrics"]
    oos_m = w["out_of_sample"]["metrics"]
    is_p = w["in_sample"]["period"]
    oos_p = w["out_of_sample"]["period"]
    best = w["in_sample"]["best_params"]
    robust = w.get("robust", False)
    verdict = report.get("verdict", "")

    lines = [
        f"*[S-Quant] {label} 完了*",
        f"IS: {is_p} → OOS: {oos_p}",
        f"Best params: TP={best.get('target_profit')} ATR={best.get('atr_mult')} "
        f"RSI≤{best.get('rsi_upper')} TS={best.get('time_stop')} "
        f"signal={best.get('signal')}",
        "",
        f"*IS*",
        f"  trades={is_m['trades']}  monthly={is_m['monthly_pnl_pct']:+.2f}%  "
        f"PF={(is_m.get('profit_factor') or 0):.2f}  DD={is_m['max_dd_pct']:+.1f}%",
        f"*OOS*",
        f"  trades={oos_m['trades']}  monthly={oos_m['monthly_pnl_pct']:+.2f}%  "
        f"PF={(oos_m.get('profit_factor') or 0):.2f}  DD={oos_m['max_dd_pct']:+.1f}%",
        "",
        f"*Robust 判定*: {'✅ PASS' if robust else '❌ FAIL'}",
        f"*Verdict*: {verdict}",
    ]
    return "\n".join(lines)


def _format_abort_msg(log_tail: str, label: str) -> str:
    return (
        f"*[S-Quant] {label} abort or error*\n"
        "WF が結果ファイルを出力せず終了しました。\n\n"
        "Log tail:\n```\n" + log_tail[-1500:] + "\n```"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-file", required=True,
                        help="監視する結果 JSON のパス")
    parser.add_argument("--log-file", required=True,
                        help="監視する WF ログのパス")
    parser.add_argument("--label", default="WF", help="Slack メッセージのラベル")
    args = parser.parse_args()

    result_file = Path(args.result_file)
    log_file = Path(args.log_file)

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        print("ERROR: SLACK_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(1)
    notifier = SlackNotifier(webhook_url=webhook, dry_run=False)

    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        if result_file.exists():
            try:
                report = json.loads(result_file.read_text())
            except Exception as e:
                notifier.send(f"[S-Quant] {args.label} 結果ファイル読み込みエラー: {e}")
                sys.exit(1)
            notifier.send(_format_msg(report, args.label))
            print(f"Sent {args.label} result to Slack", flush=True)
            return

        if log_file.exists():
            log = log_file.read_text()
            if (
                "❌ ETA 異常膨張" in log
                or "❌ 全体タイムアウト" in log
                or "全窓で結果が得られませんでした" in log
                or "ETAInflationAbort: " in log
                or "WallClockExceeded: " in log
            ):
                notifier.send(_format_abort_msg(log, args.label))
                print(f"Sent abort notice for {args.label} to Slack", flush=True)
                return

        time.sleep(POLL_INTERVAL)

    log_tail = log_file.read_text() if log_file.exists() else "(log file missing)"
    notifier.send(
        f"[S-Quant] {args.label} 待機タイムアウト ({MAX_WAIT_SECONDS}s 経過)\n"
        f"Log tail:\n```\n{log_tail[-1000:]}\n```"
    )


if __name__ == "__main__":
    main()
