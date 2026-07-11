"""Unit tests for signal_engine.with_volume_columns (B-2 defragmentation)."""

import warnings

import pandas as pd

from squant.domain.signal_engine import with_volume_columns


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.to_datetime(["2026-07-08", "2026-07-09", "2026-07-10"])
    adj = pd.DataFrame({"A.T": [100.0, 101.0, 102.0], "B.T": [50.0, 51.0, 52.0]}, index=idx)
    vol = pd.DataFrame({"A.T": [1000, 1100, 1200], "B.T": [500, 600, 700]}, index=idx)
    return adj, vol


def _legacy(adj: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    """旧実装（per-column 代入）— 等価性の基準。"""
    out = adj.copy()
    for col in vol.columns:
        out[f"{col}_vol"] = vol[col]
    return out


class TestWithVolumeColumns:
    def test_equivalent_to_legacy_assignment(self):
        adj, vol = _frames()
        pd.testing.assert_frame_equal(
            with_volume_columns(adj, vol), _legacy(adj, vol), check_dtype=False
        )

    def test_equivalent_when_volume_index_differs(self):
        """出来高側に余分な日付があっても価格側インデックスに整列（旧挙動と同一）"""
        adj, vol = _frames()
        extra = pd.DataFrame(
            {"A.T": [999], "B.T": [999]}, index=pd.to_datetime(["2026-07-11"])
        )
        vol_extra = pd.concat([vol, extra])
        pd.testing.assert_frame_equal(
            with_volume_columns(adj, vol_extra), _legacy(adj, vol_extra), check_dtype=False
        )

    def test_no_performance_warning(self):
        adj, vol = _frames()
        # 断片化警告が出ないことを多数列で確認
        many_adj = pd.concat(
            [adj.rename(columns={c: f"{c}{i}" for c in adj.columns}) for i in range(60)],
            axis=1,
        )
        many_vol = pd.concat(
            [vol.rename(columns={c: f"{c}{i}" for c in vol.columns}) for i in range(60)],
            axis=1,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", pd.errors.PerformanceWarning)
            result = with_volume_columns(many_adj, many_vol)
        assert result.shape[1] == 240
