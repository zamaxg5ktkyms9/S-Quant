"""Unit tests for A-6 backup_sheets helpers."""

import csv
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from backup_sheets import ALL_TABS, write_csv


class TestWriteCsv:
    def test_roundtrip_with_special_chars(self, tmp_path: Path):
        """カンマ・改行・日本語を含むセルが CSV 経由で失われない"""
        rows = [
            ["ticker", "note"],
            ["2201.T", "値に,カンマ"],
            ["9999.T", "改行\nあり"],
        ]
        path = tmp_path / "test.csv"
        write_csv(path, rows)
        with path.open(newline="", encoding="utf-8") as f:
            assert list(csv.reader(f)) == rows

    def test_empty_tab_creates_empty_file(self, tmp_path: Path):
        path = tmp_path / "empty.csv"
        write_csv(path, [])
        assert path.exists()
        assert path.read_text() == ""

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "a" / "b" / "x.csv"
        write_csv(path, [["h"]])
        assert path.exists()


class TestAllTabs:
    def test_covers_every_sheet_constant(self):
        """constants.py の SHEET_* 定数がすべてバックアップ対象に含まれる。

        新タブを追加したら ALL_TABS にも足すこと（このテストが検知する）。
        "snapshots" は未使用の遺物定数（コード中に参照なし）のため対象外 —
        read_all は存在しないタブを自動作成する副作用があり、含めると本番に
        空タブを作ってしまう。
        """
        from squant.config import constants

        sheet_names = {
            v for k, v in vars(constants).items() if k.startswith("SHEET_")
        }
        assert sheet_names - {"snapshots"} == set(ALL_TABS)
