"""Candidate ranking: RSI ascending → volume surge descending → PBR ascending."""

from squant.domain.models import Candidate


def rank(candidates: list[Candidate], top_n: int = 1) -> list[Candidate]:
    """Return top_n candidates sorted by (RSI asc, volume_surge desc, PBR asc)."""
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (c.rsi14, -c.volume_surge_ratio, c.pbr),
    )
    return sorted_candidates[:top_n]
