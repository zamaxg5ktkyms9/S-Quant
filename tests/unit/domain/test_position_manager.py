"""Tests for position exit rule evaluation (W1best: 単元株・ザラ場モード・+5% TP・2.5×ATR)."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from squant.config.constants import ATR_PERIOD, ATR_TRAILING_MULTIPLIER, GAP_UP_CANCEL_THRESHOLD
from squant.domain.enums import ExitReason
from squant.domain.indicators import atr
from squant.domain.models import Position
from squant.domain.position_manager import evaluate_exit, should_cancel_gap_up
from squant.domain.quantity_calculator import compute_take_profit_price


def make_position(
    entry_price: float = 500.0,
    entry_date: date = date(2026, 5, 7),
    stop_loss_rate: float = 0.025,
) -> Position:
    ep = Decimal(str(entry_price))
    stop = ep * (1 - Decimal(str(stop_loss_rate)))
    return Position(
        ticker="1234.T",
        shares=100,
        entry_price=ep,
        intended_entry_price=ep,
        entry_date=entry_date,
        stop_loss_price=stop,
        trailing_stop_price=stop,
        highest_price_since_entry=ep,
        time_stop_date=date(2026, 5, 14),
    )


def make_ohlcv(n: int = 20, base: float = 500.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    idx = pd.bdate_range(end="2026-05-11", periods=n)
    close = pd.Series([base] * n, index=idx, dtype=float)
    high = close + 5.0
    low = close - 5.0
    return high, low, close


@pytest.fixture
def tp_5pct(monkeypatch):
    """TP を +5% で有効化する（本番デフォルトは TP なしのため、TP ロジック検証用）。"""
    import squant.domain.position_manager as pm

    monkeypatch.setattr(
        pm, "compute_take_profit_price",
        lambda entry, target_net_rate=None, spread_rate=None: entry * Decimal("1.05"),
    )


# ── 終値モード（intraday_*=None）────────────────────────────────────────────

class TestTimeStopCloseMode:
    def test_exit_after_5_trading_days_when_no_other_trigger(self):
        # entry 2026-05-07, 5営業日後 = 2026-05-14。値動きはニュートラル
        pos = make_position(entry_date=date(2026, 5, 7))
        high, low, close = make_ohlcv()
        decision = evaluate_exit(pos, date(2026, 5, 14), Decimal("500"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TIME_STOP

    def test_no_exit_at_4_trading_days_neutral_price(self):
        pos = make_position(entry_date=date(2026, 5, 7))
        high, low, close = make_ohlcv()
        decision = evaluate_exit(pos, date(2026, 5, 13), Decimal("500"), high, low, close)
        assert not decision.should_exit


class TestStopLossCloseMode:
    def test_exit_below_stop_loss_price(self):
        pos = make_position(entry_price=500.0)
        # stop = 487.5; 485 < 487.5 → stop-loss
        high, low, close = make_ohlcv(base=485.0)
        decision = evaluate_exit(pos, date(2026, 5, 11), Decimal("485"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS

    def test_no_exit_above_stop_loss(self):
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=490.0)
        decision = evaluate_exit(pos, date(2026, 5, 11), Decimal("490"), high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.STOP_LOSS


class TestTakeProfitCloseMode:
    def test_default_tp_disabled(self):
        # 2026-07-05: 本番デフォルトは TP なし
        assert compute_take_profit_price(Decimal("500")) is None

    def test_tp_never_fires_when_disabled(self):
        """TP 無効（デフォルト）では価格が大きく上でも TAKE_PROFIT にならない。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=560.0)  # +12%
        decision = evaluate_exit(pos, date(2026, 5, 11), Decimal("560"), high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.TAKE_PROFIT

    def test_exit_at_take_profit(self, tp_5pct):
        pos = make_position(entry_price=500.0)
        tp_price = pos.entry_price * Decimal("1.05")  # 525
        exit_price = Decimal(str(round(float(tp_price) + 1.0, 1)))
        high, low, close = make_ohlcv(base=float(exit_price))
        decision = evaluate_exit(pos, date(2026, 5, 11), exit_price, high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT

    def test_no_exit_below_take_profit(self, tp_5pct):
        pos = make_position(entry_price=500.0)
        price = Decimal("520")  # +4%、TP(525)未満
        high, low, close = make_ohlcv(base=float(price))
        decision = evaluate_exit(pos, date(2026, 5, 11), price, high, low, close)
        if decision.should_exit:
            assert decision.reason != ExitReason.TAKE_PROFIT


# ── ザラ場モード（intraday_high/low 渡し）─────────────────────────────────

class TestIntradayMode:
    def test_intraday_low_triggers_stop_at_stop_price(self):
        """日中安値がストップ価格を割り込んだら、ストップ価格で約定。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=495.0)
        # 終値¥495だが日中安値¥486で逆指値発動
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("495"), high, low, close,
            intraday_high=Decimal("497"), intraday_low=Decimal("486"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS
        assert decision.exit_price == pos.stop_loss_price

    def test_intraday_high_triggers_tp_at_tp_price(self, tp_5pct):
        """日中高値が利確価格に到達したら、利確価格で約定（TP有効時）。"""
        pos = make_position(entry_price=500.0)
        # 終値¥520だが日中高値¥532でTP(525)発動
        high, low, close = make_ohlcv(base=520.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("520"), high, low, close,
            intraday_high=Decimal("532"), intraday_low=Decimal("518"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT
        assert decision.exit_price == pos.entry_price * Decimal("1.05")

    def test_intraday_no_trigger_when_in_range(self):
        """日中の高安がストップにもTPにも触れない場合は継続。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=505.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("505"), high, low, close,
            intraday_high=Decimal("510"), intraday_low=Decimal("498"),
        )
        assert not decision.should_exit


def make_trailing_position(
    entry_price: float = 500.0,
    trailing_stop_price: float = 570.0,
    highest: float = 575.0,
    stop_loss_rate: float = 0.025,
    entry_date: date = date(2026, 5, 11),
) -> Position:
    """トレーリングストップが hard stop より上にラチェット済みのポジション。"""
    ep = Decimal(str(entry_price))
    return Position(
        ticker="1234.T",
        shares=100,
        entry_price=ep,
        intended_entry_price=ep,
        entry_date=entry_date,
        stop_loss_price=ep * (1 - Decimal(str(stop_loss_rate))),
        trailing_stop_price=Decimal(str(trailing_stop_price)),
        highest_price_since_entry=Decimal(str(highest)),
        time_stop_date=date(2026, 5, 18),
    )


# ── 境界値（<= の等号側）─────────────────────────────────────────────────

class TestStopBoundary:
    def test_intraday_low_exactly_at_stop_triggers(self):
        """日中安値がストップ価格に「ちょうど一致」で発動する（<= の等号側）。

        mutation testing (V-3): `intraday_low <= stop` を `<` に変える変異は、
        ちょうど一致でのテストがないと生き残る。逆指値は指定価格到達で約定する。
        """
        pos = make_position(entry_price=500.0)  # stop = 487.5
        high, low, close = make_ohlcv(base=495.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("495"), high, low, close,
            intraday_high=Decimal("498"), intraday_low=Decimal("487.5"),  # == stop ちょうど
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS
        assert decision.exit_price == pos.stop_loss_price

    def test_close_exactly_at_stop_triggers(self):
        """終値モードでも終値がストップに一致で発動する。"""
        pos = make_position(entry_price=500.0)  # stop = 487.5
        high, low, close = make_ohlcv(base=487.5)
        decision = evaluate_exit(pos, date(2026, 5, 11), Decimal("487.5"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS


# ── トレーリングストップ「単独」発動（hard stop に触れない）──────────────

class TestTrailingStopOnly:
    def test_intraday_trailing_stop_fires_at_effective_stop(self):
        """トレーリングが hard stop より上にある状態で、日中安値がトレーリングに
        到達したら TRAILING_STOP でトレーリング価格約定（hard stop には触れない）。

        mutation testing (V-3): should_exit=True→False, effective_stop の <= → <,
        exit_price=effective_stop→None といった変異はこの経路のテストがないと生き残る。
        """
        pos = make_trailing_position()  # trailing 570, stop 487.5, highest 575
        high, low, close = make_ohlcv(base=575.0)  # ATR≈10 → new_stop=575-30=545 < 570
        decision = evaluate_exit(
            pos, date(2026, 5, 12), Decimal("572"), high, low, close,
            intraday_high=Decimal("575"), intraday_low=Decimal("570"),  # == trailing
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.TRAILING_STOP
        assert decision.exit_price == Decimal("570")
        assert decision.updated_trailing_stop == Decimal("570")

    def test_close_mode_trailing_stop_fires(self):
        """終値モードでも終値がトレーリングに到達したら TRAILING_STOP。

        mutation testing (V-3): `not intraday_mode` の反転や should_exit=True→False を塞ぐ。
        """
        pos = make_trailing_position()
        high, low, close = make_ohlcv(base=575.0)
        decision = evaluate_exit(pos, date(2026, 5, 12), Decimal("570"), high, low, close)
        assert decision.should_exit
        assert decision.reason == ExitReason.TRAILING_STOP


# ── HOLD 時のトレーリングストップ・ラチェット値 ────────────────────────

class TestTrailingRatchetValue:
    def test_hold_returns_ratcheted_trailing_stop(self):
        """HOLD 継続時、返す updated_trailing_stop は独立計算した
        「直近高値 - 乗数×ATR」（ラチェット）に一致する。

        mutation testing (V-3): HOLD 経路の updated_trailing_stop=None、および
        _compute_trailing_stop の iloc[-1]→iloc[-2] / round(...,2)→round(...,3) を塞ぐ。
        None にすると翌日のラチェット値が失われ、ストップ保護が entry 直後水準に後退する。
        """
        # 日々レンジが変動する系列を使う。平坦系列だと ATR が整数値になり
        # iloc[-1]/iloc[-2] や round(,2)/round(,3)/round(,None) の違いが観測できず、
        # それらの変異が生き残ってしまうため、意図的に非整数 ATR を作る。
        n = 30
        idx = pd.bdate_range(end="2026-05-12", periods=n)
        closes = [500 + i * 2 + (3 if i % 2 else -1) for i in range(n)]
        close = pd.Series([float(c) for c in closes], index=idx, dtype=float)
        high = close + pd.Series([2.0 + (i % 3) for i in range(n)], index=idx)
        low = close - pd.Series([1.0 + (i % 4) for i in range(n)], index=idx)
        latest_close = Decimal(str(closes[-1]))
        highest = Decimal(str(max(closes)))
        pos = make_trailing_position(trailing_stop_price=487.5, highest=float(highest))

        decision = evaluate_exit(pos, date(2026, 5, 12), latest_close, high, low, close)
        assert not decision.should_exit  # latest_close は effective_stop より十分上

        # 本番と同じ式で期待値を独立算出（iloc[-1] と round(,2) を固定）
        atr_series = atr(high, low, close, period=ATR_PERIOD)
        latest_atr = Decimal(str(round(float(atr_series.iloc[-1]), 2)))
        expected = max(pos.trailing_stop_price, latest_close - ATR_TRAILING_MULTIPLIER * latest_atr)
        assert decision.updated_trailing_stop is not None
        assert decision.updated_trailing_stop == expected


# ── ギャップアップ発注見送り閾値 ────────────────────────────────────────

class TestShouldCancelGapUp:
    def test_cancels_when_open_above_threshold(self):
        # intended 500, threshold 0.02 → 510 超で見送り
        assert should_cancel_gap_up(Decimal("500"), Decimal("511")) is True

    def test_does_not_cancel_exactly_at_threshold(self):
        """始値がちょうど閾値価格なら見送らない（> の境界、>= ではない）。"""
        assert should_cancel_gap_up(Decimal("500"), Decimal("510")) is False

    def test_does_not_cancel_below_threshold(self):
        """閾値未満では見送らない（*→/ や 1+→1-/2+ の変異を塞ぐ境界）。"""
        assert should_cancel_gap_up(Decimal("500"), Decimal("505")) is False

    def test_explicit_threshold_value(self):
        assert should_cancel_gap_up(Decimal("1000"), Decimal("1051"), threshold=Decimal("0.05")) is True
        assert should_cancel_gap_up(Decimal("1000"), Decimal("1049"), threshold=Decimal("0.05")) is False

    def test_default_threshold_matches_constant(self):
        """デフォルト閾値が定数 GAP_UP_CANCEL_THRESHOLD と一致することを固定。"""
        boundary = Decimal("500") * (1 + GAP_UP_CANCEL_THRESHOLD)
        assert should_cancel_gap_up(Decimal("500"), boundary + Decimal("0.01")) is True
        assert should_cancel_gap_up(Decimal("500"), boundary) is False


class TestExitPriority:
    def test_stop_loss_beats_take_profit_in_intraday(self, tp_5pct):
        """同日にlow≤stopとhigh≥tpが両方成立 → 保守的にストップ優先。"""
        pos = make_position(entry_price=500.0)
        high, low, close = make_ohlcv(base=510.0)
        decision = evaluate_exit(
            pos, date(2026, 5, 11), Decimal("510"), high, low, close,
            intraday_high=Decimal("532"), intraday_low=Decimal("485"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS

    def test_take_profit_beats_time_stop(self, tp_5pct):
        """ザラ場でTPに到達したらタイムストップより優先（TP有効時）。"""
        pos = make_position(entry_price=500.0, entry_date=date(2026, 5, 7))
        tp_price = pos.entry_price * Decimal("1.05")
        exit_price = Decimal(str(round(float(tp_price) + 1.0, 1)))
        high, low, close = make_ohlcv(base=float(exit_price))
        # 5営業日経過日にTPも同時成立 → TAKE_PROFITが優先
        decision = evaluate_exit(
            pos, date(2026, 5, 14), exit_price, high, low, close,
            intraday_high=exit_price, intraday_low=Decimal("525"),
        )
        assert decision.should_exit
        assert decision.reason == ExitReason.TAKE_PROFIT
