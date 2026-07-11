"""Weekly summary — 週次サマリーの Slack 配信（改善提案 A-4）.

土曜 10:00 JST に GHA から実行（金曜ランの完走 22〜24時 JST を確実に取り込むため
「金曜引け後」を土曜朝と解釈）。内容:

- 今週の売却（trades タブ、直近7日）と現在の保有
- 累積 PnL / サーキットブレーカー余裕（¥90,000 − 純損失。F-1 修正後のネット判定）
- 対 TOPIX 超過: TOPIX 連動 ETF 1306.T（J-Quants）を基準に、weekly_log の前週
  スナップショットとの比較で週次リターン差を出す（初回は基準記録のみ）
- スクリーニング通過数の推移（funnel_log 直近5営業日）

weekly_log タブに評価額・TOPIX 終値をスナップショットとして毎週追記する。
入出金（予算変更）があった週はポートフォリオ週次リターンが歪む — note 列に
手動で記録し、本文の注記で明示する運用。
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.config.constants import CIRCUIT_BREAKER_LOSS_JPY, SHEET_TRADES  # noqa: E402

TOPIX_ETF = "1306.T"

# trades タブのスキーマ（sheets_repository._TRADES_HEADER と同値）
_TRADES_HEADER = [
    "run_id", "ticker", "side", "shares", "price",
    "executed_at", "pnl_jpy", "exit_reason",
]


def trades_in_week(trade_rows: list[list[str]], since: date) -> list[dict[str, str]]:
    """trades タブの行から since 以降に約定した行を dict で返す。"""
    out = []
    for raw in trade_rows:
        if not raw or len(raw) < 6:
            continue
        r = dict(zip(_TRADES_HEADER, list(raw) + [""] * (len(_TRADES_HEADER) - len(raw))))  # noqa: B905
        if r["executed_at"][:10] >= since.isoformat():
            out.append(r)
    return out


def weekly_returns(
    snapshots: list[dict[str, str]], equity: Decimal, topix: Decimal
) -> tuple[Decimal, Decimal] | None:
    """前週スナップショット比の (ポート週次リターン%, TOPIX 週次リターン%)。

    前週行がない（初回）か、前週の値が不正なら None。
    """
    if not snapshots:
        return None
    prev = snapshots[-1]
    try:
        prev_equity = Decimal(prev["equity_jpy"])
        prev_topix = Decimal(prev["topix_close"])
    except Exception:
        return None
    if prev_equity <= 0 or prev_topix <= 0:
        return None
    port = (equity / prev_equity - 1) * 100
    tpx = (topix / prev_topix - 1) * 100
    return port.quantize(Decimal("0.01")), tpx.quantize(Decimal("0.01"))


def build_summary(
    *,
    today: date,
    week_trades: list[dict[str, str]],
    holdings: list[tuple[str, int, Decimal, Decimal]],  # (ticker, shares, entry, latest)
    cash_jpy: Decimal,
    equity_jpy: Decimal,
    cumulative_pnl_jpy: Decimal,
    cb_net_loss_jpy: Decimal,
    topix_close: Decimal,
    returns: tuple[Decimal, Decimal] | None,
    screener_counts: list[int],
    prev_note: str = "",
) -> str:
    lines = [f":calendar: *[S-Quant] 週次サマリー*（〜{today}）", ""]

    # 今週の売却
    if week_trades:
        lines.append("*■ 今週の売却*")
        for t in week_trades:
            pnl = Decimal(t["pnl_jpy"]) if t["pnl_jpy"] else Decimal("0")
            lines.append(
                f"• {t['executed_at'][:10]} {t['ticker']} ×{t['shares']}株 "
                f"@ ¥{t['price']}（{t['exit_reason']}）PnL ¥{int(pnl):+,}"
            )
    else:
        lines.append("*■ 今週の売却*: なし")

    # 保有
    if holdings:
        lines.append("*■ 現在の保有*")
        for ticker, shares, entry, latest in holdings:
            chg = (latest / entry - 1) * 100
            lines.append(
                f"• {ticker} ×{shares}株 取得 ¥{entry} → ¥{latest}（{chg:+.1f}%）"
            )
    else:
        lines.append("*■ 現在の保有*: なし（IDLE）")

    # 資産・PnL・CB
    lines += [
        "",
        f"*■ 評価額*: ¥{int(equity_jpy):,}（現金 ¥{int(cash_jpy):,}）",
        f"*■ 累積 PnL*: ¥{int(cumulative_pnl_jpy):+,}",
    ]
    margin = CIRCUIT_BREAKER_LOSS_JPY - cb_net_loss_jpy
    lines.append(
        f"*■ CB 余裕*: ¥{int(margin):,} / ¥{int(CIRCUIT_BREAKER_LOSS_JPY):,}"
        f"（純損失 ¥{int(cb_net_loss_jpy):,}）"
    )

    # 対 TOPIX
    if returns is not None:
        port, tpx = returns
        lines.append(
            f"*■ 週次リターン*: ポート {port:+.2f}% / TOPIX(1306) {tpx:+.2f}% "
            f"= *超過 {port - tpx:+.2f}pt*"
        )
        if prev_note:
            lines.append(f"　※ 前週 note: {prev_note}（入出金があった週の超過は参考値）")
    else:
        lines.append(
            f"*■ 週次リターン*: 初回スナップショット（TOPIX(1306) ¥{topix_close}）— 比較は来週から"
        )

    # ファネル
    if screener_counts:
        trend = " → ".join(str(c) for c in screener_counts)
        lines.append(f"*■ スクリーニング通過数（直近5営業日）*: {trend}")
    else:
        lines.append(
            "*■ スクリーニング通過数*: 記録なし（保有中は IDLE スキャンが走らないため）"
        )

    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="週次サマリーを Slack に配信する")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="標準出力のみ（weekly_log 追記なし・Slack 送信なし）",
    )
    args = parser.parse_args()

    from squant.config.settings import Settings
    from squant.infrastructure.jquants_client import JQuantsClient
    from squant.infrastructure.sheets_client import GoogleSheetsClient
    from squant.infrastructure.sheets_repository import SheetsStateRepository
    from squant.infrastructure.slack_notifier import SlackNotifier
    from squant.utils.jst import now_jst
    from squant.utils.logging import get_logger

    logger = get_logger(__name__)
    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    repo = SheetsStateRepository(client)
    market = JQuantsClient(
        api_key=settings.jquants_api_key, requests_per_minute=settings.jquants_rpm
    )

    today = now_jst().date()
    since = today - timedelta(days=7)

    portfolio = repo.load_portfolio()
    cb = repo.load_circuit_breaker()
    snapshots = repo.load_weekly_snapshots()

    # Idempotency: same-day rerun (workflow_dispatch retry) must not duplicate
    # the snapshot, and must compare against last week, not this morning's row.
    already_logged_today = bool(snapshots) and snapshots[-1].get("date") == today.isoformat()
    if already_logged_today:
        snapshots = snapshots[:-1]

    # 価格取得: 保有銘柄 + TOPIX ETF。個別銘柄の取得失敗は entry_price で概算。
    tickers = [p.ticker for p in portfolio.positions] + [TOPIX_ETF]
    adj, _ = market.fetch_ohlcv(tickers, today - timedelta(days=14), today)

    def latest_close(ticker: str) -> Decimal | None:
        try:
            series = adj[ticker].dropna()
            return Decimal(str(round(float(series.iloc[-1]), 1)))
        except Exception:
            return None

    topix_close = latest_close(TOPIX_ETF)
    if topix_close is None:
        logger.error("TOPIX ETF price unavailable — aborting weekly summary")
        SlackNotifier(settings.slack_webhook_url).send_error(
            "週次サマリー失敗", "TOPIX(1306.T) の価格が取得できませんでした。"
        )
        return 1

    holdings = []
    note = ""
    equity = portfolio.cash_jpy
    for p in portfolio.positions:
        latest = latest_close(p.ticker)
        if latest is None:
            latest = p.entry_price
            note = f"{p.ticker} は価格取得失敗のため取得単価で概算"
        holdings.append((p.ticker, p.shares, p.entry_price, latest))
        equity += latest * p.shares

    rows = client.read_all(SHEET_TRADES)
    week_trades = trades_in_week(rows[1:] if rows else [], since)
    returns = weekly_returns(snapshots, equity, topix_close)
    prev_note = snapshots[-1].get("note", "") if snapshots else ""

    report = build_summary(
        today=today,
        week_trades=week_trades,
        holdings=holdings,
        cash_jpy=portfolio.cash_jpy,
        equity_jpy=equity,
        cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
        cb_net_loss_jpy=cb.cumulative_loss_jpy,
        topix_close=topix_close,
        returns=returns,
        screener_counts=repo.load_recent_screener_counts(5),
        prev_note=prev_note,
    )

    if args.dry_run:
        print("[dry-run] weekly_log 追記・Slack 送信なし\n")
        print(report)
        return 0

    if not already_logged_today:
        repo.append_weekly_snapshot(
            log_date=today, equity_jpy=equity, topix_close=topix_close,
            cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            cb_net_loss_jpy=cb.cumulative_loss_jpy, note=note,
        )

    print(report)
    SlackNotifier(settings.slack_webhook_url).send(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
