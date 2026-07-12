"""Daily parity check — モデル予測 ↔ 本番実挙動の日次照合（検証強化 V-1）.

本番と同じ日付でバックテストエンジン（モデル）を影実行し、本番が Sheets に
記録した実挙動と突き合わせる。モデルと現実の乖離（F-2 型: 約定モデルの楽観、
データ差異、エンジン挙動差）を月次リターンより桁違いに速く検出する自動網。

照合ドメイン（対象日 D = 直近の本番 success ラン日）:
1. scan parity — funnel_log に D の行がある日（IDLE スキャン日）のみ。
   独立に J-Quants を再取得して本番と同じスクリーニング・シグナル検出を再実行し、
   有効銘柄数 / スクリーニング通過数 / 候補数 / シグナル数と、シグナル銘柄・
   参照価格（pending_signals タブ）を照合。件数の不一致は ALERT。
2. exit parity — D の開始時点で保有していた各ポジションについて、バックテスト
   の出口モデル（ザラ場 OCO・ギャップ考慮約定 = F-2 修正後）をエントリー日から
   D まで再生し、本番の記録（継続保有ならトレーリング値、決済済みなら
   trades 行の理由・価格）と照合。決済判断の不一致は ALERT、トレーリングの
   数値ドリフトは INFO（終値比 1% 超で ALERT）。
   注: モデルはザラ場安値でストップ発動を判定し、本番夜ランは終値でしか判定
   しない。「モデルは決済済み・本番は保有中」の ALERT は SBI の実逆指値が
   ザラ場で約定した可能性を意味する — オーナーに約定確認を促す通知になる。
3. entry parity — D にエントリーしたポジションについて、モデルの寄付約定・
   ギャップ見送り判定と実際の約定を照合。モデルが「見送り」なのに実エントリー
   していれば ALERT。

出力:
- 全照合結果を parity/parity_log.csv に追記（GHA が bot コミットで永続化）
- ALERT があった日だけ Slack 通知
- 本番パイプラインへの影響ゼロ（Sheets は読み取りのみ）

Usage:
    python scripts/parity_check.py                # 直近 success ラン日を照合
    python scripts/parity_check.py --date 2026-07-10
    python scripts/parity_check.py --dry-run      # CSV 追記・Slack なし
    python scripts/parity_check.py --skip-scan    # scan parity を省略（高速）
    python scripts/parity_check.py --force-scan   # funnel 行が無くても scan 実測
"""
import argparse
import csv
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from squant.config.constants import (  # noqa: E402
    SHEET_FUNNEL_LOG,
    SHEET_PENDING_SIGNALS,
    SHEET_RUN_LOG,
    SHEET_TRADES,
)
from squant.domain.enums import ExitReason  # noqa: E402
from squant.domain.models import Position  # noqa: E402
from squant.domain.position_manager import evaluate_exit  # noqa: E402
from squant.domain.quantity_calculator import compute_stop_loss_price  # noqa: E402
from squant.utils.jst import add_trading_days, is_tse_trading_day  # noqa: E402

# トレーリング数値ドリフトの ALERT しきい値（本番トレーリング値比）
TRAILING_DRIFT_ALERT_RATIO = Decimal("0.01")

_PARITY_CSV = Path(__file__).resolve().parent.parent / "parity" / "parity_log.csv"
_CSV_HEADER = [
    "run_date", "checked_at", "domain", "item", "model", "actual", "severity", "note",
]

# Sheets タブのスキーマ（sheets_repository と同値）
_RUN_LOG_HEADER = ["run_id", "run_date", "status", "note", "completed_at"]
_FUNNEL_HEADER = [
    "run_date", "universe", "valid_tickers", "screener_passed",
    "signal_candidates", "signals_sent",
]
_TRADES_HEADER = [
    "run_id", "ticker", "side", "shares", "price",
    "executed_at", "pnl_jpy", "exit_reason",
]
_PENDING_HEADER = [
    "run_id", "ticker", "reference_price", "shares", "cancel_above_price",
    "stop_loss_price", "rsi", "reason", "generated_at", "execution_status",
    "actual_entry_price", "actual_shares", "confirmed_at",
]


@dataclass
class ParityRow:
    run_date: date
    domain: str      # "run" | "scan" | "exit" | "entry"
    item: str
    model: str
    actual: str
    severity: str    # "ok" | "info" | "alert"
    note: str = ""


@dataclass
class ModelExitOutcome:
    """バックテスト出口モデルの再生結果。"""
    exited: bool
    exit_date: date | None = None
    reason: str = ""
    exit_price: Decimal | None = None
    trailing_stop: Decimal | None = None
    highest: Decimal | None = None
    days_replayed: int = 0
    note: str = ""


def _rows_to_dicts(rows: list[list[str]], header: list[str]) -> list[dict[str, str]]:
    out = []
    for raw in rows[1:] if rows else []:
        if not raw or not str(raw[0]).strip():
            continue
        padded = list(raw) + [""] * (len(header) - len(raw))
        out.append(dict(zip(header, padded)))  # noqa: B905
    return out


def resolve_target_date(run_rows: list[dict[str, str]]) -> tuple[date, str] | None:
    """run_log から直近の success 行の (run_date, note) を返す。無ければ None。"""
    for r in reversed(run_rows):
        if r.get("status") == "success":
            try:
                return date.fromisoformat(r["run_date"]), r.get("note", "")
            except ValueError:
                continue
    return None


def _dec(v: object) -> Decimal:
    """バックテストと同じ丸め（小数1桁）で Decimal 化する。"""
    return Decimal(str(round(float(v), 1)))  # type: ignore[arg-type]


def replay_exit_model(
    *,
    ticker: str,
    shares: int,
    entry_price: Decimal,
    entry_date: date,
    stop_loss_rate: Decimal,
    ohlc: pd.DataFrame,
    through: date,
) -> ModelExitOutcome:
    """バックテストの出口エンジン（ザラ場 OCO・ギャップ考慮約定）を再生する。

    scripts/backtest.py の _process_exit と同じ意味論:
    - evaluate_exit に intraday high/low を渡して OCO 発動を判定
    - 逆指値系は寄付がトリガー割れならば寄付価格で約定（F-2 ギャップ考慮）
    - 継続時は highest を当日高値で更新し、トレーリングをラチェット
    ohlc は "Adj Close" / "High" / "Low" 列を持つ日次 DataFrame
    （JQuantsClient.fetch_ohlcv_full の返り値形式）。
    """
    stop = compute_stop_loss_price(entry_price, stop_loss_rate)
    pos = Position(
        ticker=ticker,
        shares=shares,
        entry_price=entry_price,
        intended_entry_price=entry_price,
        entry_date=entry_date,
        stop_loss_price=stop,
        trailing_stop_price=stop,
        highest_price_since_entry=entry_price,
        time_stop_date=add_trading_days(entry_date, 5),
    )

    days = [
        d.date()
        for d in pd.bdate_range(entry_date + timedelta(days=1), through)
        if is_tse_trading_day(d.date())
    ]
    replayed = 0
    for day in days:
        try:
            row = ohlc.loc[str(day)]
        except KeyError:
            continue
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if row[["Adj Close", "High", "Low"]].isna().any():
            continue
        close = _dec(row["Adj Close"])
        high = _dec(row["High"])
        low = _dec(row["Low"])
        day_slice = ohlc.loc[:str(day)]
        replayed += 1

        decision = evaluate_exit(
            position=pos,
            today=day,
            latest_close=close,
            high_series=day_slice["High"],
            low_series=day_slice["Low"],
            close_series=day_slice["Adj Close"],
            intraday_high=high,
            intraday_low=low,
        )
        if decision.should_exit:
            exit_price = decision.exit_price if decision.exit_price is not None else close
            reason = decision.reason.value if decision.reason else "unknown"
            # F-2 ギャップ考慮: 寄付がトリガーを越えていれば寄付価格で約定
            open_price = (
                _dec(row["Open"])
                if "Open" in row.index and not pd.isna(row["Open"]) else None
            )
            if open_price is not None and (
                (reason in (ExitReason.STOP_LOSS.value, ExitReason.TRAILING_STOP.value)
                 and open_price < exit_price)
                or (reason == ExitReason.TAKE_PROFIT.value and open_price > exit_price)
            ):
                exit_price = open_price
            return ModelExitOutcome(
                exited=True, exit_date=day, reason=reason, exit_price=exit_price,
                trailing_stop=decision.updated_trailing_stop or pos.trailing_stop_price,
                highest=pos.highest_price_since_entry,
                days_replayed=replayed, note=decision.note,
            )

        new_highest = max(pos.highest_price_since_entry, high)
        new_trailing = decision.updated_trailing_stop or pos.trailing_stop_price
        pos = replace(
            pos,
            trailing_stop_price=new_trailing,
            highest_price_since_entry=new_highest,
        )

    return ModelExitOutcome(
        exited=False,
        trailing_stop=pos.trailing_stop_price,
        highest=pos.highest_price_since_entry,
        days_replayed=replayed,
    )


def compare_exit_still_held(
    run_date: date, position: Position, model: ModelExitOutcome,
) -> list[ParityRow]:
    """本番が D 終了時点で継続保有しているポジションとモデル再生の照合。"""
    rows: list[ParityRow] = []
    t = position.ticker

    if model.days_replayed == 0:
        rows.append(ParityRow(
            run_date, "exit", f"{t}.replay", "n/a", "HOLD", "info",
            "モデル再生に使える日次データなし（照合スキップ）",
        ))
        return rows

    if model.exited:
        rows.append(ParityRow(
            run_date, "exit", f"{t}.decision",
            f"EXIT {model.exit_date} {model.reason} @¥{model.exit_price}",
            "HOLD（本番は継続保有）", "alert",
            "モデルはザラ場ストップ約定を予測。SBI の実逆指値が約定していないか"
            "オーナー確認が必要",
        ))
        return rows

    rows.append(ParityRow(
        run_date, "exit", f"{t}.decision", "HOLD", "HOLD", "ok", model.note,
    ))
    # トレーリングのドリフト（モデル=ザラ場高値基準、本番=終値基準なので
    # モデル ≥ 本番が通常。逆転や大幅乖離は構造問題のシグナル）
    drift = (model.trailing_stop or Decimal("0")) - position.trailing_stop_price
    base = position.trailing_stop_price
    ratio = abs(drift) / base if base > 0 else Decimal("0")
    severity = "alert" if ratio > TRAILING_DRIFT_ALERT_RATIO else (
        "info" if drift != 0 else "ok"
    )
    rows.append(ParityRow(
        run_date, "exit", f"{t}.trailing_stop",
        str(model.trailing_stop), str(position.trailing_stop_price), severity,
        f"drift ¥{drift}（{ratio * 100:.2f}%）",
    ))
    rows.append(ParityRow(
        run_date, "exit", f"{t}.highest",
        str(model.highest), str(position.highest_price_since_entry),
        "info" if model.highest != position.highest_price_since_entry else "ok",
        "モデルはザラ場高値、本番は終値で更新（既知の定義差）",
    ))
    return rows


def compare_exit_traded(
    run_date: date, trade: dict[str, str], model: ModelExitOutcome,
) -> list[ParityRow]:
    """本番が D に決済したポジション（trades 行）とモデル再生の照合。"""
    rows: list[ParityRow] = []
    t = trade["ticker"]
    actual_reason = trade.get("exit_reason", "")
    actual_price = Decimal(trade["price"]) if trade.get("price") else None

    if not model.exited:
        rows.append(ParityRow(
            run_date, "exit", f"{t}.decision",
            "HOLD（モデルは継続保有）",
            f"EXIT {actual_reason} @¥{actual_price}", "alert",
            "本番は決済したがモデルは保有継続を予測 — 判定ロジックか入力データの乖離",
        ))
        return rows

    date_match = model.exit_date == run_date
    reason_match = model.reason == actual_reason
    severity = "ok" if (date_match and reason_match) else "alert"
    rows.append(ParityRow(
        run_date, "exit", f"{t}.decision",
        f"EXIT {model.exit_date} {model.reason}",
        f"EXIT {run_date} {actual_reason}", severity,
        "" if severity == "ok" else "決済日または理由が不一致",
    ))
    if actual_price is not None and model.exit_price is not None:
        diff = model.exit_price - actual_price
        rows.append(ParityRow(
            run_date, "exit", f"{t}.exit_price",
            str(model.exit_price), str(actual_price),
            "info" if diff != 0 else "ok",
            f"モデル−本番 = ¥{diff}（モデル=ザラ場ストップ/寄付、本番=終値記録。"
            "実約定は confirm_exit.py の slippage_log が正）",
        ))
    return rows


def compare_entry(
    run_date: date,
    position: Position,
    open_price: Decimal | None,
    gap_up_threshold: Decimal,
    stop_loss_rate: Decimal,
) -> list[ParityRow]:
    """D にエントリーしたポジションのモデル寄付約定判定との照合。"""
    t = position.ticker
    if open_price is None:
        return [ParityRow(
            run_date, "entry", f"{t}.decision", "n/a",
            f"ENTRY @¥{position.entry_price}", "info", "当日寄付価格が取得できず照合スキップ",
        )]

    intended = position.intended_entry_price
    cancel_above = intended * (1 + gap_up_threshold)
    stop_ref = compute_stop_loss_price(intended, stop_loss_rate)

    if open_price > cancel_above:
        model_decision = f"SKIP gap-up（寄付¥{open_price} > ¥{cancel_above}）"
        severity = "alert"
        note = "モデルはギャップアップ見送りを予測したが実際はエントリーした"
    elif open_price <= stop_ref:
        model_decision = f"SKIP gap-down（寄付¥{open_price} ≤ 推奨ストップ¥{stop_ref}）"
        severity = "alert"
        note = "モデルはギャップダウン見送りを予測したが実際はエントリーした"
    else:
        model_decision = f"ENTRY @¥{open_price}"
        diff = position.entry_price - open_price
        severity = "ok" if diff == 0 else "info"
        note = f"実約定−モデル寄付 = ¥{diff}（スリッページの正は slippage_log）"

    return [ParityRow(
        run_date, "entry", f"{t}.decision", model_decision,
        f"ENTRY @¥{position.entry_price}", severity, note,
    )]


def compare_scan_counts(
    run_date: date, funnel_row: dict[str, str] | None, model_counts: dict[str, int],
) -> list[ParityRow]:
    """funnel_log の実測件数とモデル再実行の件数を照合する。"""
    rows: list[ParityRow] = []
    for key in ("valid_tickers", "screener_passed", "signal_candidates", "signals_sent"):
        model_v = str(model_counts.get(key, ""))
        if funnel_row is None:
            rows.append(ParityRow(
                run_date, "scan", key, model_v, "n/a", "info",
                "funnel_log に当日行なし（--force-scan 実測のみ）",
            ))
            continue
        actual_v = funnel_row.get(key, "")
        severity = "ok" if model_v == actual_v else "alert"
        rows.append(ParityRow(
            run_date, "scan", key, model_v, actual_v, severity,
            "" if severity == "ok" else "独立再取得での再現に失敗 — データ差異かエンジン非決定性",
        ))
    return rows


def compare_signal_tickers(
    run_date: date,
    pending_rows: list[dict[str, str]],
    model_signals: list[tuple[str, Decimal]],
) -> list[ParityRow]:
    """pending_signals（D 生成分）とモデルのシグナル銘柄・参照価格を照合する。"""
    rows: list[ParityRow] = []
    actual = {r["ticker"]: r.get("reference_price", "") for r in pending_rows}
    model = {t: str(p) for t, p in model_signals}
    if not actual and not model:
        return rows

    if set(actual) != set(model):
        rows.append(ParityRow(
            run_date, "scan", "signal_tickers",
            ",".join(sorted(model)) or "(none)",
            ",".join(sorted(actual)) or "(none)", "alert",
            "シグナル銘柄セットが不一致",
        ))
    else:
        rows.append(ParityRow(
            run_date, "scan", "signal_tickers",
            ",".join(sorted(model)) or "(none)",
            ",".join(sorted(actual)) or "(none)", "ok",
        ))
        for t in sorted(actual):
            severity = "ok" if model[t] == actual[t] else "alert"
            rows.append(ParityRow(
                run_date, "scan", f"{t}.reference_price", model[t], actual[t], severity,
                "" if severity == "ok" else "参照価格（=シグナル日終値）が不一致",
            ))
    return rows


# 影実行の OHLCV 取得カバレッジがこれを下回ったら scan parity の alert を
# info に格下げする（429 レート制限による銘柄欠落を本物の乖離と誤認しないため）
SCAN_COVERAGE_MIN_RATIO = 0.9


def _run_scan_shadow(target: date, held_pre_run: set[str], cash_jpy: Decimal,
                     forbidden: set[str], settings,
                     ) -> tuple[dict[str, int], list[tuple[str, Decimal]], float]:
    """本番 idle_pipeline のスキャンを独立データ取得で再実行する（影実行）。

    戻り値: (funnel と同スキーマの件数 dict, [(シグナル銘柄, 参照価格)],
             OHLCV 取得カバレッジ比率)
    """
    from squant.application.universe_loader import load_earnings_blackouts, load_universe
    from squant.domain import ranking, screener, signal_engine
    from squant.domain.exceptions import InsufficientCapitalError
    from squant.domain.quantity_calculator import compute_quantity
    from squant.infrastructure.data_validator import DataValidator
    from squant.infrastructure.jquants_client import JQuantsClient

    client = JQuantsClient(
        api_key=settings.jquants_api_key, requests_per_minute=settings.jquants_rpm
    )
    validator = DataValidator()
    universe = load_universe()
    blackouts = load_earnings_blackouts(as_of=target)

    t0 = time.monotonic()
    start = target - timedelta(days=160)  # idle_pipeline と同じ取得窓
    adj_close, volume = client.fetch_ohlcv(universe, start, target)
    print(f"scan shadow: OHLCV fetch {time.monotonic() - t0:.0f}s "
          f"({len(adj_close.columns)}/{len(universe)} tickers)")

    valid_tickers: list[str] = []
    for ticker in universe:
        if ticker not in adj_close.columns:
            continue
        series = adj_close[ticker].dropna()
        if not validator.validate_close_series(ticker, series, target).ok:
            continue
        if ticker in volume.columns and \
                not validator.validate_volume_series(ticker, volume[ticker].dropna()).ok:
            continue
        valid_tickers.append(ticker)

    t1 = time.monotonic()
    fundamentals = client.fetch_fundamentals(valid_tickers)
    print(f"scan shadow: fundamentals fetch {time.monotonic() - t1:.0f}s "
          f"({len(fundamentals)} rows)")

    adj_slice = adj_close.loc[:str(target)]
    vol_slice = volume.loc[:str(target)]
    filtered = screener.apply_fundamental_filters(
        valid_tickers, adj_slice, fundamentals, target, blackouts
    )
    screener_passed = len(filtered)

    filtered = screener.exclude_recent_sales(filtered, forbidden)
    filtered = screener.exclude_held_positions(filtered, held_pre_run)

    candidates = []
    if not filtered.empty:
        ohlcv_sig = signal_engine.with_volume_columns(adj_slice, vol_slice)
        signal_func = signal_engine.get_signal_func(settings.signal_strategy)
        candidates = signal_func(filtered["ticker"].tolist(), ohlcv_sig, fundamentals, target)

    open_slots = max(0, settings.max_positions - len(held_pre_run))
    signals: list[tuple[str, Decimal]] = []
    if candidates and open_slots > 0:
        slot_budget = (cash_jpy / open_slots).quantize(Decimal("1"))
        for best in ranking.rank(candidates, top_n=open_slots):
            try:
                compute_quantity(
                    available_cash=cash_jpy,
                    prev_close=best.close,
                    gap_up_threshold=settings.gap_up_threshold,
                    budget=slot_budget,
                )
            except InsufficientCapitalError:
                continue
            signals.append((best.ticker, best.close))

    counts = {
        "valid_tickers": len(valid_tickers),
        "screener_passed": screener_passed,
        "signal_candidates": len(candidates),
        "signals_sent": len(signals),
    }
    coverage = len(adj_close.columns) / len(universe) if universe else 0.0
    return counts, signals, coverage


def downgrade_alerts_for_low_coverage(
    scan_rows: list[ParityRow], coverage: float,
    min_ratio: float = SCAN_COVERAGE_MIN_RATIO,
) -> list[ParityRow]:
    """影実行の取得カバレッジが低いとき scan parity の alert を info に格下げする。

    429 レート制限で影実行側の銘柄が欠けると件数不一致が必ず出るが、
    それは本番との乖離ではなく影実行の取得品質の問題のため。
    """
    if coverage >= min_ratio:
        return scan_rows
    for r in scan_rows:
        if r.severity == "alert":
            r.severity = "info"
            r.note = (f"{r.note}（判定保留: 影実行の取得カバレッジ "
                      f"{coverage * 100:.0f}% < {min_ratio * 100:.0f}%）")
    return scan_rows


def append_parity_csv(rows: list[ParityRow], checked_at: str, path: Path = _PARITY_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(_CSV_HEADER)
        for r in rows:
            w.writerow([
                r.run_date.isoformat(), checked_at, r.domain, r.item,
                r.model, r.actual, r.severity, r.note,
            ])


def main() -> int:
    parser = argparse.ArgumentParser(description="日次パリティ照合（モデル ↔ 本番実挙動）")
    parser.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時: 直近 success ラン日）")
    parser.add_argument("--dry-run", action="store_true", help="CSV 追記・Slack 送信なし")
    parser.add_argument("--skip-scan", action="store_true", help="scan parity を省略")
    parser.add_argument("--force-scan", action="store_true",
                        help="funnel_log に当日行が無くても scan 影実行を行う（実測用）")
    args = parser.parse_args()

    from squant.config.settings import Settings
    from squant.infrastructure.jquants_client import JQuantsClient
    from squant.infrastructure.sheets_client import GoogleSheetsClient
    from squant.infrastructure.sheets_repository import SheetsStateRepository
    from squant.infrastructure.slack_notifier import SlackNotifier
    from squant.utils.jst import now_jst

    settings = Settings()
    client = GoogleSheetsClient(
        sa_key_json=settings.gcp_sa_key_json,
        spreadsheet_id=settings.spreadsheet_id,
    )
    repo = SheetsStateRepository(client)

    run_rows = _rows_to_dicts(client.read_all(SHEET_RUN_LOG), _RUN_LOG_HEADER)
    if args.date:
        target = date.fromisoformat(args.date)
        run_note = next(
            (r.get("note", "") for r in reversed(run_rows)
             if r.get("run_date") == args.date and r.get("status") == "success"),
            None,
        )
    else:
        resolved = resolve_target_date(run_rows)
        if resolved is None:
            print("run_log に success 行がありません — 照合対象なし")
            return 0
        target, run_note = resolved

    checked_at = now_jst().isoformat(timespec="seconds")
    rows: list[ParityRow] = []

    if run_note is None:
        print(f"{target} の success ランが run_log にありません — 照合をスキップ")
        rows.append(ParityRow(
            target, "run", "coverage", "n/a", "no success run", "info",
            "本番ランが無い日は照合できない（watchdog が失敗検知を担当）",
        ))
    else:
        # ── 本番実挙動の読み取り（すべて読み取り専用）──────────────────────
        portfolio = repo.load_portfolio()
        trades_d = [
            t for t in _rows_to_dicts(client.read_all(SHEET_TRADES), _TRADES_HEADER)
            if t.get("side") == "SELL" and t.get("executed_at", "")[:10] == target.isoformat()
        ]
        funnel_row = next(
            (r for r in reversed(
                _rows_to_dicts(client.read_all(SHEET_FUNNEL_LOG), _FUNNEL_HEADER))
             if r.get("run_date") == target.isoformat()),
            None,
        )
        pending_d = [
            r for r in _rows_to_dicts(
                client.read_all(SHEET_PENDING_SIGNALS), _PENDING_HEADER)
            if r.get("generated_at", "")[:10] == target.isoformat()
        ]
        recent_sales = repo.load_recent_sales()
        next_exec = add_trading_days(target, 1)
        forbidden = {s.ticker for s in recent_sales if s.settlement_date > next_exec}

        held_now = {p.ticker: p for p in portfolio.positions}
        held_pre_run = {
            t for t, p in held_now.items() if p.entry_date < target
        } | {t["ticker"] for t in trades_d}
        entered_today = [p for p in portfolio.positions if p.entry_date == target]

        rows.append(ParityRow(
            target, "run", "coverage",
            f"scan={'yes' if funnel_row else 'no'} "
            f"exits={len(held_pre_run)} entries={len(entered_today)}",
            f"state={portfolio.state.value} note={run_note}", "ok",
        ))

        market = JQuantsClient(
            api_key=settings.jquants_api_key, requests_per_minute=settings.jquants_rpm
        )

        # ── exit parity: D 開始時点の保有ポジションのモデル再生 ─────────────
        for ticker in sorted(held_pre_run):
            pos = held_now.get(ticker)
            trade = next((t for t in trades_d if t["ticker"] == ticker), None)
            if pos is not None:
                entry_price, entry_date_, shares = pos.entry_price, pos.entry_date, pos.shares
            else:
                # D に決済済み: エントリー情報を trades 行から復元
                # (entry = exit_price - pnl/shares。日付は slippage_log の BUY 行)
                info = _reconstruct_entry(repo, trade)
                if info is None:
                    rows.append(ParityRow(
                        target, "exit", f"{ticker}.replay", "n/a",
                        f"EXIT {trade.get('exit_reason', '')}", "info",
                        "エントリー情報を復元できず再生スキップ（slippage_log に BUY 行なし）",
                    ))
                    continue
                entry_price, entry_date_, shares = info

            ohlc = market.fetch_ohlcv_full(
                [ticker], entry_date_ - timedelta(days=160), target
            )
            if ohlc.empty:
                rows.append(ParityRow(
                    target, "exit", f"{ticker}.replay", "n/a", "n/a", "info",
                    "OHLC 取得失敗で再生スキップ",
                ))
                continue
            model = replay_exit_model(
                ticker=ticker, shares=shares, entry_price=entry_price,
                entry_date=entry_date_, stop_loss_rate=settings.stop_loss_rate,
                ohlc=ohlc, through=target,
            )
            if trade is not None:
                rows.extend(compare_exit_traded(target, trade, model))
            else:
                rows.extend(compare_exit_still_held(target, pos, model))

        # ── entry parity: D にエントリーしたポジション ──────────────────────
        for pos in entered_today:
            ohlc = market.fetch_ohlcv_full(
                [pos.ticker], target - timedelta(days=10), target
            )
            open_price = None
            if not ohlc.empty and "Open" in ohlc.columns:
                try:
                    r = ohlc.loc[str(target)]
                    if isinstance(r, pd.DataFrame):
                        r = r.iloc[0]
                    if not pd.isna(r["Open"]):
                        open_price = _dec(r["Open"])
                except KeyError:
                    pass
            rows.extend(compare_entry(
                target, pos, open_price,
                settings.gap_up_threshold, settings.stop_loss_rate,
            ))

        # ── scan parity: IDLE スキャン日のみ（または --force-scan）──────────
        if not args.skip_scan and (funnel_row is not None or args.force_scan):
            counts, signals, coverage = _run_scan_shadow(
                target, held_pre_run, portfolio.cash_jpy, forbidden, settings
            )
            scan_rows = compare_scan_counts(target, funnel_row, counts)
            if funnel_row is not None:
                # 銘柄・参照価格の照合は本番スキャンがあった日のみ意味を持つ
                scan_rows.extend(compare_signal_tickers(target, pending_d, signals))
            elif signals:
                scan_rows.append(ParityRow(
                    target, "scan", "signal_tickers",
                    ",".join(t for t, _ in signals), "n/a", "info",
                    "--force-scan 実測（本番スキャンなし日のため照合対象なし）",
                ))
            rows.extend(downgrade_alerts_for_low_coverage(scan_rows, coverage))
        elif funnel_row is None:
            rows.append(ParityRow(
                target, "scan", "coverage", "n/a", "no funnel row", "ok",
                "スキャンが走らない日（保有中/CB/非営業日）は scan parity 対象外",
            ))

    # ── 出力 ────────────────────────────────────────────────────────────────
    alerts = [r for r in rows if r.severity == "alert"]
    infos = [r for r in rows if r.severity == "info"]
    for r in rows:
        mark = {"ok": "✓", "info": "i", "alert": "!"}[r.severity]
        print(f"  [{mark}] {r.domain}/{r.item}: model={r.model} actual={r.actual} {r.note}")
    print(f"parity {target}: {len(rows)} checks, {len(alerts)} alerts, {len(infos)} infos")

    if args.dry_run:
        print("[dry-run] CSV 追記・Slack 送信なし")
        return 0

    append_parity_csv(rows, checked_at)

    if alerts:
        msg = (
            f":rotating_light: *[S-Quant] パリティ照合乖離 — {target}*\n"
            "モデル予測と本番実挙動に差分を検出しました。\n"
            + "\n".join(
                f"• `{r.domain}/{r.item}` モデル: {r.model} / 実際: {r.actual}"
                + (f"\n　→ {r.note}" if r.note else "")
                for r in alerts
            )
        )
        SlackNotifier(settings.slack_webhook_url).send(msg)
        return 1

    return 0


def _reconstruct_entry(repo, trade: dict[str, str]) -> tuple[Decimal, date, int] | None:
    """D に決済済みのポジションのエントリー情報を復元する。

    entry_price = exit_price - pnl/shares（trades 行から厳密算出）。
    entry_date は slippage_log の直近 BUY 行の date。無ければ復元不能。
    """
    try:
        shares = int(trade["shares"])
        exit_price = Decimal(trade["price"])
        pnl = Decimal(trade["pnl_jpy"])
    except (KeyError, ValueError, ArithmeticError):
        return None
    if shares <= 0:
        return None
    entry_price = exit_price - pnl / shares

    buy_rows = [
        r for r in repo.load_slippage_rows()
        if r.get("ticker") == trade["ticker"] and r.get("side") == "BUY"
    ]
    if not buy_rows:
        return None
    try:
        entry_date_ = date.fromisoformat(buy_rows[-1]["date"])
    except ValueError:
        return None
    return entry_price, entry_date_, shares


if __name__ == "__main__":
    sys.exit(main())
