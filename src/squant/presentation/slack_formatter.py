"""Slack Block Kit message builders for trading notifications."""

from decimal import Decimal

from squant.domain.enums import ExitReason
from squant.domain.models import ExitDecision, Signal


def format_buy_signal(signal: Signal) -> tuple[str, list[dict]]:
    """Return (fallback_text, blocks) for a BUY signal notification."""
    cancel_yen = int(signal.cancel_above_price)
    stop_yen = int(signal.stop_loss_price)

    text = (
        f"[BUY SIGNAL] {signal.ticker} ×{signal.shares}株 "
        f"参照値¥{int(signal.reference_price)} キャンセル条件: 寄付き>¥{cancel_yen}"
    )

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":chart_with_upwards_trend: BUY SIGNAL"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*銘柄*\n{signal.ticker}"},
                {"type": "mrkdwn", "text": f"*株数*\n{signal.shares}株"},
                {"type": "mrkdwn", "text": f"*参照値(前日終値)*\n¥{int(signal.reference_price)}"},
                {"type": "mrkdwn", "text": f"*RSI(14)*\n{signal.rsi:.1f}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":warning: *キャンセル条件*\n"
                    f"寄付き価格が *¥{cancel_yen}* を超えた場合は発注しないこと\n"
                    f"（前日終値 ¥{int(signal.reference_price)} × 1.02）"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":stopwatch: *損切ライン（別途逆指値注文を入れること）*\n"
                    f"¥{stop_yen}（エントリー価格 × 0.975）"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*発注後、Sheetsに記入*\n"
                    "`actual_entry_price` = 約定価格\n"
                    "`actual_shares` = 約定株数\n"
                    "`execution_status` = `FILLED` または `CANCELLED`"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"シグナル理由: {signal.reason}"},
                {"type": "mrkdwn", "text": f"生成: {signal.generated_at.strftime('%Y-%m-%d %H:%M JST')}"},
            ],
        },
    ]
    return text, blocks


def format_no_signal() -> tuple[str, list[dict]]:
    text = "[S-Quant] 本日のシグナル: なし（条件を満たす銘柄なし）"
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":white_circle: *本日のシグナル: なし*\n条件を満たす銘柄が見つかりませんでした。"},
        }
    ]
    return text, blocks


def format_exit_signal(ticker: str, exit_decision: ExitDecision, current_close: Decimal) -> tuple[str, list[dict]]:
    reason_map = {
        ExitReason.STOP_LOSS: ":red_circle: 損切（ハードストップ）",
        ExitReason.TRAILING_STOP: ":orange_circle: トレイリングストップ",
        ExitReason.TIME_STOP: ":hourglass: タイムストップ（5営業日経過）",
        ExitReason.TAKE_PROFIT: ":white_check_mark: 利確（スプレッド後純利益+7%）",
        ExitReason.MANUAL: ":blue_circle: 手動決済",
    }
    reason_label = reason_map.get(exit_decision.reason, str(exit_decision.reason))

    text = f"[EXIT SIGNAL] {ticker} — {reason_label}"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":door: EXIT SIGNAL"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*銘柄*\n{ticker}"},
                {"type": "mrkdwn", "text": f"*理由*\n{reason_label}"},
                {"type": "mrkdwn", "text": f"*現在値*\n¥{int(current_close)}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":notepad_spiral: {exit_decision.note}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*売却後、Sheetsに記入*\n"
                    "`execution_status` = `FILLED`\n"
                    "`actual_entry_price` = 約定価格"
                ),
            },
        },
    ]
    return text, blocks


def format_hold_status(ticker: str, days_held: int, current_close: Decimal, trailing_stop: Decimal) -> tuple[str, list[dict]]:
    text = f"[HOLD] {ticker} — {days_held}日目 現在値¥{int(current_close)}"
    blocks = [
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*状態*\n保有中 ({days_held}/5日)"},
                {"type": "mrkdwn", "text": f"*銘柄*\n{ticker}"},
                {"type": "mrkdwn", "text": f"*現在値*\n¥{int(current_close)}"},
                {"type": "mrkdwn", "text": f"*トレイリングストップ*\n¥{int(trailing_stop)}"},
            ],
        }
    ]
    return text, blocks


def format_settling(ticker: str, settle_date: str) -> tuple[str, list[dict]]:
    text = f"[SETTLING] {ticker} — 受渡待ち（解除: {settle_date}）"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":bank: *受渡待ち*\n{ticker} の売却資金は {settle_date} に解除されます。",
            },
        }
    ]
    return text, blocks


def format_circuit_breaker() -> tuple[str, list[dict]]:
    text = "[S-Quant] サーキットブレーカー発動 — 全取引停止"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":rotating_light: *サーキットブレーカー発動*\n累積損失が¥30,000に達したため、全取引を停止しました。\n再開するには Sheets の `circuit_breaker` タブで `is_tripped = False` に変更してください。",
            },
        }
    ]
    return text, blocks


# ── Multi-position summary helpers (2026-05-30) ────────────────────────────

def format_buy_signals_summary(signals: list[Signal]) -> tuple[str, list[dict]]:
    """Summarise N BUY signals into a single notification.

    Used when idle_pipeline queues multiple pending signals in one run
    (Phase 1 = 2 stocks, Phase 2/3 = 3 stocks).
    """
    if not signals:
        return format_no_signal()
    if len(signals) == 1:
        return format_buy_signal(signals[0])

    headers = ", ".join(s.ticker for s in signals)
    text = f"[BUY SIGNALS x{len(signals)}] {headers}"

    fields = []
    for s in signals:
        fields.append(
            {"type": "mrkdwn", "text": (
                f"*{s.ticker}* ×{s.shares}株\n"
                f"参照値: ¥{int(s.reference_price)}\n"
                f"キャンセル条件: 寄付き > ¥{int(s.cancel_above_price)}\n"
                f"損切ライン: ¥{int(s.stop_loss_price)}"
            )}
        )

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":chart_with_upwards_trend: BUY SIGNALS × {len(signals)}"},
        },
        {"type": "section", "fields": fields},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*各銘柄について発注後、Sheets の pending_signals タブに記入してください*\n"
                    "`ticker` 列で各銘柄を識別し、`actual_entry_price` / `actual_shares` / `execution_status` を更新。"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn",
                 "text": f"生成: {signals[0].generated_at.strftime('%Y-%m-%d %H:%M JST')}"},
            ],
        },
    ]
    return text, blocks


def format_hold_statuses_summary(items: list[tuple[str, int, Decimal, Decimal]]) -> tuple[str, list[dict]]:
    """Summarise N HOLD statuses into one notification.

    items: list of (ticker, days_held, current_close, trailing_stop)
    """
    if not items:
        return "[S-Quant] 保有なし", [
            {"type": "section", "text": {"type": "mrkdwn", "text": ":white_circle: 保有なし"}}
        ]
    if len(items) == 1:
        ticker, days_held, current_close, trailing_stop = items[0]
        return format_hold_status(ticker, days_held, current_close, trailing_stop)

    tickers = ", ".join(t for t, _, _, _ in items)
    text = f"[HOLD x{len(items)}] {tickers}"

    fields = []
    for ticker, days_held, current_close, trailing_stop in items:
        fields.append({"type": "mrkdwn", "text": (
            f"*{ticker}* ({days_held}/5日)\n"
            f"現在値: ¥{int(current_close)}\n"
            f"トレーリング: ¥{int(trailing_stop)}"
        )})

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":briefcase: 保有銘柄 × {len(items)}"},
        },
        {"type": "section", "fields": fields},
    ]
    return text, blocks
