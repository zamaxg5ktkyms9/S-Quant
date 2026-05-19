"""
S-Quant バックテスト

既存の screener / signal_engine / evaluate_exit をそのまま再利用して
過去データ上でシミュレーションを行う。

制限事項:
- ファンダメンタルズ（自己資本比率・発行済株数）は現時点の最新値を使用
- PBR のみバックテスト各日の終値で再計算（BPS = 最新終値 / 最新PBR で導出）
- 売買は「シグナル日の終値で即成立」と仮定（翌日始値ではない）
- S株スプレッドは take-profit 計算にのみ反映（entry/exit コストは含めない）
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, "src")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd

from squant.application.universe_loader import load_universe
from squant.config.settings import Settings
from squant.domain import ranking, screener, signal_engine
from squant.domain.models import Position
from squant.domain.position_manager import evaluate_exit
from squant.domain.quantity_calculator import compute_quantity, compute_stop_loss_price
from squant.infrastructure.jquants_client import JQuantsClient
from squant.utils.jst import add_trading_days, is_tse_trading_day


@dataclass
class TradeRecord:
    ticker: str
    entry_date: date
    exit_date: date
    entry_price: Decimal
    exit_price: Decimal
    shares: int
    pnl: Decimal
    reason: str


@dataclass
class BacktestState:
    position: Position | None = None
    cash: Decimal = Decimal("100000")
    trades: list[TradeRecord] = field(default_factory=list)
    signals_found: int = 0


def _build_bps_map(fund_base: pd.DataFrame, adj_close_full: pd.DataFrame) -> dict[str, float]:
    """最新終値とPBRからBPSを逆算。各日のPBR再計算に使う。"""
    bps_map: dict[str, float] = {}
    for ticker in fund_base.index:
        pbr = float(fund_base.at[ticker, "pbr"])
        if pbr <= 0 or ticker not in adj_close_full.columns:
            continue
        latest_close = adj_close_full[ticker].dropna()
        if latest_close.empty:
            continue
        bps_map[ticker] = float(latest_close.iloc[-1]) / pbr
    return bps_map


def _update_pbr(fund_base: pd.DataFrame, bps_map: dict[str, float], adj_slice: pd.DataFrame) -> pd.DataFrame:
    """adj_slice の最終値でPBRを上書きしたファンダメンタルズを返す。"""
    fund = fund_base.copy()
    for ticker, bps in bps_map.items():
        if ticker not in adj_slice.columns or bps <= 0:
            continue
        today_close_series = adj_slice[ticker].dropna()
        if today_close_series.empty:
            continue
        fund.at[ticker, "pbr"] = float(today_close_series.iloc[-1]) / bps
    return fund


def _process_exit(
    state: BacktestState,
    today: date,
    full_cache: dict[str, pd.DataFrame],
) -> bool:
    """保有ポジションの出口判定。退出したら True を返す。"""
    pos = state.position
    assert pos is not None

    cache_df = full_cache.get(pos.ticker)
    if cache_df is None or cache_df.empty:
        return False

    close_col = "AdjC" if "AdjC" in cache_df.columns else "C"
    high_col  = "AdjH" if "AdjH" in cache_df.columns else "H"
    low_col   = "AdjL" if "AdjL" in cache_df.columns else "L"

    day_data = cache_df.loc[:str(today)]
    if day_data.empty:
        return False

    latest_close = Decimal(str(round(float(day_data[close_col].iloc[-1]), 1)))

    exit_dec = evaluate_exit(
        position=pos,
        today=today,
        latest_close=latest_close,
        high_series=day_data[high_col],
        low_series=day_data[low_col],
        close_series=day_data[close_col],
    )

    if exit_dec.should_exit:
        pnl = (latest_close - pos.entry_price) * pos.shares
        state.trades.append(TradeRecord(
            ticker=pos.ticker,
            entry_date=pos.entry_date,
            exit_date=today,
            entry_price=pos.entry_price,
            exit_price=latest_close,
            shares=pos.shares,
            pnl=pnl,
            reason=exit_dec.reason.value,
        ))
        state.cash += latest_close * pos.shares
        state.position = None
        sign = "+" if pnl >= 0 else ""
        print(f"  EXIT  {pos.ticker} @ ¥{latest_close} → {exit_dec.reason.value}  "
              f"P&L: ¥{sign}{int(pnl):,}", flush=True)
        return True

    # ポジション継続: トレーリングストップを更新
    if exit_dec.updated_trailing_stop:
        state.position = Position(
            ticker=pos.ticker,
            shares=pos.shares,
            entry_price=pos.entry_price,
            intended_entry_price=pos.intended_entry_price,
            entry_date=pos.entry_date,
            stop_loss_price=pos.stop_loss_price,
            trailing_stop_price=exit_dec.updated_trailing_stop,
            highest_price_since_entry=max(pos.highest_price_since_entry, latest_close),
            time_stop_date=pos.time_stop_date,
        )
    return False


def _process_entry(
    state: BacktestState,
    today: date,
    adj_close_full: pd.DataFrame,
    volume_full: pd.DataFrame,
    fund_base: pd.DataFrame,
    bps_map: dict[str, float],
    universe: list[str],
    settings: Settings,
    verbose: bool = False,
) -> None:
    """シグナルを探してポジションを建てる。"""
    adj_slice = adj_close_full.loc[:str(today)]
    vol_slice  = volume_full.loc[:str(today)]

    if len(adj_slice) < 30:
        if verbose:
            print(f"  [{today}] SKIP: 履歴不足 ({len(adj_slice)}行)")
        return  # 履歴不足

    fund = _update_pbr(fund_base, bps_map, adj_slice)

    filtered = screener.apply_fundamental_filters(
        universe, adj_slice, fund, today, set()
    )
    if verbose:
        counts = filtered.attrs.get("filter_counts", {})
        print(f"  [{today}] screener: {len(filtered)}件通過  除外={counts}")
    if filtered.empty:
        return

    ohlcv_sig = adj_slice.copy()
    for col in vol_slice.columns:
        ohlcv_sig[f"{col}_vol"] = vol_slice[col]

    candidates = signal_engine.detect_signals(
        filtered["ticker"].tolist(), ohlcv_sig, fund, today
    )
    if verbose:
        print(f"  [{today}] シグナル候補: {len(candidates)}件")
    if not candidates:
        return

    state.signals_found += 1
    best = ranking.rank(candidates, top_n=1)[0]

    shares = compute_quantity(
        state.cash, best.close, settings.gap_up_threshold, settings.budget_jpy
    )
    stop = compute_stop_loss_price(best.close, settings.stop_loss_rate)
    time_stop_date = add_trading_days(today, 5)

    state.position = Position(
        ticker=best.ticker,
        shares=shares,
        entry_price=best.close,
        intended_entry_price=best.close,
        entry_date=today,
        stop_loss_price=stop,
        trailing_stop_price=stop,
        highest_price_since_entry=best.close,
        time_stop_date=time_stop_date,
    )
    state.cash -= best.close * shares
    print(f"  ENTRY {best.ticker} @ ¥{best.close} ×{shares}  "
          f"RSI={best.rsi14:.1f}  stop=¥{int(stop)}  ({today})", flush=True)


def _print_report(state: BacktestState, start: date, end: date, adj_close_full: pd.DataFrame) -> None:
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"バックテスト結果: {start} 〜 {end}")
    print(sep)
    print(f"シグナル検出日数 : {state.signals_found} 日")
    print(f"取引件数         : {len(state.trades)} 件")

    if state.position is not None:
        p = state.position
        latest = adj_close_full[p.ticker].dropna().iloc[-1] if p.ticker in adj_close_full.columns else p.entry_price
        unrealized = (Decimal(str(round(float(latest), 1))) - p.entry_price) * p.shares
        sign = "+" if unrealized >= 0 else ""
        print(f"未決済ポジション : {p.ticker}  エントリー {p.entry_date}  "
              f"未実現損益 ¥{sign}{int(unrealized):,}")

    if not state.trades:
        print("\n→ バックテスト期間中に取引なし")
        print(f"\n最終キャッシュ: ¥{int(state.cash):,}")
        return

    pnl_list = [float(t.pnl) for t in state.trades]
    wins  = [p for p in pnl_list if p > 0]
    total = sum(pnl_list)

    print(f"\n勝率      : {len(wins)}/{len(state.trades)} = {len(wins)/len(state.trades)*100:.1f}%")
    print(f"平均損益  : ¥{total/len(pnl_list):+,.0f}")
    print(f"累積損益  : ¥{total:+,.0f}")
    print(f"最大利益  : ¥{max(pnl_list):+,.0f}")
    print(f"最大損失  : ¥{min(pnl_list):+,.0f}")

    # 最大ドローダウン（累積損益ベース）
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_list:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    print(f"最大DD    : ¥{max_dd:+,.0f}")

    # 出口理由別集計
    by_reason: dict[str, list[float]] = {}
    for t in state.trades:
        by_reason.setdefault(t.reason, []).append(float(t.pnl))
    print("\n出口理由別:")
    for reason, pnls in sorted(by_reason.items()):
        w = sum(1 for p in pnls if p > 0)
        print(f"  {reason:20s}: {len(pnls)}件  勝率{w/len(pnls)*100:.0f}%  avg ¥{sum(pnls)/len(pnls):+,.0f}")

    print("\n個別取引:")
    for t in state.trades:
        days = (t.exit_date - t.entry_date).days
        sign = "+" if t.pnl >= 0 else ""
        print(f"  {t.entry_date} {t.ticker:8s} "
              f"¥{t.entry_price}→¥{t.exit_price} ×{t.shares}  "
              f"P&L:¥{sign}{int(t.pnl):,}  ({t.reason}, {days}日)")

    print(f"\n最終キャッシュ: ¥{int(state.cash):,}  (初期 ¥100,000)")


def main() -> None:
    parser = argparse.ArgumentParser(description="S-Quant バックテスト")
    parser.add_argument("--start", default="2024-01-04", help="開始日 YYYY-MM-DD")
    parser.add_argument("--end",   default="2024-12-31", help="終了日 YYYY-MM-DD")
    parser.add_argument("--rpm",     type=int, default=30,   help="J-Quants RPM (default: 30)")
    parser.add_argument("--verbose", action="store_true",   help="毎日のフィルタ結果を表示")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    api_key = os.environ.get("JQUANTS_API_KEY", "")
    client   = JQuantsClient(api_key=api_key, requests_per_minute=args.rpm)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    universe = load_universe()

    # シグナル検出に必要な履歴 + バックテスト期間を一括取得
    fetch_start = start - timedelta(days=180)

    print(f"Fetching OHLCV ({fetch_start} → {end})...", flush=True)
    adj_close_full, volume_full = client.fetch_ohlcv(universe, fetch_start, end)
    full_cache = dict(client._ohlcv_cache)
    print(f"  → {len(adj_close_full.columns)} tickers", flush=True)

    print("Fetching fundamentals...", flush=True)
    fund_base = client.fetch_fundamentals(universe)
    print(f"  → {len(fund_base)} rows", flush=True)

    bps_map = _build_bps_map(fund_base, adj_close_full)

    # バックテスト対象の営業日リスト
    trading_days = sorted([
        d.date()
        for d in pd.bdate_range(start, end)
        if is_tse_trading_day(d.date())
    ])
    print(f"\nバックテスト期間: {start} 〜 {end}  ({len(trading_days)} 営業日)\n", flush=True)

    state = BacktestState()

    for today in trading_days:
        if state.position is not None:
            _process_exit(state, today, full_cache)
        else:
            _process_entry(
                state, today,
                adj_close_full, volume_full,
                fund_base, bps_map,
                universe, settings,
                verbose=args.verbose,
            )

    _print_report(state, start, end, adj_close_full)


if __name__ == "__main__":
    main()
