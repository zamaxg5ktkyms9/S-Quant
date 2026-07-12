"""F1: ポイントインタイム・バックテスト用データキャッシュの構築.

data/universe_pit/ の四半期スナップショット（union ≈ 2,700 銘柄、上場廃止含む）
について、バックテストに必要な全データを J-Quants から取得し pickle 化する。

取得戦略:
- OHLCV: `/equities/bars/daily?date=`（1コール=全市場1日分）を営業日ぶん回す
  （銘柄別取得だと union×1 コール ≈ 2,700 だが、日別なら ≈ 1,200 コールで済み
  廃止銘柄も自動的に含まれる）
- ファンダ: `/fins/summary?code=` を union 銘柄ぶん（開示履歴つき → 四半期ごとの
  as-of 参照でポイントインタイム・ファンダを実現）

中断再開: 中間結果を .backtest_cache/pit_bars_raw.pkl / pit_fins_raw.pkl に
逐次保存し、再実行時は取得済み分をスキップする。

出力（.backtest_cache/pit_data_<start>_<end>.pkl）:
    adj_close / volume        … wide DataFrame（union 銘柄）
    full_cache                … {ticker: OHLCV DataFrame}（backtest 互換）
    universe_by_quarter       … {四半期初営業日: [tickers]}
    fundamentals_by_quarter   … {四半期初営業日: DataFrame}（as-of 開示 + 時点価格）

Usage:
    python scripts/build_pit_cache.py                # 2021-07-01 〜 直近営業日
    python scripts/build_pit_cache.py --end 2026-07-10
"""
import argparse
import pickle
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd  # noqa: E402

from squant.utils.jst import is_tse_trading_day  # noqa: E402

_CACHE_DIR = Path(".backtest_cache")
_BARS_RAW = _CACHE_DIR / "pit_bars_raw.pkl"
_FINS_RAW = _CACHE_DIR / "pit_fins_raw.pkl"
_SNAP_DIR = Path("data/universe_pit")

# fins/summary から保持するフィールド（PIT ファンダ算出に必要な最小限）
_FINS_FIELDS = ("DiscDate", "EqAR", "BPS", "Eq", "ShOutFY")


def load_snapshots(snap_dir: Path = _SNAP_DIR) -> dict[date, list[str]]:
    """{四半期 as-of 日: [tickers]}。as-of はファイル1行目コメントから読む。"""
    out: dict[date, list[str]] = {}
    for p in sorted(snap_dir.glob("*.csv")):
        lines = p.read_text().splitlines()
        as_of = None
        tickers = []
        for ln in lines:
            if ln.startswith("#") and "as of" in ln:
                as_of = date.fromisoformat(ln.split("as of")[1].split()[0])
            elif ln and not ln.startswith("#") and ln != "ticker":
                tickers.append(ln.strip())
        if as_of is None or not tickers:
            raise ValueError(f"snapshot malformed: {p}")
        out[as_of] = tickers
    return out


def ticker_to_code(ticker: str) -> str:
    return ticker.replace(".T", "") + "0"


def trading_days(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if is_tse_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def build_frames(
    bars_by_day: dict[date, list[dict]], union_codes: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    """日別バー → (adj_close, volume, full_cache)。"""
    rows = []
    for day_rows in bars_by_day.values():
        rows.extend(r for r in day_rows if r.get("Code") in union_codes)
    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])
    df["ticker"] = df["Code"].str[:-1] + ".T"

    keep = [c for c in ("AdjO", "AdjH", "AdjL", "AdjC", "AdjVo", "Va",
                        "O", "H", "L", "C", "Vo") if c in df.columns]
    full_cache: dict[str, pd.DataFrame] = {
        t: g.set_index("Date").sort_index()[keep]
        for t, g in df.groupby("ticker")
    }
    adj_close = df.pivot_table(index="Date", columns="ticker", values="AdjC").sort_index()
    volume = df.pivot_table(index="Date", columns="ticker", values="AdjVo").sort_index()
    adj_close.columns.name = volume.columns.name = None
    return adj_close, volume, full_cache


def fundamentals_asof(
    fins: dict[str, list[dict]],
    tickers: list[str],
    as_of: date,
    adj_close: pd.DataFrame,
    full_cache: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """四半期 as-of 時点のファンダ frame（screener 互換スキーマ）。

    - 開示: DiscDate <= as_of の最新開示（ポイントインタイム）
    - 価格・売買代金: as_of 直前5営業日（未来参照なし）
    """
    cutoff = as_of.isoformat()
    records = []
    for t in tickers:
        stmt = {}
        for d in fins.get(t, []):
            dd = d.get("DiscDate") or ""
            if dd and dd <= cutoff and dd > (stmt.get("DiscDate") or ""):
                stmt = d
        equity_ratio = float(stmt.get("EqAR") or 0)
        bvps = float(stmt.get("BPS") or 0)
        equity = float(stmt.get("Eq") or 0)
        shares = float(stmt.get("ShOutFY") or 0)
        if bvps == 0.0 and equity > 0 and shares > 0:
            bvps = equity / shares

        last_close = 0.0
        avg_5d_tv = 0.0
        if t in adj_close.columns:
            closes = adj_close[t].loc[:cutoff].dropna()
            if not closes.empty:
                last_close = float(closes.iloc[-1])
        cached = full_cache.get(t)
        if cached is not None and "Va" in cached.columns:
            va = pd.to_numeric(cached["Va"], errors="coerce").loc[:cutoff].dropna()
            if not va.empty:
                avg_5d_tv = float(va.tail(5).mean())

        pbr = last_close / bvps if bvps > 0 and last_close > 0 else 0.0
        if shares > 0 and last_close > 0:
            market_cap = shares * last_close
        elif pbr > 0 and equity > 0:
            market_cap = pbr * equity
        else:
            market_cap = 0.0
        records.append({
            "ticker": t,
            "market_cap_jpy": market_cap,
            "pbr": pbr,
            "equity_ratio": equity_ratio,
            "avg_5d_trading_value_jpy": avg_5d_tv,
        })
    return pd.DataFrame(records).set_index("ticker")


def main() -> int:
    parser = argparse.ArgumentParser(description="PIT データキャッシュ構築（F1）")
    parser.add_argument("--start", default="2021-07-01", help="OHLCV 取得開始日")
    parser.add_argument("--end", default=None, help="OHLCV 取得終了日 (default: 直近営業日)")
    args = parser.parse_args()

    import os

    from squant.infrastructure.jquants_client import JQuantsClient
    rpm = int(os.environ.get("JQUANTS_RPM", "50"))
    client = JQuantsClient(api_key=os.environ.get("JQUANTS_API_KEY", ""),
                           requests_per_minute=rpm)
    pace = 60.0 / rpm

    start = date.fromisoformat(args.start)
    if args.end:
        end = date.fromisoformat(args.end)
    else:
        end = date.today()
        while not is_tse_trading_day(end):
            end -= timedelta(days=1)

    snapshots = load_snapshots()
    union = sorted(set().union(*snapshots.values()))
    union_codes = {ticker_to_code(t) for t in union}
    days = trading_days(start, end)
    print(f"union {len(union)} tickers / {len(days)} trading days "
          f"({start} → {end}) / rpm={rpm}", flush=True)

    # ── Phase 1: 日別バー（再開可能） ────────────────────────────────────
    _CACHE_DIR.mkdir(exist_ok=True)
    bars_by_day: dict[date, list[dict]] = (
        pickle.load(_BARS_RAW.open("rb")) if _BARS_RAW.exists() else {}
    )
    todo = [d for d in days if d not in bars_by_day]
    print(f"Phase 1 (bars by date): {len(todo)} days to fetch "
          f"({len(bars_by_day)} cached)", flush=True)
    t0 = time.monotonic()
    for i, d in enumerate(todo):
        rows = client.fetch_bars_for_date(d)
        # 祝日でない平日でもまれに空（半日場等はデータあり）— 空は空で記録して再取得しない
        bars_by_day[d] = rows
        if (i + 1) % 50 == 0 or (i + 1) == len(todo):
            pickle.dump(bars_by_day, _BARS_RAW.open("wb"))
            el = time.monotonic() - t0
            eta = el / (i + 1) * (len(todo) - i - 1)
            print(f"  bars {i + 1}/{len(todo)} ({el / 60:.1f}m elapsed, "
                  f"ETA {eta / 60:.1f}m)", flush=True)
        time.sleep(pace)
    if todo:
        pickle.dump(bars_by_day, _BARS_RAW.open("wb"))

    # ── Phase 2: fins/summary（再開可能） ───────────────────────────────
    fins: dict[str, list[dict]] = (
        pickle.load(_FINS_RAW.open("rb")) if _FINS_RAW.exists() else {}
    )
    todo_t = [t for t in union if t not in fins]
    print(f"Phase 2 (fins/summary): {len(todo_t)} tickers to fetch "
          f"({len(fins)} cached)", flush=True)
    t0 = time.monotonic()
    limiter = client._make_limiter(None)
    for i, t in enumerate(todo_t):
        rows = client._get_paginated(
            "/fins/summary", {"code": ticker_to_code(t)}, limiter, f"fins({t})",
        ) or []
        fins[t] = [{k: r.get(k) for k in _FINS_FIELDS} for r in rows]
        if (i + 1) % 200 == 0 or (i + 1) == len(todo_t):
            pickle.dump(fins, _FINS_RAW.open("wb"))
            el = time.monotonic() - t0
            eta = el / (i + 1) * (len(todo_t) - i - 1)
            print(f"  fins {i + 1}/{len(todo_t)} ({el / 60:.1f}m elapsed, "
                  f"ETA {eta / 60:.1f}m)", flush=True)
        time.sleep(pace)
    if todo_t:
        pickle.dump(fins, _FINS_RAW.open("wb"))

    # ── 組み立て ─────────────────────────────────────────────────────────
    print("Building frames...", flush=True)
    adj_close, volume, full_cache = build_frames(bars_by_day, union_codes)
    print(f"  adj_close: {adj_close.shape}", flush=True)

    fundamentals_by_quarter = {
        q: fundamentals_asof(fins, tickers, q, adj_close, full_cache)
        for q, tickers in sorted(snapshots.items())
    }

    out_path = _CACHE_DIR / f"pit_data_{start}_{end}.pkl"
    pickle.dump({
        "adj_close": adj_close,
        "volume": volume,
        "full_cache": full_cache,
        "universe_by_quarter": {q: list(t) for q, t in snapshots.items()},
        "fundamentals_by_quarter": fundamentals_by_quarter,
    }, out_path.open("wb"), protocol=4)
    print(f"saved: {out_path} ({out_path.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
