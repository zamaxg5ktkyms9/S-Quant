"""Tests for candidate ranking logic."""

from decimal import Decimal

import pytest

from squant.domain.models import Candidate
from squant.domain.ranking import rank


def _c(ticker: str, rsi: float, surge: float = 1.0, pbr: float = 1.0) -> Candidate:
    return Candidate(
        ticker=ticker,
        close=Decimal("500"),
        rsi14=rsi,
        volume_surge_ratio=surge,
        pbr=pbr,
        market_cap_jpy=50_000_000_000.0,
    )


class TestRank:
    def test_empty_returns_empty(self):
        assert rank([], top_n=1) == []

    def test_single_candidate_returned(self):
        c = _c("A.T", rsi=30.0)
        assert rank([c], top_n=1) == [c]

    def test_lower_rsi_wins(self):
        a = _c("A.T", rsi=40.0)
        b = _c("B.T", rsi=25.0)
        result = rank([a, b], top_n=1)
        assert result[0].ticker == "B.T"

    def test_higher_volume_surge_wins_on_rsi_tie(self):
        a = _c("A.T", rsi=30.0, surge=1.5)
        b = _c("B.T", rsi=30.0, surge=3.0)
        result = rank([a, b], top_n=1)
        assert result[0].ticker == "B.T"

    def test_lower_pbr_wins_on_rsi_and_surge_tie(self):
        a = _c("A.T", rsi=30.0, surge=2.0, pbr=1.1)
        b = _c("B.T", rsi=30.0, surge=2.0, pbr=0.6)
        result = rank([a, b], top_n=1)
        assert result[0].ticker == "B.T"

    def test_top_n_limits_output(self):
        candidates = [_c(f"{i}.T", rsi=float(i)) for i in range(5)]
        result = rank(candidates, top_n=3)
        assert len(result) == 3

    def test_top_n_larger_than_list_returns_all(self):
        candidates = [_c("A.T", rsi=30.0), _c("B.T", rsi=35.0)]
        result = rank(candidates, top_n=10)
        assert len(result) == 2

    def test_sort_order_is_rsi_ascending(self):
        candidates = [_c(f"{i}.T", rsi=float(50 - i)) for i in range(5)]
        result = rank(candidates, top_n=5)
        rsis = [c.rsi14 for c in result]
        assert rsis == sorted(rsis)
