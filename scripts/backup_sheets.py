"""Sheets daily backup — 全タブを CSV で repo に書き出す（改善提案 A-6）.

Google Sheets は本番状態の単一障害点（誤編集・タブ削除・API 障害で復旧手段なし）。
平日深夜 00:30 JST に GHA から実行され、全タブを backups/sheets/<tab>.csv に
書き出す。ファイルは毎回上書きし、日次スナップショットは Git 履歴が保持する
（workflow 側で差分があるときだけ bot がコミット & push）。

復旧手順: 該当日のコミットから CSV を取り出し、Sheets に手動で貼り戻す
（bootstrap_sheet.py --apply はオーナー明示指示が必要な破壊的操作なので使わない）。
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from squant.config.constants import (  # noqa: E402
    SHEET_CIRCUIT_BREAKER,
    SHEET_FUNNEL_LOG,
    SHEET_PENDING_SIGNALS,
    SHEET_PORTFOLIO,
    SHEET_RECENT_SALES,
    SHEET_RUN_LOG,
    SHEET_SLIPPAGE_LOG,
    SHEET_TRADES,
    SHEET_WEEKLY_LOG,
)

BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups" / "sheets"

ALL_TABS = [
    SHEET_PORTFOLIO,
    SHEET_TRADES,
    SHEET_CIRCUIT_BREAKER,
    SHEET_RUN_LOG,
    SHEET_PENDING_SIGNALS,
    SHEET_RECENT_SALES,
    SHEET_FUNNEL_LOG,
    SHEET_SLIPPAGE_LOG,
    SHEET_WEEKLY_LOG,
]


def write_csv(path: Path, rows: list[list[str]]) -> None:
    """Write sheet rows to CSV. Empty tabs produce an empty file (still tracked)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def main() -> int:
    from squant.config.settings import Settings
    from squant.infrastructure.sheets_client import GoogleSheetsClient

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )

    failures: list[str] = []
    for tab in ALL_TABS:
        try:
            rows = client.read_all(tab)
            write_csv(BACKUP_DIR / f"{tab}.csv", rows)
            print(f"OK: {tab} ({len(rows)} rows)")
        except Exception as e:
            # Keep going — a partial backup beats none. The workflow surfaces
            # the non-zero exit so a broken tab is not silently skipped forever.
            failures.append(f"{tab}: {e}")
            print(f"FAILED: {tab}: {e}", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)}/{len(ALL_TABS)} tabs failed", file=sys.stderr)
        return 1
    print(f"\nAll {len(ALL_TABS)} tabs backed up to {BACKUP_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
