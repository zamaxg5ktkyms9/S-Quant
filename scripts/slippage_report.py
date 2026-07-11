"""Slippage weekly report — slippage_log の週次集計（改善提案 A-3）.

金曜夜に GHA から実行され、slippage_log を集計して Slack に配信する。
正準約定モデル（backtest_report §8.17 gap-aware）が実測に対して楽観/悲観どちらに
ズレているかの一次モニタリング。符号規約は adverse-positive（不利方向が正）。

デフォルトは直近7日分＋累積サマリー。記録が1件もない週は送信しない
（--always で空でも送信）。
"""
import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _aggregate(rows: list[dict[str, str]]) -> dict[str, dict]:
    """side ごとに件数・平均/中央値 bps・累計円を集計する。"""
    out: dict[str, dict] = {}
    for side in ("BUY", "SELL"):
        subset = [r for r in rows if r.get("side") == side]
        if not subset:
            continue
        bps = [float(r["slippage_bps"]) for r in subset]
        jpy = sum(Decimal(r["slippage_jpy"]) for r in subset)
        out[side] = {
            "n": len(subset),
            "mean_bps": mean(bps),
            "median_bps": median(bps),
            "total_jpy": jpy,
        }
    return out


def build_report(
    rows: list[dict[str, str]], since: date, today: date
) -> str | None:
    """週次レポート本文を組み立てる。対象期間に記録がなければ None。"""
    recent = [r for r in rows if r.get("date", "") >= since.isoformat()]
    if not recent:
        return None

    lines = [
        f":bar_chart: *[S-Quant] スリッページ週次集計*（{since} 〜 {today}、不利方向が正）",
        "",
    ]
    for r in recent:
        lines.append(
            f"• {r['date']} {r['ticker']} {r['side']}: "
            f"想定 ¥{r['intended_price']} → 実約定 ¥{r['actual_price']} "
            f"= {float(r['slippage_bps']):+.1f}bps（¥{int(Decimal(r['slippage_jpy'])):+,}）"
            + (f" [{r['note']}]" if r.get("note") else "")
        )

    week = _aggregate(recent)
    cumulative = _aggregate(rows)
    lines.append("")
    for label, agg in (("今週", week), ("累積", cumulative)):
        for side, a in agg.items():
            lines.append(
                f"{label} {side}: {a['n']}件 / 平均 {a['mean_bps']:+.1f}bps / "
                f"中央値 {a['median_bps']:+.1f}bps / 累計 ¥{int(a['total_jpy']):+,}"
            )
    lines.append("")
    lines.append("正準モデル（§8.17 gap-aware）との乖離が正方向に累積する場合は要再検証。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="slippage_log の週次集計を Slack に配信する")
    parser.add_argument("--days", type=int, default=7, help="集計対象日数（既定7）")
    parser.add_argument("--always", action="store_true", help="記録ゼロでも送信する")
    parser.add_argument("--no-notify", action="store_true", help="標準出力のみ・Slack に送らない")
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

    today = now_jst().date()
    since = today - timedelta(days=args.days)
    report = build_report(repo.load_slippage_rows(), since, today)

    if report is None:
        msg = f"OK: no slippage records in the last {args.days} days"
        print(msg)
        if args.always and not args.no_notify:
            SlackNotifier(settings.slack_webhook_url).send(
                f":bar_chart: [S-Quant] スリッページ週次集計 — 直近{args.days}日の記録なし"
            )
        return 0

    print(report)
    if not args.no_notify:
        SlackNotifier(settings.slack_webhook_url).send(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
