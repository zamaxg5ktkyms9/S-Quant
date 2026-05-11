"""Test fixture helpers — build mock pandas Series from mock_yfinance.json specs."""

import json
from datetime import date
from pathlib import Path

import pandas as pd

_SPEC_PATH = Path(__file__).parent / "mock_yfinance.json"


def _load_spec() -> dict:
    with _SPEC_PATH.open() as f:
        return json.load(f)


def build_close_series(scenario: str, end_date: date = date(2026, 5, 11)) -> pd.Series:
    """Return a pd.Series of close prices for the given scenario."""
    spec = _load_spec()
    s = spec["scenarios"][scenario]
    n = s["n_bars"]
    step = s["price_step"]
    base = s["base_price"]

    prices = [round(base + i * step, 4) for i in range(n)]

    if s.get("last_price_override") is not None:
        prices[-1] = s["last_price_override"]

    idx = pd.bdate_range(end=str(end_date), periods=n)
    series = pd.Series(prices, index=idx, dtype=float)

    for i in s.get("nan_indices", []):
        if i < len(series):
            series.iloc[i] = float("nan")

    return series


def build_volume_series(scenario: str, end_date: date = date(2026, 5, 11)) -> pd.Series:
    """Return a pd.Series of volume values for the given scenario."""
    spec = _load_spec()
    s = spec["scenarios"][scenario]
    n = s["n_bars"]
    base = s["base_volume"]

    volumes = [float(base)] * n

    if s.get("last_volume_override") is not None:
        volumes[-1] = float(s["last_volume_override"])

    idx = pd.bdate_range(end=str(end_date), periods=n)
    return pd.Series(volumes, index=idx, dtype=float)


def list_scenarios() -> list[str]:
    return list(_load_spec()["scenarios"].keys())


def expected_validation(scenario: str) -> str:
    return _load_spec()["scenarios"][scenario]["expected_validation"]
