"""PEAD 用 決算データキャッシュ構築（Plan B ㋐）.

既存の PIT パイプライン（pit_data_*.pkl / pit_fins_raw.pkl）は *一切触らず*、決算イベント
（売上・利益・EPS・会社予想・開示日時）を別ファイルに取得・組成する。

- Phase A（再開可能）: `/fins/summary` を union 銘柄ぶん取得し、earnings フィールドを保持
  → `.backtest_cache/pit_earnings_raw.pkl`。
- Phase B（組成）: 銘柄ごとに開示イベント列を作り、前年同四半期比（YoY）の純利益・売上
  サプライズを算出 → `.backtest_cache/pit_earnings_events.pkl`
  （{ticker: [{disc_date, cur_per_type, cur_per_en, np, sales, eps, yoy_np, yoy_sales}, ...]}）。

YoY サプライズ: アナリスト予想が J-Quants に無いため、業界標準の「前年同四半期比」を採用。
CurPerType（1Q/2Q/3Q/FY 等）でグルーピングし、CurPerEn 昇順で1つ前（≒1年前の同種期間）と比較。
J-Quants の四半期利益は累計値のため、同種期間同士の比較で整合する。

使い方:
    python scripts/build_pead_cache.py            # Phase A（フェッチ）→ Phase B（組成）
    python scripts/build_pead_cache.py --assemble-only  # 既存 raw から組成のみ
"""

import argparse
import os
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from build_pit_cache import _CACHE_DIR, load_snapshots, ticker_to_code

_EARN_RAW = _CACHE_DIR / "pit_earnings_raw.pkl"
_EARN_EVENTS = _CACHE_DIR / "pit_earnings_events.pkl"

# fins/summary から PEAD 用に保持するフィールド
_EARN_FIELDS = (
    "DiscDate", "DiscTime", "DiscNo", "DocType", "CurPerType", "CurPerSt", "CurPerEn",
    "Sales", "OP", "OdP", "NP", "EPS",
    "FSales", "FOP", "FOdP", "FNP", "FEPS",
    "NCSales", "NCOP", "NCOdP", "NCNP", "NCEPS",  # 非連結フォールバック用
)


def _f(v) -> float | None:
    """空文字・None を弾いて float に。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_earnings(union: list[str]) -> dict[str, list[dict]]:
    from squant.infrastructure.jquants_client import JQuantsClient

    rpm = int(os.environ.get("JQUANTS_RPM", "50"))
    client = JQuantsClient(api_key=os.environ.get("JQUANTS_API_KEY", ""),
                           requests_per_minute=rpm)
    limiter = client._make_limiter(None)

    raw: dict[str, list[dict]] = (
        pickle.load(_EARN_RAW.open("rb")) if _EARN_RAW.exists() else {}
    )
    todo = [t for t in union if t not in raw]
    print(f"Phase A (earnings fins): {len(todo)} to fetch ({len(raw)} cached)", flush=True)
    t0 = time.monotonic()
    for i, t in enumerate(todo):
        rows = client._get_paginated(
            "/fins/summary", {"code": ticker_to_code(t)}, limiter, f"earn({t})",
        ) or []
        raw[t] = [{k: r.get(k) for k in _EARN_FIELDS} for r in rows]
        if (i + 1) % 200 == 0 or (i + 1) == len(todo):
            with _EARN_RAW.open("wb") as fh:
                pickle.dump(raw, fh)
            el = time.monotonic() - t0
            eta = el / (i + 1) * (len(todo) - i - 1)
            print(f"  earn {i + 1}/{len(todo)} ({el / 60:.1f}m elapsed, "
                  f"ETA {eta / 60:.1f}m)", flush=True)
    if todo:
        with _EARN_RAW.open("wb") as fh:
            pickle.dump(raw, fh)
    return raw


def assemble_events(raw: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """銘柄ごとに開示イベント列を作り、YoY サプライズを付与。"""
    events_by_ticker: dict[str, list[dict]] = {}
    n_ev = 0
    for ticker, rows in raw.items():
        evs = []
        for r in rows:
            dd = r.get("DiscDate") or ""
            if not dd:
                continue
            # 連結優先、空なら非連結フォールバック
            np_ = _f(r.get("NP"))
            if np_ is None:
                np_ = _f(r.get("NCNP"))
            sales = _f(r.get("Sales")) or _f(r.get("NCSales"))
            eps = _f(r.get("EPS"))
            if eps is None:
                eps = _f(r.get("NCEPS"))
            evs.append({
                "disc_date": dd,
                "cur_per_type": r.get("CurPerType") or "",
                "cur_per_en": r.get("CurPerEn") or "",
                "np": np_,
                "sales": sales,
                "eps": eps,
                "yoy_np": None,
                "yoy_sales": None,
            })
        # YoY: CurPerType でグループ化し CurPerEn 昇順で前期(≒1年前同種)と比較
        by_type: dict[str, list[dict]] = {}
        for e in evs:
            by_type.setdefault(e["cur_per_type"], []).append(e)
        for _t, group in by_type.items():
            group.sort(key=lambda e: e["cur_per_en"])
            for i in range(1, len(group)):
                prev, cur = group[i - 1], group[i]
                if cur["np"] is not None and prev["np"] not in (None, 0):
                    cur["yoy_np"] = (cur["np"] - prev["np"]) / abs(prev["np"])
                if cur["sales"] is not None and prev["sales"] not in (None, 0):
                    cur["yoy_sales"] = (cur["sales"] - prev["sales"]) / abs(prev["sales"])
        evs.sort(key=lambda e: e["disc_date"])
        events_by_ticker[ticker] = evs
        n_ev += len(evs)
    print(f"Phase B (assemble): {len(events_by_ticker)} tickers, {n_ev} events total")
    return events_by_ticker


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assemble-only", action="store_true")
    args = ap.parse_args()

    snapshots = load_snapshots()
    union = sorted(set().union(*snapshots.values()))
    print(f"union {len(union)} tickers", flush=True)

    if args.assemble_only:
        with _EARN_RAW.open("rb") as fh:
            raw = pickle.load(fh)
    else:
        raw = fetch_earnings(union)

    events = assemble_events(raw)
    with _EARN_EVENTS.open("wb") as fh:
        pickle.dump(events, fh)
    print(f"saved: {_EARN_EVENTS}  ({_EARN_EVENTS.stat().st_size / 1e6:.1f} MB)")
    # サニティ: サプライズが計算できたイベント数
    n_yoy = sum(1 for evs in events.values() for e in evs if e["yoy_np"] is not None)
    print(f"events with yoy_np: {n_yoy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
