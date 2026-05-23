"""
S-Quant バックテスト（改訂版・Phase 1 / 単元株 / ザラ場約定モード）

実装方針:
- エントリーは「シグナル日翌営業日の始値（=Open）で約定」と仮定
  （旧版の「シグナル日の終値で即成立」より現実的）
- ギャップアップ判定: 始値 > 前日終値 × 1.02 → エントリー見送り
- 保有中の出口判定はザラ場モード（high/low使用、OCO逆指値・指値の発動を再現）
- 株数は単元株（100株単位）で算出
- スプレッド・手数料は0（SBI証券ゼロ革命適用）

ファンダメンタルズ:
- 自己資本比率・発行済株数は最新値のみ。バックテスト各日でPBRのみ再計算（BPS逆算）。
"""

import argparse
import os
import pickle
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "src")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd

from squant.application.universe_loader import load_universe
from squant.config.constants import GAP_UP_CANCEL_THRESHOLD, SHARES_PER_UNIT
from squant.config.settings import Settings
from squant.domain import ranking, screener, signal_engine
from squant.domain.models import Position
from squant.domain.position_manager import evaluate_exit
from squant.domain.quantity_calculator import compute_quantity, compute_stop_loss_price
from squant.infrastructure.jquants_client import FetchTimeoutError, JQuantsClient
from squant.utils.jst import add_trading_days, is_tse_trading_day


@dataclass
class TradeRecord:
    ticker: str
    signal_date: date
    entry_date: date
    exit_date: date
    entry_price: Decimal
    exit_price: Decimal
    shares: int
    pnl: Decimal
    pnl_pct: float
    holding_days: int
    reason: str


@dataclass
class PendingEntry:
    """シグナル日に検出して、翌営業日の始値で約定判定する。"""
    ticker: str
    signal_date: date
    reference_close: Decimal       # シグナル日の終値（ギャップアップ判定の基準）
    rsi14: float


@dataclass
class BacktestState:
    position: Position | None = None
    cash: Decimal = Decimal("100000")
    initial_capital: Decimal = Decimal("100000")
    trades: list[TradeRecord] = field(default_factory=list)
    signals_found: int = 0
    gap_up_skipped: int = 0
    insufficient_capital_skipped: int = 0
    pending_entry: PendingEntry | None = None  # 翌営業日に約定判定


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


def _update_pbr(
    fund_base: pd.DataFrame,
    bps_map: dict[str, float],
    adj_slice: pd.DataFrame,
) -> pd.DataFrame:
    fund = fund_base.copy()
    for ticker, bps in bps_map.items():
        if ticker not in adj_slice.columns or bps <= 0:
            continue
        s = adj_slice[ticker].dropna()
        if s.empty:
            continue
        fund.at[ticker, "pbr"] = float(s.iloc[-1]) / bps
    return fund


def _get_ohlc_for_date(
    cache_df: pd.DataFrame,
    target_date: date,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    """指定日の (open, high, low, close) を返す。営業日が一致しなければNone。"""
    if cache_df is None or cache_df.empty:
        return None

    # 列名の解決（v2はAdjC等のAdj系を優先）
    o_col = "AdjO" if "AdjO" in cache_df.columns else "O"
    h_col = "AdjH" if "AdjH" in cache_df.columns else "H"
    l_col = "AdjL" if "AdjL" in cache_df.columns else "L"
    c_col = "AdjC" if "AdjC" in cache_df.columns else "C"

    try:
        row = cache_df.loc[str(target_date)]
    except KeyError:
        return None

    if isinstance(row, pd.DataFrame):  # 複数行ヒット
        row = row.iloc[0]

    def _dec(v: object) -> Decimal:
        return Decimal(str(round(float(v), 1)))  # type: ignore[arg-type]

    return _dec(row[o_col]), _dec(row[h_col]), _dec(row[l_col]), _dec(row[c_col])


def _process_pending_entry(
    state: BacktestState,
    today: date,
    full_cache: dict[str, pd.DataFrame],
    settings: Settings,
) -> None:
    """翌営業日の始値でエントリー判定（ギャップアップなら見送り、株数も計算）。"""
    pe = state.pending_entry
    assert pe is not None
    state.pending_entry = None  # consume

    cache_df = full_cache.get(pe.ticker)
    ohlc = _get_ohlc_for_date(cache_df, today) if cache_df is not None else None
    if ohlc is None:
        print(f"  SKIP  {pe.ticker} @ {today}: 当日OHLC欠損", flush=True)
        return

    today_open, today_high, today_low, today_close = ohlc

    # ギャップアップ判定
    cancel_threshold = pe.reference_close * (1 + GAP_UP_CANCEL_THRESHOLD)
    if today_open > cancel_threshold:
        state.gap_up_skipped += 1
        gap_pct = float((today_open / pe.reference_close - 1) * 100)
        print(f"  SKIP  {pe.ticker} @ ¥{today_open}: gap-up +{gap_pct:.1f}% > 2%", flush=True)
        return

    # 株数計算（単元株100株単位）
    try:
        shares = compute_quantity(
            state.cash, pe.reference_close, settings.gap_up_threshold, settings.budget_jpy
        )
    except Exception as e:
        state.insufficient_capital_skipped += 1
        print(f"  SKIP  {pe.ticker} @ ¥{today_open}: {e}", flush=True)
        return

    # ザラ場ですぐ約定すると仮定 → エントリー価格は始値
    entry_price = today_open
    stop = compute_stop_loss_price(entry_price, settings.stop_loss_rate)
    time_stop_date = add_trading_days(today, 5)

    state.position = Position(
        ticker=pe.ticker,
        shares=shares,
        entry_price=entry_price,
        intended_entry_price=pe.reference_close,
        entry_date=today,
        stop_loss_price=stop,
        trailing_stop_price=stop,
        highest_price_since_entry=entry_price,
        time_stop_date=time_stop_date,
    )
    state.cash -= entry_price * shares
    print(
        f"  ENTRY {pe.ticker} @ ¥{entry_price} ×{shares}  "
        f"(sig {pe.signal_date}, RSI={pe.rsi14:.1f}, stop=¥{stop})",
        flush=True,
    )


def _process_exit(
    state: BacktestState,
    today: date,
    full_cache: dict[str, pd.DataFrame],
) -> bool:
    """保有ポジションのザラ場出口判定。退出したら True を返す。"""
    pos = state.position
    assert pos is not None

    cache_df = full_cache.get(pos.ticker)
    if cache_df is None or cache_df.empty:
        return False

    ohlc = _get_ohlc_for_date(cache_df, today)
    if ohlc is None:
        return False

    _today_open, today_high, today_low, today_close = ohlc

    # ザラ場OHLC履歴（指標計算用）
    close_col = "AdjC" if "AdjC" in cache_df.columns else "C"
    high_col  = "AdjH" if "AdjH" in cache_df.columns else "H"
    low_col   = "AdjL" if "AdjL" in cache_df.columns else "L"
    day_data  = cache_df.loc[:str(today)]
    if day_data.empty:
        return False

    exit_dec = evaluate_exit(
        position=pos,
        today=today,
        latest_close=today_close,
        high_series=day_data[high_col],
        low_series=day_data[low_col],
        close_series=day_data[close_col],
        intraday_high=today_high,
        intraday_low=today_low,
    )

    if exit_dec.should_exit:
        # ザラ場約定 or 終値約定（タイムストップ等）
        exit_price = exit_dec.exit_price if exit_dec.exit_price is not None else today_close
        pnl = (exit_price - pos.entry_price) * pos.shares
        pnl_pct = float((exit_price / pos.entry_price - 1) * 100)
        holding_days = (today - pos.entry_date).days
        state.trades.append(TradeRecord(
            ticker=pos.ticker,
            signal_date=pos.entry_date,  # 簡略化: 実際はentryの前日
            entry_date=pos.entry_date,
            exit_date=today,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=holding_days,
            reason=exit_dec.reason.value if exit_dec.reason else "unknown",
        ))
        state.cash += exit_price * pos.shares
        state.position = None
        sign = "+" if pnl >= 0 else ""
        print(
            f"  EXIT  {pos.ticker} @ ¥{exit_price} → {exit_dec.reason.value if exit_dec.reason else '?'}  "
            f"P&L: ¥{sign}{int(pnl):,} ({sign}{pnl_pct:.1f}%)  hold={holding_days}d",
            flush=True,
        )
        return True

    # ポジション継続: トレーリングストップ＋直近高値を更新
    new_highest = max(pos.highest_price_since_entry, today_high)
    if exit_dec.updated_trailing_stop is not None or new_highest > pos.highest_price_since_entry:
        new_trailing = exit_dec.updated_trailing_stop or pos.trailing_stop_price
        state.position = Position(
            ticker=pos.ticker,
            shares=pos.shares,
            entry_price=pos.entry_price,
            intended_entry_price=pos.intended_entry_price,
            entry_date=pos.entry_date,
            stop_loss_price=pos.stop_loss_price,
            trailing_stop_price=new_trailing,
            highest_price_since_entry=new_highest,
            time_stop_date=pos.time_stop_date,
        )
    return False


def _process_signal_scan(
    state: BacktestState,
    today: date,
    adj_close_full: pd.DataFrame,
    volume_full: pd.DataFrame,
    fund_base: pd.DataFrame,
    bps_map: dict[str, float],
    universe: list[str],
    verbose: bool = False,
) -> None:
    """終値後にシグナルを検出し、翌営業日のエントリー候補としてキューイング。"""
    adj_slice = adj_close_full.loc[:str(today)]
    vol_slice = volume_full.loc[:str(today)]

    if len(adj_slice) < 30:
        return

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

    state.pending_entry = PendingEntry(
        ticker=best.ticker,
        signal_date=today,
        reference_close=best.close,
        rsi14=best.rsi14,
    )
    print(f"  SIGNAL {best.ticker} @ ¥{best.close} RSI={best.rsi14:.1f}  (entry next day)", flush=True)


def _print_report(
    state: BacktestState,
    start: date,
    end: date,
    adj_close_full: pd.DataFrame,
) -> None:
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"バックテスト結果: {start} 〜 {end}")
    print(sep)

    months = max(1, (end - start).days / 30.4)

    print(f"シグナル検出回数 : {state.signals_found}")
    print(f"ギャップアップ見送り: {state.gap_up_skipped}")
    print(f"資金不足見送り    : {state.insufficient_capital_skipped}")
    print(f"取引件数         : {len(state.trades)} 件  ({len(state.trades)/months:.1f} 件/月)")

    if state.position is not None:
        p = state.position
        latest = (
            adj_close_full[p.ticker].dropna().iloc[-1]
            if p.ticker in adj_close_full.columns else p.entry_price
        )
        unrealized = (Decimal(str(round(float(latest), 1))) - p.entry_price) * p.shares
        sign = "+" if unrealized >= 0 else ""
        print(f"未決済ポジション : {p.ticker} entry {p.entry_date}  "
              f"未実現損益 ¥{sign}{int(unrealized):,}")

    if not state.trades:
        print("\n→ バックテスト期間中に取引なし")
        print(f"\n最終キャッシュ: ¥{int(state.cash):,}")
        return

    pnl_list = [float(t.pnl) for t in state.trades]
    pct_list = [t.pnl_pct for t in state.trades]
    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]
    total = sum(pnl_list)

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    win_rate = len(wins) / len(state.trades) * 100
    expectancy = total / len(pnl_list)
    monthly_avg = total / months

    print(f"\n--- リターン統計 ---")
    print(f"勝率           : {len(wins)}/{len(state.trades)} = {win_rate:.1f}%")
    print(f"平均損益       : ¥{expectancy:+,.0f}  ({sum(pct_list)/len(pct_list):+.2f}%/trade)")
    print(f"累積損益       : ¥{total:+,.0f}  ({total/float(state.initial_capital)*100:+.1f}%)")
    print(f"月平均損益     : ¥{monthly_avg:+,.0f}  ({monthly_avg/float(state.initial_capital)*100:+.2f}%/月)")
    print(f"平均利益       : ¥{avg_win:+,.0f}  ({len(wins)}件)")
    print(f"平均損失       : ¥{avg_loss:+,.0f}  ({len(losses)}件)")
    if avg_loss != 0:
        print(f"PF (Profit Factor): {abs(sum(wins)/sum(losses)):.2f}" if sum(losses) != 0 else "PF: N/A")
    print(f"最大利益       : ¥{max(pnl_list):+,.0f}")
    print(f"最大損失       : ¥{min(pnl_list):+,.0f}")

    # 最大ドローダウン
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_list:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    print(f"最大DD         : ¥{max_dd:+,.0f}  ({max_dd/float(state.initial_capital)*100:+.1f}%)")

    # 出口理由別
    print("\n--- 出口理由別 ---")
    by_reason: dict[str, list[float]] = {}
    for t in state.trades:
        by_reason.setdefault(t.reason, []).append(float(t.pnl))
    for reason, pnls in sorted(by_reason.items()):
        w = sum(1 for p in pnls if p > 0)
        print(f"  {reason:20s}: {len(pnls):3d}件  勝率{w/len(pnls)*100:5.1f}%  avg ¥{sum(pnls)/len(pnls):+,.0f}")

    # 保有日数別
    print("\n--- 保有日数分布 ---")
    days_dist: dict[int, list[float]] = {}
    for t in state.trades:
        days_dist.setdefault(t.holding_days, []).append(float(t.pnl))
    for d in sorted(days_dist.keys()):
        pnls = days_dist[d]
        w = sum(1 for p in pnls if p > 0)
        print(f"  {d}日: {len(pnls):3d}件  勝率{w/len(pnls)*100:5.1f}%  avg ¥{sum(pnls)/len(pnls):+,.0f}")

    print("\n--- 直近10取引 ---")
    for t in state.trades[-10:]:
        sign = "+" if t.pnl >= 0 else ""
        print(
            f"  {t.entry_date}→{t.exit_date} {t.ticker:8s} "
            f"¥{t.entry_price}→¥{t.exit_price} ×{t.shares}  "
            f"P&L:¥{sign}{int(t.pnl):,} ({sign}{t.pnl_pct:.1f}%)  "
            f"{t.reason} {t.holding_days}d"
        )

    print(f"\n最終キャッシュ : ¥{int(state.cash):,}  (初期 ¥{int(state.initial_capital):,})")


def main() -> None:
    parser = argparse.ArgumentParser(description="S-Quant バックテスト（改訂版・単元株・ザラ場モード）")
    parser.add_argument("--start", default="2024-01-04", help="開始日 YYYY-MM-DD")
    parser.add_argument("--end",   default="2025-12-30", help="終了日 YYYY-MM-DD")
    parser.add_argument("--budget", type=int, default=100_000, help="初期資本 (default: 100000)")
    parser.add_argument("--rpm", type=int, default=30, help="J-Quants RPM (default: 30)")
    parser.add_argument("--verbose", action="store_true", help="毎日のフィルタ結果を表示")
    parser.add_argument("--cache-dir", default=".backtest_cache", help="データキャッシュ保存先")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    api_key = os.environ.get("JQUANTS_API_KEY", "")
    client   = JQuantsClient(api_key=api_key, requests_per_minute=args.rpm)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings.budget_jpy = Decimal(str(args.budget))
    universe = load_universe()

    fetch_start = start - timedelta(days=180)
    cache_dir = Path(args.cache_dir)
    cache_key  = f"{fetch_start}_{end}"
    cache_file = cache_dir / f"data_{cache_key}.pkl"

    def _progress(label: str):
        def _cb(done: int, total: int) -> None:
            if done % 50 == 0 or done == total:
                print(f"  {label}: {done}/{total} ({done/total*100:.0f}%)", flush=True)
        return _cb

    if cache_file.exists():
        print(f"キャッシュ読み込み中: {cache_file}", flush=True)
        with cache_file.open("rb") as f:
            cached = pickle.load(f)
        adj_close_full = cached["adj_close"]
        volume_full    = cached["volume"]
        full_cache     = cached["full_cache"]
        fund_base      = cached["fundamentals"]
        print(f"  → OHLCV {len(adj_close_full.columns)} tickers, fundamentals {len(fund_base)} rows", flush=True)
    else:
        _api_calls = len(universe) * 2
        _fetch_timeout = _api_calls / args.rpm * 60 * 2
        _fetch_timeout_min = int(_fetch_timeout / 60)

        try:
            print(f"Fetching OHLCV ({fetch_start} → {end})...  (タイムアウト上限: {_fetch_timeout_min}分)", flush=True)
            adj_close_full, volume_full = client.fetch_ohlcv(
                universe, fetch_start, end,
                on_progress=_progress("OHLCV"),
                timeout_seconds=_fetch_timeout,
            )
            full_cache = dict(client._ohlcv_cache)
            print(f"  → {len(adj_close_full.columns)} tickers", flush=True)

            print("Fetching fundamentals...", flush=True)
            fund_base = client.fetch_fundamentals(
                universe,
                on_progress=_progress("fundamentals"),
                timeout_seconds=_fetch_timeout,
            )
            print(f"  → {len(fund_base)} rows", flush=True)

        except FetchTimeoutError as e:
            print(f"\n❌ データ取得が{_fetch_timeout_min}分以内に完了しませんでした。", flush=True)
            print("   原因: J-Quants APIのレートリミット (429) が連鎖しています。", flush=True)
            print("   対処: 時間をおいて再実行するか、--rpm を下げてください。", flush=True)
            print(f"   詳細: {e}", flush=True)
            sys.exit(1)

        cache_dir.mkdir(parents=True, exist_ok=True)
        with cache_file.open("wb") as f:
            pickle.dump({
                "adj_close": adj_close_full,
                "volume":    volume_full,
                "full_cache": full_cache,
                "fundamentals": fund_base,
            }, f)
        print(f"キャッシュ保存: {cache_file}", flush=True)

    bps_map = _build_bps_map(fund_base, adj_close_full)

    trading_days = sorted([
        d.date()
        for d in pd.bdate_range(start, end)
        if is_tse_trading_day(d.date())
    ])
    print(f"\nバックテスト期間: {start} 〜 {end}  ({len(trading_days)} 営業日)", flush=True)
    print(f"初期資本: ¥{args.budget:,}  単元: {SHARES_PER_UNIT}株\n", flush=True)

    state = BacktestState(
        cash=Decimal(str(args.budget)),
        initial_capital=Decimal(str(args.budget)),
    )
    total_days = len(trading_days)
    report_interval = max(1, total_days // 8)

    for i, today in enumerate(trading_days):
        # 1. 保有中ポジションの出口判定
        if state.position is not None:
            _process_exit(state, today, full_cache)

        # 2. 前日シグナルを翌営業日始値で約定判定
        if state.position is None and state.pending_entry is not None:
            _process_pending_entry(state, today, full_cache, settings)

        # 3. 引け後にシグナルスキャン（翌営業日のpending_entry作成）
        if state.position is None and state.pending_entry is None:
            _process_signal_scan(
                state, today,
                adj_close_full, volume_full,
                fund_base, bps_map,
                universe,
                verbose=args.verbose,
            )

        if (i + 1) % report_interval == 0 or (i + 1) == total_days:
            pct = (i + 1) / total_days * 100
            print(
                f"  [{today}] 進捗 {i+1}/{total_days}日 ({pct:.0f}%)  "
                f"取引{len(state.trades)}件  シグナル{state.signals_found}回",
                flush=True,
            )

    _print_report(state, start, end, adj_close_full)


if __name__ == "__main__":
    main()
