"""Ledger invariant check — 会計恒等式の毎晩検証（検証強化 V-2）.

帳簿系の欠陥（F-1 型: 判定ロジックと実損益の乖離）はコードレビューより
恒等式が先に検出する、という原則の実装。watchdog（23:45 JST）の後段で
毎晩実行され、違反があれば Slack アラート + exit 1（workflow が赤になる）。

検証する不変条件（すべて Decimal 厳密比較）:
1. portfolio.cumulative_pnl_jpy == Σ(trades タブの pnl_jpy)
   … 表示用累積と取引台帳の一致
2. cash + Σ(保有ポジションの取得原価) == 初期資本(BUDGET_JPY) + 累積実現損益
   … 現金の増減がすべて記録済み取引で説明できること
3. (warn-only) circuit_breaker.cumulative_loss_jpy == -Σ(pnl)
   … CB 手動リセット後は成立しなくなるため警告表示のみ（判定には使わない）

前提: 入出金（予算変更）時は BUDGET_JPY（.env / GHA secret）が param-change
フローで同期される運用。ズレたらこのチェックが検出する（それも仕様）。
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.config.constants import SHEET_TRADES  # noqa: E402

# trades タブのスキーマ（sheets_repository._TRADES_HEADER と同値）
_PNL_COL = 6  # pnl_jpy


def sum_trades_pnl(trade_rows: list[list[str]]) -> Decimal:
    """trades タブの実現損益合計。pnl 空欄（BUY 等）は 0 扱い。"""
    total = Decimal("0")
    for raw in trade_rows:
        if not raw or len(raw) <= _PNL_COL or not str(raw[_PNL_COL]).strip():
            continue
        total += Decimal(str(raw[_PNL_COL]))
    return total


def check_ledger(
    *,
    cash_jpy: Decimal,
    held_cost_jpy: Decimal,
    cumulative_pnl_jpy: Decimal,
    trades_pnl_sum: Decimal,
    initial_capital_jpy: Decimal,
) -> list[str]:
    """恒等式の違反リストを返す（空 = 健全）。"""
    violations: list[str] = []

    if cumulative_pnl_jpy != trades_pnl_sum:
        violations.append(
            f"[1] 累積PnL不一致: portfolio.cumulative_pnl=¥{cumulative_pnl_jpy} "
            f"≠ Σtrades.pnl=¥{trades_pnl_sum}（差 ¥{cumulative_pnl_jpy - trades_pnl_sum}）"
        )

    lhs = cash_jpy + held_cost_jpy
    rhs = initial_capital_jpy + cumulative_pnl_jpy
    if lhs != rhs:
        violations.append(
            f"[2] 現金恒等式不一致: 現金¥{cash_jpy} + 保有原価¥{held_cost_jpy} = ¥{lhs} "
            f"≠ 初期資本¥{initial_capital_jpy} + 累積PnL¥{cumulative_pnl_jpy} = ¥{rhs}"
            f"（差 ¥{lhs - rhs}。入出金が BUDGET_JPY 未反映の可能性も）"
        )

    return violations


def main() -> int:
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

    portfolio = repo.load_portfolio()
    cb = repo.load_circuit_breaker()
    rows = client.read_all(SHEET_TRADES)
    trades_pnl = sum_trades_pnl(rows[1:] if rows else [])

    held_cost = sum(
        (p.entry_price * p.shares for p in portfolio.positions), Decimal("0")
    )

    violations = check_ledger(
        cash_jpy=portfolio.cash_jpy,
        held_cost_jpy=held_cost,
        cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
        trades_pnl_sum=trades_pnl,
        initial_capital_jpy=settings.budget_jpy,
    )

    # Check 3 (warn-only): CB net loss vs realized PnL. Manual CB resets break
    # this identity by design, so it never fails the run.
    if cb.cumulative_loss_jpy != -trades_pnl:
        print(
            f"INFO: CB純損失 ¥{cb.cumulative_loss_jpy} ≠ -Σpnl ¥{-trades_pnl} "
            "（手動リセット後なら正常）"
        )

    if not violations:
        print(
            f"OK: ledger invariants hold "
            f"(cash ¥{portfolio.cash_jpy} + cost ¥{held_cost} "
            f"= capital ¥{settings.budget_jpy} + pnl ¥{portfolio.cumulative_pnl_jpy})"
        )
        return 0

    msg = (
        ":rotating_light: *[S-Quant] 会計恒等式違反* — 帳簿の不整合を検出しました。"
        "取引記録・現金・ポジションのどれかがズレています。手動確認が必要です。\n"
        + "\n".join(f"• {v}" for v in violations)
    )
    print(msg, file=sys.stderr)
    SlackNotifier(settings.slack_webhook_url).send(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
