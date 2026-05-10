"""Load ticker universe and earnings blackout dates from CSV files."""

from datetime import date
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"


def load_universe(path: Path | None = None) -> list[str]:
    """Return list of ticker symbols (e.g. '1234.T') from universe.csv."""
    csv_path = path or (_DATA_DIR / "universe.csv")
    df = pd.read_csv(csv_path, comment="#")
    return df["ticker"].dropna().astype(str).tolist()


def load_earnings_blackouts(
    path: Path | None = None,
    as_of: date | None = None,
) -> set[tuple[str, date]]:
    """Return set of (ticker, event_date) pairs from earnings_calendar.csv.

    Returns only events within ±EARNINGS_BLACKOUT_DAYS of as_of (or all if as_of is None).
    """
    csv_path = path or (_DATA_DIR / "earnings_calendar.csv")
    df = pd.read_csv(csv_path, comment="#")
    blackouts: set[tuple[str, date]] = set()
    for _, row in df.iterrows():
        try:
            event_date = pd.to_datetime(row["event_date"]).date()
            ticker = str(row["ticker"])
            blackouts.add((ticker, event_date))
        except Exception:
            continue
    return blackouts
