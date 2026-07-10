"""Unit tests for A-5: remind_pending / confirm_entry helper functions."""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

import pytest
from confirm_entry import ConfirmationError, find_pending, validate_fill
from remind_pending import build_reminder, find_stale_pendings

from squant.domain.enums import ExecutionStatus
from squant.domain.models import PendingSignal, Signal

JST = timezone(timedelta(hours=9))
TODAY = date(2026, 7, 13)


def _pending(
    ticker: str = "2201.T",
    generated_on: date = date(2026, 7, 10),
    status: ExecutionStatus = ExecutionStatus.PENDING,
    reference_price: str = "2733.5",
    shares: int = 100,
) -> PendingSignal:
    ref = Decimal(reference_price)
    signal = Signal(
        ticker=ticker,
        reference_price=ref,
        shares=shares,
        cancel_above_price=ref * Decimal("1.02"),
        stop_loss_price=ref * Decimal("0.97"),
        rsi=55.0,
        reason="MA cross",
        generated_at=datetime.combine(generated_on, datetime.min.time(), JST).replace(hour=20, minute=35),
    )
    return PendingSignal(signal=signal, execution_status=status)


# ── remind_pending.find_stale_pendings ─────────────────────────────────────────

class TestFindStalePendings:
    def test_old_pending_is_stale(self):
        """前営業日以前に生成された PENDING は催促対象"""
        stale = find_stale_pendings((_pending(generated_on=date(2026, 7, 10)),), TODAY)
        assert len(stale) == 1

    def test_today_pending_not_stale(self):
        """当日生成分（今夜のラン発報）は対象外"""
        stale = find_stale_pendings((_pending(generated_on=TODAY),), TODAY)
        assert stale == []

    def test_filled_not_stale(self):
        """FILLED 済みは催促しない"""
        stale = find_stale_pendings(
            (_pending(status=ExecutionStatus.FILLED),), TODAY
        )
        assert stale == []

    def test_cancelled_not_stale(self):
        """CANCELLED 済みは催促しない"""
        stale = find_stale_pendings(
            (_pending(status=ExecutionStatus.CANCELLED),), TODAY
        )
        assert stale == []

    def test_mixed(self):
        """混在時は古い PENDING のみ抽出"""
        pendings = (
            _pending(ticker="2201.T", generated_on=date(2026, 7, 10)),
            _pending(ticker="9999.T", status=ExecutionStatus.FILLED),
            _pending(ticker="8888.T", generated_on=TODAY),
        )
        stale = find_stale_pendings(pendings, TODAY)
        assert [p.signal.ticker for p in stale] == ["2201.T"]


class TestBuildReminder:
    def test_message_contains_ticker_and_cli(self):
        msg = build_reminder([_pending()])
        assert "2201.T" in msg
        assert "confirm_entry.py" in msg
        assert "自動キャンセル" in msg
        assert "×100株" in msg


# ── confirm_entry.find_pending ────────────────────────────────────────────────

class TestFindPending:
    def test_exact_match(self):
        p = find_pending((_pending(),), "2201.T")
        assert p.signal.ticker == "2201.T"

    def test_bare_code_matches_t_suffix(self):
        """'2201' でも '2201.T' にマッチする"""
        p = find_pending((_pending(),), "2201")
        assert p.signal.ticker == "2201.T"

    def test_not_found_raises(self):
        with pytest.raises(ConfirmationError, match="見つかりません"):
            find_pending((_pending(),), "9999.T")

    def test_empty_pendings_raises(self):
        with pytest.raises(ConfirmationError, match="見つかりません"):
            find_pending((), "2201.T")

    def test_already_filled_raises(self):
        with pytest.raises(ConfirmationError, match="FILLED"):
            find_pending((_pending(status=ExecutionStatus.FILLED),), "2201.T")

    def test_already_cancelled_raises(self):
        with pytest.raises(ConfirmationError, match="CANCELLED"):
            find_pending((_pending(status=ExecutionStatus.CANCELLED),), "2201.T")


# ── confirm_entry.validate_fill ───────────────────────────────────────────────

class TestValidateFill:
    def test_clean_fill_no_warnings(self):
        warnings = validate_fill(_pending(), Decimal("2717.5"), 100, force=False)
        assert warnings == []

    def test_zero_price_rejected(self):
        with pytest.raises(ConfirmationError, match="価格が不正"):
            validate_fill(_pending(), Decimal("0"), 100, force=False)

    def test_negative_shares_rejected(self):
        with pytest.raises(ConfirmationError, match="株数が不正"):
            validate_fill(_pending(), Decimal("2717.5"), -100, force=False)

    def test_fat_finger_price_rejected_without_force(self):
        """桁誤り（10倍）は --force なしで拒否"""
        with pytest.raises(ConfirmationError, match="乖離"):
            validate_fill(_pending(), Decimal("27175"), 100, force=False)

    def test_fat_finger_price_downgraded_with_force(self):
        warnings = validate_fill(_pending(), Decimal("27175"), 100, force=True)
        assert any("乖離" in w for w in warnings)

    def test_price_above_cancel_threshold_rejected_without_force(self):
        """キャンセル上限超え（乖離5%以内）は --force なしで拒否"""
        # cancel_above = 2733.5*1.02 ≈ 2788.2; +3% ≈ 2815.5 は乖離チェックは通る
        with pytest.raises(ConfirmationError, match="キャンセル上限"):
            validate_fill(_pending(), Decimal("2815.5"), 100, force=False)

    def test_shares_mismatch_is_warning_only(self):
        warnings = validate_fill(_pending(), Decimal("2717.5"), 90, force=False)
        assert any("株数" in w for w in warnings)
