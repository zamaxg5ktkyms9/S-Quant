"""Unit tests for F1 point-in-time universe generation."""

import sys
from datetime import date

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

from build_universe import (
    code_to_ticker,
    generate_universe,
    iter_quarters,
    quarter_first_trading_day,
    trading_days_before,
)


def _master_row(code, prodcat="011", mkt="プライム"):
    return {"Code": code, "ProdCat": prodcat, "MktNm": mkt}


def _bar(code, close=1000.0, va=200_000_000.0):
    return {"Code": code, "AdjC": close, "C": close, "Va": va}


class TestGenerateUniverse:
    def test_basic_filter_chain(self):
        master = [
            _master_row("72030"),                          # 採用
            _master_row("13050", prodcat="014"),           # ETF → 除外
            _master_row("99990", mkt="TOKYO PRO MARKET"),  # TPM → 除外
            _master_row("50000", mkt="東証一部"),           # 旧区分 → 採用
        ]
        bars = [[_bar("72030"), _bar("13050"), _bar("99990"), _bar("50000")]]
        assert generate_universe(master, bars) == ["5000.T", "7203.T"]

    def test_price_band(self):
        master = [_master_row("10000"), _master_row("20000"), _master_row("30000")]
        bars = [[_bar("10000", close=99.0), _bar("20000", close=3000.0),
                 _bar("30000", close=3000.5)]]
        assert generate_universe(master, bars) == ["2000.T"]

    def test_liquidity_uses_multiday_average(self):
        master = [_master_row("10000"), _master_row("20000")]
        bars = [
            [_bar("10000", va=40_000_000), _bar("20000", va=150_000_000)],
            [_bar("10000", va=180_000_000), _bar("20000", va=90_000_000)],
        ]
        # 10000: avg 110M ≥ 100M 採用 / 20000: avg 120M 採用
        assert generate_universe(master, bars) == ["1000.T", "2000.T"]
        # 閾値を上げると平均 110M の銘柄が落ちる
        assert generate_universe(master, bars, liquidity_min_jpy=115_000_000) == ["2000.T"]

    def test_price_is_last_day_close(self):
        master = [_master_row("10000")]
        bars = [
            [_bar("10000", close=5000.0)],  # 昔の日: 帯域外
            [_bar("10000", close=2500.0)],  # 最終日: 帯域内
        ]
        assert generate_universe(master, bars) == ["1000.T"]

    def test_missing_bars_excluded(self):
        master = [_master_row("10000")]
        assert generate_universe(master, [[]]) == []


class TestHelpers:
    def test_code_to_ticker(self):
        assert code_to_ticker("72030") == "7203.T"
        assert code_to_ticker("130A0") == "130A.T"   # 英字コード
        assert code_to_ticker("25935") is None       # 優先株等（末尾≠0）
        assert code_to_ticker("1301") is None

    def test_quarter_first_trading_day(self):
        assert quarter_first_trading_day(2026, 1) == date(2026, 1, 5)  # 1/1-1/3 休場
        assert quarter_first_trading_day(2026, 3) == date(2026, 7, 1)

    def test_iter_quarters(self):
        assert list(iter_quarters("2025Q3", "2026Q2")) == [
            (2025, 3), (2025, 4), (2026, 1), (2026, 2)]

    def test_trading_days_before(self):
        days = trading_days_before(date(2026, 7, 13), 5)  # 月曜
        assert len(days) == 5
        assert days[-1] == date(2026, 7, 10)  # 前営業日 = 金曜
        assert days == sorted(days)
