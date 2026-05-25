"""
S-Quant バックテスト（B フェーズ・分散版・単元株・ザラ場約定モード）

実装方針:
- エントリーは「シグナル日翌営業日の始値（=Open）で約定」と仮定
- ギャップアップ判定: 始値 > 前日終値 × 1.02 → その銘柄のエントリー見送り
- 保有中の出口判定はザラ場モード（high/low使用、OCO逆指値・指値の発動を再現）
- 株数は単元株（100株単位）で算出
- スプレッド・手数料は0（SBI証券ゼロ革命適用）

B フェーズ拡張 (2026-05-25):
- 最大 max_positions 銘柄を同時保有（Phase 1: 2、Phase 2/3: 3）
- 1銘柄予算は動的: floor(残キャッシュ / 残空きスロット数)
- 同日複数シグナル可（空きスロット数までランキング上位順）
- 保有銘柄・同日 pending は screener 後に除外（重複排除）
- 各銘柄独立の出口判定（OCO・トレーリング・タイムストップ）

ファンダメンタルズ:
- 自己資本比率・発行済株数は最新値のみ。バックテスト各日でPBRのみ再計算（BPS逆算）。
"""

import argparse
import json
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
from squant.config.constants import (
    DEFAULT_MAX_POSITIONS,
    GAP_UP_CANCEL_THRESHOLD,
    SHARES_PER_UNIT,
)
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
    positions: list[Position] = field(default_factory=list)
    cash: Decimal = Decimal("100000")
    initial_capital: Decimal = Decimal("100000")
    max_positions: int = DEFAULT_MAX_POSITIONS
    trades: list[TradeRecord] = field(default_factory=list)
    signals_found: int = 0
    gap_up_skipped: int = 0
    insufficient_capital_skipped: int = 0
    pending_entries: list[PendingEntry] = field(default_factory=list)  # 翌営業日に順次約定判定

    @property
    def open_slots(self) -> int:
        return self.max_positions - len(self.positions)

    @property
    def held_tickers(self) -> set[str]:
        return {p.ticker for p in self.positions}


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
    pe: PendingEntry,
    today: date,
    full_cache: dict[str, pd.DataFrame],
    settings: Settings,
) -> None:
    """1件の pending_entry を翌営業日始値で約定判定。動的予算 = 残キャッシュ / 残空きスロット数。"""
    # 念のため重複エントリーガード
    if pe.ticker in state.held_tickers:
        return

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

    # 動的予算: 残キャッシュ / 残空きスロット数
    open_slots = state.open_slots
    if open_slots <= 0:
        return  # 全スロット埋まっている（早期スキップ）
    slot_budget = (state.cash / open_slots).quantize(Decimal("1"))

    # 株数計算（単元株100株単位）
    try:
        shares = compute_quantity(
            state.cash, pe.reference_close, settings.gap_up_threshold, slot_budget
        )
    except Exception as e:
        state.insufficient_capital_skipped += 1
        print(f"  SKIP  {pe.ticker} @ ¥{today_open}: {e}", flush=True)
        return

    entry_price = today_open
    stop = compute_stop_loss_price(entry_price, settings.stop_loss_rate)
    time_stop_date = add_trading_days(today, 5)

    state.positions.append(Position(
        ticker=pe.ticker,
        shares=shares,
        entry_price=entry_price,
        intended_entry_price=pe.reference_close,
        entry_date=today,
        stop_loss_price=stop,
        trailing_stop_price=stop,
        highest_price_since_entry=entry_price,
        time_stop_date=time_stop_date,
    ))
    state.cash -= entry_price * shares
    print(
        f"  ENTRY {pe.ticker} @ ¥{entry_price} ×{shares}  budget=¥{int(slot_budget):,}  "
        f"(sig {pe.signal_date}, RSI={pe.rsi14:.1f}, stop=¥{stop})",
        flush=True,
    )


def _process_exit(
    state: BacktestState,
    pos: Position,
    today: date,
    full_cache: dict[str, pd.DataFrame],
) -> Position | None:
    """1 Position の出口判定。退出時は None を返し、継続時は更新後 Position を返す。"""
    cache_df = full_cache.get(pos.ticker)
    if cache_df is None or cache_df.empty:
        return pos  # データなし → 継続扱い

    ohlc = _get_ohlc_for_date(cache_df, today)
    if ohlc is None:
        return pos

    _today_open, today_high, today_low, today_close = ohlc

    close_col = "AdjC" if "AdjC" in cache_df.columns else "C"
    high_col  = "AdjH" if "AdjH" in cache_df.columns else "H"
    low_col   = "AdjL" if "AdjL" in cache_df.columns else "L"
    day_data  = cache_df.loc[:str(today)]
    if day_data.empty:
        return pos

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
        exit_price = exit_dec.exit_price if exit_dec.exit_price is not None else today_close
        pnl = (exit_price - pos.entry_price) * pos.shares
        pnl_pct = float((exit_price / pos.entry_price - 1) * 100)
        holding_days = (today - pos.entry_date).days
        state.trades.append(TradeRecord(
            ticker=pos.ticker,
            signal_date=pos.entry_date,
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
        sign = "+" if pnl >= 0 else ""
        print(
            f"  EXIT  {pos.ticker} @ ¥{exit_price} → {exit_dec.reason.value if exit_dec.reason else '?'}  "
            f"P&L: ¥{sign}{int(pnl):,} ({sign}{pnl_pct:.1f}%)  hold={holding_days}d",
            flush=True,
        )
        return None  # exited

    # 継続: トレーリングストップ・直近高値更新
    new_highest = max(pos.highest_price_since_entry, today_high)
    if exit_dec.updated_trailing_stop is not None or new_highest > pos.highest_price_since_entry:
        new_trailing = exit_dec.updated_trailing_stop or pos.trailing_stop_price
        return Position(
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
    return pos


def _process_signal_scan(
    state: BacktestState,
    today: date,
    adj_close_full: pd.DataFrame,
    volume_full: pd.DataFrame,
    fund_base: pd.DataFrame,
    bps_map: dict[str, float],
    universe: list[str],
    open_slots: int,
    verbose: bool = False,
) -> None:
    """終値後にシグナルを検出し、翌営業日のエントリー候補（最大 open_slots 件）としてキューイング。"""
    if open_slots <= 0:
        return

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

    # 保有銘柄を候補から除外（重複ポジション防止）
    filtered = screener.exclude_held_positions(filtered, state.held_tickers)
    if filtered.empty:
        return

    ohlcv_sig = adj_slice.copy()
    for col in vol_slice.columns:
        ohlcv_sig[f"{col}_vol"] = vol_slice[col]

    candidates = signal_engine.detect_signals(
        filtered["ticker"].tolist(), ohlcv_sig, fund, today
    )
    if verbose:
        print(f"  [{today}] シグナル候補: {len(candidates)}件  open_slots={open_slots}")
    if not candidates:
        return

    # 動的予算で買えない高価銘柄を scan 段階で除外（資金不足見送りの削減）
    # 見積予算 = 残キャッシュ / 残空きスロット数、最悪ケース執行価 ≤ 見積予算 / 100 株 × buffer
    slot_budget_est = state.cash / max(open_slots, 1)
    max_buyable = (slot_budget_est / SHARES_PER_UNIT) / (1 + GAP_UP_CANCEL_THRESHOLD)
    affordable = [c for c in candidates if c.close <= max_buyable]
    if verbose:
        print(f"    予算フィルタ後: {len(affordable)}件 (max ¥{int(max_buyable)}/株)")
    if not affordable:
        return

    state.signals_found += 1
    top = ranking.rank(affordable, top_n=open_slots)
    for best in top:
        state.pending_entries.append(PendingEntry(
            ticker=best.ticker,
            signal_date=today,
            reference_close=best.close,
            rsi14=best.rsi14,
        ))
        print(
            f"  SIGNAL {best.ticker} @ ¥{best.close} RSI={best.rsi14:.1f}  (entry next day)",
            flush=True,
        )


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

    if state.positions:
        print(f"未決済ポジション : {len(state.positions)}件 / max {state.max_positions}")
        for p in state.positions:
            latest = (
                adj_close_full[p.ticker].dropna().iloc[-1]
                if p.ticker in adj_close_full.columns else p.entry_price
            )
            unrealized = (Decimal(str(round(float(latest), 1))) - p.entry_price) * p.shares
            sign = "+" if unrealized >= 0 else ""
            print(f"  {p.ticker} entry {p.entry_date}  "
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


def _apply_param_overrides(args: argparse.Namespace) -> None:
    """CLI 引数でパラメータを上書きする（grid search 用）。

    constants.py のモジュール属性と、各ドメインモジュールが import 済みの定数を
    両方書き換える必要がある（from X import Y はローカルバインディングのため）。
    """
    import squant.config.constants as C
    import squant.domain.position_manager as PM
    import squant.domain.quantity_calculator as QC
    import squant.domain.signal_engine as SE

    if args.target_profit is not None:
        rate = Decimal(str(args.target_profit))
        C.TARGET_PROFIT_RATE = rate
        QC.TARGET_PROFIT_RATE = rate
        # compute_take_profit_price の default 引数も差し替え
        _orig = QC.compute_take_profit_price
        def _patched_tp(entry_price, target_net_rate=rate, spread_rate=Decimal("0")):
            return _orig(entry_price, target_net_rate, spread_rate)
        QC.compute_take_profit_price = _patched_tp
        PM.compute_take_profit_price = _patched_tp  # position_manager の import バインドも

    if args.atr_mult is not None:
        m = Decimal(str(args.atr_mult))
        C.ATR_TRAILING_MULTIPLIER = m
        PM.ATR_TRAILING_MULTIPLIER = m

    if args.rsi_upper is not None:
        C.RSI_BUY_UPPER = float(args.rsi_upper)
        SE.RSI_BUY_UPPER = float(args.rsi_upper)

    if args.rsi_lower is not None:
        C.RSI_BUY_LOWER = float(args.rsi_lower)
        SE.RSI_BUY_LOWER = float(args.rsi_lower)

    if args.time_stop is not None:
        C.TIME_STOP_TRADING_DAYS = int(args.time_stop)
        PM.TIME_STOP_TRADING_DAYS = int(args.time_stop)


def _state_to_metrics(state: BacktestState, start: date, end: date) -> dict:
    """業界標準のリスク・リターン指標を一式算出する。

    主要指標（プロのファクトシート準拠）:
    - CAGR: 年率複利成長率
    - Sharpe Ratio: 取引リターン系列の年率化
    - Sortino Ratio: 下方リスクのみで年率化
    - Calmar Ratio: CAGR / |MaxDD%|
    - Win Rate / Profit Factor / Expectancy
    - Max Consecutive Wins / Losses
    """
    import math

    months = max(1.0, (end - start).days / 30.4)
    years = max(1.0 / 12, (end - start).days / 365.25)
    initial = float(state.initial_capital)

    pnl_list = [float(t.pnl) for t in state.trades]
    pct_list = [t.pnl_pct for t in state.trades]
    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]
    total = sum(pnl_list)

    by_reason: dict[str, int] = {}
    for t in state.trades:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1

    pf = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)

    # 累積エクイティ系列とドローダウン
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_duration = 0
    dd_duration = 0
    for p in pnl_list:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
            dd_duration = 0
        else:
            dd_duration += 1
            max_dd_duration = max(max_dd_duration, dd_duration)
        max_dd = min(max_dd, cumulative - peak)

    # CAGR: (1 + total_return) ^ (1/years) - 1
    final_equity = initial + total
    total_return = total / initial if initial > 0 else 0.0
    cagr = ((final_equity / initial) ** (1.0 / years) - 1.0) if initial > 0 and final_equity > 0 else -1.0

    # Sharpe / Sortino: 取引リターン%系列を annualization
    # 年あたり取引数で年率化（trade-based Sharpe）
    n_trades = len(pct_list)
    trades_per_year = (n_trades / years) if years > 0 else 0
    if n_trades >= 2 and trades_per_year > 0:
        mean_r = sum(pct_list) / n_trades / 100  # 比率に
        var_r = sum((r / 100 - mean_r) ** 2 for r in pct_list) / (n_trades - 1)
        std_r = math.sqrt(var_r)
        sharpe = (mean_r / std_r * math.sqrt(trades_per_year)) if std_r > 0 else 0.0
        # Sortino: 下方分散のみ
        downside = [r / 100 for r in pct_list if r < 0]
        if len(downside) >= 2:
            dvar = sum((r - 0) ** 2 for r in downside) / (n_trades - 1)
            dstd = math.sqrt(dvar)
            sortino = (mean_r / dstd * math.sqrt(trades_per_year)) if dstd > 0 else 0.0
        else:
            sortino = float("inf") if mean_r > 0 else 0.0
    else:
        sharpe = 0.0
        sortino = 0.0

    # Calmar Ratio: CAGR / |MaxDD%|
    max_dd_pct = max_dd / initial * 100
    calmar = (cagr * 100 / abs(max_dd_pct)) if max_dd_pct != 0 else 0.0

    # 連勝・連敗
    max_wins = max_losses = cur_wins = cur_losses = 0
    for p in pnl_list:
        if p > 0:
            cur_wins += 1
            cur_losses = 0
            max_wins = max(max_wins, cur_wins)
        elif p < 0:
            cur_losses += 1
            cur_wins = 0
            max_losses = max(max_losses, cur_losses)
        else:
            cur_wins = cur_losses = 0

    avg_holding = (sum(t.holding_days for t in state.trades) / n_trades) if n_trades > 0 else 0.0

    best_trade = max(pnl_list) if pnl_list else 0.0
    worst_trade = min(pnl_list) if pnl_list else 0.0

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    win_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf") if avg_win > 0 else 0.0

    return {
        # 基本カウント
        "trades": n_trades,
        "signals": state.signals_found,
        "gap_up_skipped": state.gap_up_skipped,
        "insufficient_skipped": state.insufficient_capital_skipped,
        "trades_per_month": n_trades / months,
        "trades_per_year": trades_per_year,
        "avg_holding_days": avg_holding,

        # リターン
        "total_pnl": total,
        "total_return_pct": total_return * 100,
        "monthly_pnl": total / months,
        "monthly_pnl_pct": total / initial * 100 / months,
        "cagr_pct": cagr * 100,
        "final_equity": final_equity,

        # 勝敗・期待値
        "win_rate": (len(wins) / n_trades) if n_trades > 0 else 0.0,
        "expectancy": (total / n_trades) if n_trades > 0 else 0.0,
        "expectancy_pct": (sum(pct_list) / n_trades) if n_trades > 0 else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,

        # リスク
        "max_dd": max_dd,
        "max_dd_pct": max_dd_pct,
        "max_dd_duration_trades": max_dd_duration,

        # リスク調整後リターン
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino if sortino != float("inf") else None,
        "calmar_ratio": calmar,
        "profit_factor": pf if pf != float("inf") else None,

        # 出口分布
        "by_reason": by_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="S-Quant バックテスト（改訂版・単元株・ザラ場モード）")
    parser.add_argument("--start", default="2024-01-04", help="開始日 YYYY-MM-DD")
    parser.add_argument("--end",   default="2025-12-30", help="終了日 YYYY-MM-DD")
    parser.add_argument("--budget", type=int, default=100_000, help="初期資本 (default: 100000)")
    parser.add_argument("--rpm", type=int, default=30, help="J-Quants RPM (default: 30)")
    parser.add_argument("--verbose", action="store_true", help="毎日のフィルタ結果を表示")
    parser.add_argument("--cache-dir", default=".backtest_cache", help="データキャッシュ保存先")
    # Grid search overrides
    parser.add_argument("--target-profit", type=float, default=None, help="利確目標 (例: 0.06)")
    parser.add_argument("--atr-mult", type=float, default=None, help="ATRトレーリング乗数 (例: 2.5)")
    parser.add_argument("--rsi-upper", type=float, default=None, help="RSI上限 (例: 50)")
    parser.add_argument("--rsi-lower", type=float, default=None, help="RSI下限 (例: 35)")
    parser.add_argument("--time-stop", type=int, default=None, help="タイムストップ営業日 (例: 5)")
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS,
                        help=f"同時保有銘柄数の上限 (default: {DEFAULT_MAX_POSITIONS} = Phase 1)")
    parser.add_argument("--json", action="store_true", help="JSONメトリクスを最終行に出力（grid search用）")
    parser.add_argument("--quiet", action="store_true", help="進捗ログ抑制（grid search用）")
    args = parser.parse_args()

    _apply_param_overrides(args)

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

    def _find_compatible_cache() -> Path | None:
        """必要期間 [fetch_start, end] をカバーする既存キャッシュを探す。
        ファイル名フォーマット: data_{cached_fetch_start}_{cached_end}.pkl
        cached_fetch_start <= fetch_start && cached_end >= end なら再利用可。
        """
        if not cache_dir.exists():
            return None
        candidates: list[tuple[Path, date, date]] = []
        for p in cache_dir.glob("data_*.pkl"):
            stem = p.stem.removeprefix("data_")
            try:
                cs_str, ce_str = stem.split("_")
                cs = date.fromisoformat(cs_str)
                ce = date.fromisoformat(ce_str)
            except (ValueError, IndexError):
                continue
            if cs <= fetch_start and ce >= end:
                candidates.append((p, cs, ce))
        if not candidates:
            return None
        # 最も小さい（タイトな）カバー範囲を優先
        candidates.sort(key=lambda x: (x[2] - x[1]).days)
        return candidates[0][0]

    usable_cache = cache_file if cache_file.exists() else _find_compatible_cache()

    if usable_cache is not None:
        if not args.quiet:
            note = "" if usable_cache == cache_file else " (期間カバー一致)"
            print(f"キャッシュ読み込み中: {usable_cache}{note}", flush=True)
        with usable_cache.open("rb") as f:
            cached = pickle.load(f)
        adj_close_full = cached["adj_close"]
        volume_full    = cached["volume"]
        full_cache     = cached["full_cache"]
        fund_base      = cached["fundamentals"]
        if not args.quiet:
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
    if not args.quiet:
        print(f"\nバックテスト期間: {start} 〜 {end}  ({len(trading_days)} 営業日)", flush=True)
        print(f"初期資本: ¥{args.budget:,}  単元: {SHARES_PER_UNIT}株  "
              f"最大同時保有: {args.max_positions}銘柄\n", flush=True)

    state = BacktestState(
        cash=Decimal(str(args.budget)),
        initial_capital=Decimal(str(args.budget)),
        max_positions=args.max_positions,
    )
    total_days = len(trading_days)
    report_interval = max(1, total_days // 8)

    import contextlib
    import io as _io

    def _run_loop():
        for i, today in enumerate(trading_days):
            # 1. 全 Position の出口判定（独立評価、退出時は positions から除外）
            updated: list[Position] = []
            for pos in state.positions:
                result = _process_exit(state, pos, today, full_cache)
                if result is not None:
                    updated.append(result)
            state.positions = updated

            # 2. 前日からの pending_entries を順次約定処理（空きスロットの上で）
            pending_today = state.pending_entries
            state.pending_entries = []  # 翌日には持ち越さない
            for pending in pending_today:
                if state.open_slots <= 0:
                    break
                _process_pending_entry(state, pending, today, full_cache, settings)

            # 3. 当日シグナルスキャン（翌営業日エントリー候補、空きスロット数まで）
            if state.open_slots > 0:
                _process_signal_scan(
                    state, today,
                    adj_close_full, volume_full,
                    fund_base, bps_map,
                    universe,
                    open_slots=state.open_slots,
                    verbose=args.verbose,
                )

            if not args.quiet and ((i + 1) % report_interval == 0 or (i + 1) == total_days):
                pct = (i + 1) / total_days * 100
                print(
                    f"  [{today}] 進捗 {i+1}/{total_days}日 ({pct:.0f}%)  "
                    f"取引{len(state.trades)}件  保有{len(state.positions)}件  "
                    f"シグナル{state.signals_found}回",
                    flush=True,
                )

    if args.quiet:
        with contextlib.redirect_stdout(_io.StringIO()):
            _run_loop()
    else:
        _run_loop()

    if not args.quiet:
        _print_report(state, start, end, adj_close_full)

    if args.json:
        metrics = _state_to_metrics(state, start, end)
        metrics["params"] = {
            "target_profit": args.target_profit,
            "atr_mult": args.atr_mult,
            "rsi_upper": args.rsi_upper,
            "rsi_lower": args.rsi_lower,
            "time_stop": args.time_stop,
            "max_positions": args.max_positions,
        }
        print("__METRICS_JSON__" + json.dumps(metrics))


if __name__ == "__main__":
    main()
