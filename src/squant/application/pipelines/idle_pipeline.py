"""IDLE state pipeline: screen → signal → rank → notify."""

from datetime import date, timedelta
from decimal import Decimal

from squant.config.constants import FUNNEL_ALERT_MIN_AVG, FUNNEL_ALERT_WINDOW_DAYS
from squant.config.settings import Settings
from squant.domain import ranking, screener, signal_engine
from squant.domain.exceptions import InsufficientCapitalError
from squant.domain.models import PendingSignal, PortfolioState, Signal
from squant.domain.quantity_calculator import (
    compute_cancel_threshold,
    compute_quantity,
    compute_stop_loss_price,
)
from squant.infrastructure.data_validator import DataValidator, Severity
from squant.infrastructure.interfaces import IClock, IMarketDataClient, INotifier, IStateRepository
from squant.presentation.slack_formatter import (
    format_buy_signals_summary,
    format_no_signal,
)
from squant.utils.jst import add_trading_days
from squant.utils.logging import get_logger

logger = get_logger(__name__)


class IdlePipeline:
    def __init__(
        self,
        state_repo: IStateRepository,
        market_data: IMarketDataClient,
        notifier: INotifier,
        validator: DataValidator,
        clock: IClock,
        settings: Settings,
        universe: list[str],
        blackouts: set[tuple[str, date]],
    ) -> None:
        self._repo = state_repo
        self._data = market_data
        self._notifier = notifier
        self._validator = validator
        self._clock = clock
        self._settings = settings
        self._universe = universe
        self._blackouts = blackouts

    def _record_funnel(
        self, run_date: date, *, valid: int, passed: int, candidates: int, signals: int,
    ) -> None:
        """日次スクリーニングファネルを funnel_log に記録し、通過数の低下をアラート。

        requirements §ユニバース健全性の監視（20日平均 < 3 で構造レビュー）の実装。
        テレメトリなので失敗しても運用は止めない（warning ログのみ）。
        """
        if self._settings.dry_run:
            return
        try:
            self._repo.append_funnel_log(
                run_date, universe=len(self._universe), valid_tickers=valid,
                screener_passed=passed, signal_candidates=candidates,
                signals_sent=signals,
            )
            counts = self._repo.load_recent_screener_counts(FUNNEL_ALERT_WINDOW_DAYS)
            if len(counts) >= FUNNEL_ALERT_WINDOW_DAYS:
                avg = sum(counts) / len(counts)
                if avg < FUNNEL_ALERT_MIN_AVG:
                    self._notifier.send(
                        f":warning: [S-Quant] ユニバース健全性アラート: "
                        f"スクリーニング通過数の{FUNNEL_ALERT_WINDOW_DAYS}日平均が "
                        f"{avg:.1f}銘柄 < {FUNNEL_ALERT_MIN_AVG:.0f}。"
                        f"構造レビュー（requirements §ユニバース健全性の監視）が必要です"
                    )
        except Exception as e:
            logger.warning(f"funnel_log recording failed (non-fatal): {e}")

    def run(self, portfolio: PortfolioState, run_id: str) -> PortfolioState:
        today = self._clock.today_jst()

        # Compute 差金決済 forbidden tickers
        recent_sales = self._repo.load_recent_sales()
        next_exec = add_trading_days(today, 1)
        forbidden = {s.ticker for s in recent_sales if s.settlement_date > next_exec}
        if forbidden:
            logger.info(f"差金決済 exclusion: {forbidden}")

        # Fetch market data — 160 calendar days ≈ 115 trading days (>= HISTORY_DAYS_REQUIRED=90)
        start = today - timedelta(days=160)
        total_tickers = len(self._universe)

        def _ohlcv_progress(done: int, total: int) -> None:
            pct = done / total * 100
            if done in {total // 3, total * 2 // 3, total}:
                self._notifier.send(f"[S-Quant] データ取得中: OHLCV {done}/{total}銘柄 ({pct:.0f}%)")

        adj_close, volume = self._data.fetch_ohlcv(
            self._universe, start, today, on_progress=_ohlcv_progress
        )

        # System-level freshness check — skipped when bypass_trading_day_check is on,
        # because a non-trading day legitimately has no fresh OHLCV for `today`.
        if self._settings.bypass_trading_day_check:
            logger.warning(
                "assert_universe_fresh skipped because bypass_trading_day_check is on"
            )
        else:
            self._validator.assert_universe_fresh(adj_close, today)

        # Per-ticker validation
        valid_tickers: list[str] = []
        for ticker in self._universe:
            if ticker not in adj_close.columns:
                continue
            close_series = adj_close[ticker].dropna()
            result = self._validator.validate_close_series(ticker, close_series, today)
            if result.severity == Severity.ABORT_RUN:
                raise RuntimeError(f"Abort-level validation failure for {ticker}: {result.issues}")
            if not result.ok:
                logger.debug(f"Skip {ticker}: {result.issues}")
                continue
            if ticker in volume.columns:
                vol_result = self._validator.validate_volume_series(ticker, volume[ticker].dropna())
                if not vol_result.ok:
                    logger.debug(f"Skip {ticker} (volume): {vol_result.issues}")
                    continue
            valid_tickers.append(ticker)

        logger.info(f"Valid tickers after validation: {len(valid_tickers)}/{len(self._universe)}")

        if not valid_tickers:
            logger.info("No valid tickers after data validation — skipping")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            self._record_funnel(today, valid=0, passed=0, candidates=0, signals=0)
            return portfolio

        self._notifier.send(
            f"[S-Quant] OHLCV取得完了 — 有効銘柄 {len(valid_tickers)}/{total_tickers}件、ファンダメンタルズ取得開始"
        )

        def _fund_progress(done: int, total: int) -> None:
            if done == total:
                self._notifier.send(f"[S-Quant] ファンダメンタルズ取得完了 — {total}銘柄、スクリーニング開始")

        # Fetch fundamentals for valid tickers
        fundamentals = self._data.fetch_fundamentals(valid_tickers, on_progress=_fund_progress)

        # Screener: fundamental + price + blackout filters
        filtered_df = screener.apply_fundamental_filters(
            valid_tickers, adj_close, fundamentals, today, self._blackouts
        )
        fc = filtered_df.attrs.get("filter_counts", {})
        logger.info(
            f"Screener filter counts (dropped): "
            f"no_fund={fc.get('no_fundamentals',0)} "
            f"market_cap={fc.get('market_cap',0)} "
            f"liquidity={fc.get('liquidity',0)} "
            f"pbr={fc.get('pbr',0)} "
            f"equity_ratio={fc.get('equity_ratio',0)} "
            f"price={fc.get('price',0)} "
            f"blackout={fc.get('blackout',0)} "
            f"passed={len(filtered_df)}"
        )
        screener_passed = len(filtered_df)
        if filtered_df.empty:
            logger.info("No candidates after fundamental screening")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            self._record_funnel(today, valid=len(valid_tickers), passed=0,
                                candidates=0, signals=0)
            return portfolio

        self._notifier.send(
            f"[S-Quant] スクリーニング通過: {len(filtered_df)}銘柄 — シグナル検出中"
        )

        filtered_df = screener.exclude_recent_sales(filtered_df, forbidden)
        # Multi-position: exclude tickers already in portfolio (held or pending)
        filtered_df = screener.exclude_held_positions(filtered_df, portfolio.held_tickers)
        if filtered_df.empty:
            logger.info("No candidates after recent-sales/held exclusion")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            self._record_funnel(today, valid=len(valid_tickers), passed=screener_passed,
                                candidates=0, signals=0)
            return portfolio

        filtered_tickers = filtered_df["ticker"].tolist()
        logger.info(f"Candidates after screening: {len(filtered_tickers)}")

        # Signal detection — pluggable strategy (pullback / ma_cross)
        ohlcv_for_signals = signal_engine.with_volume_columns(adj_close, volume)

        signal_func = signal_engine.get_signal_func(self._settings.signal_strategy)
        candidates = signal_func(filtered_tickers, ohlcv_for_signals, fundamentals, today)
        if not candidates:
            logger.info("No buy signals detected")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            self._record_funnel(today, valid=len(valid_tickers), passed=screener_passed,
                                candidates=0, signals=0)
            return portfolio

        # Multi-position: rank top-N where N = open slots
        open_slots = max(0, self._settings.max_positions - portfolio.in_use_slots)
        if open_slots == 0:
            logger.info("No open slots — skipping signal generation")
            self._record_funnel(today, valid=len(valid_tickers), passed=screener_passed,
                                candidates=len(candidates), signals=0)
            return portfolio
        top = ranking.rank(candidates, top_n=open_slots)
        logger.info(f"Top candidates ({len(top)}/{open_slots} slots): "
                    + ", ".join(f"{c.ticker}(RSI={c.rsi14:.1f})" for c in top))

        # Per-slot dynamic budget: remaining cash split across remaining open slots
        # (more accurate when partial fills are tracked; simple split for now)
        slot_budget = (portfolio.cash_jpy / open_slots).quantize(Decimal("1"))
        strategy = self._settings.signal_strategy

        new_pendings: list[PendingSignal] = []
        for best in top:
            try:
                shares = compute_quantity(
                    available_cash=portfolio.cash_jpy,
                    prev_close=best.close,
                    gap_up_threshold=self._settings.gap_up_threshold,
                    budget=slot_budget,
                )
            except InsufficientCapitalError as e:
                logger.warning(f"Skip {best.ticker}: {e}")
                continue

            cancel_price = compute_cancel_threshold(best.close, self._settings.gap_up_threshold)
            stop_price = compute_stop_loss_price(best.close, self._settings.stop_loss_rate)

            if strategy == "ma_cross":
                reason = (
                    f"5×25日MAクロス | 出来高急増={best.volume_surge_ratio:.2f}× | "
                    f"PBR={best.pbr:.2f} | 25日MA上向き"
                )
            else:
                reason = (
                    f"RSI={best.rsi14:.1f} | 出来高急増={best.volume_surge_ratio:.2f}× | "
                    f"PBR={best.pbr:.2f} | MA75上方 | ボラ収束"
                )

            signal = Signal(
                ticker=best.ticker,
                reference_price=best.close,
                shares=shares,
                cancel_above_price=cancel_price,
                stop_loss_price=stop_price,
                rsi=best.rsi14,
                reason=reason,
                generated_at=self._clock.now_jst(),
            )
            new_pendings.append(PendingSignal(signal=signal))
            logger.info(f"BUY signal queued: {signal.ticker} ×{signal.shares}")

        if not new_pendings:
            logger.info("No pending signals generated (all skipped)")
            self._record_funnel(today, valid=len(valid_tickers), passed=screener_passed,
                                candidates=len(candidates), signals=0)
            return portfolio

        # Single combined Slack notification for all queued signals (no spam)
        signals_for_slack = [p.signal for p in new_pendings]
        text, blocks = format_buy_signals_summary(signals_for_slack)
        self._notifier.send(text, blocks)

        # Merge with any existing pending signals (e.g. from yesterday awaiting fill)
        all_pendings = portfolio.pending_signals + tuple(new_pendings)

        if not self._settings.dry_run:
            self._repo.save_pending_signals(all_pendings)

        self._record_funnel(today, valid=len(valid_tickers), passed=screener_passed,
                            candidates=len(candidates), signals=len(new_pendings))

        from squant.domain.enums import SystemState

        new_portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT,
            cash_jpy=portfolio.cash_jpy,
            positions=portfolio.positions,
            pending_signals=all_pendings,
            settle_dates=portfolio.settle_dates,
            last_run_id=run_id,
            cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
        )

        if not self._settings.dry_run:
            self._repo.save_portfolio(new_portfolio)

        return new_portfolio
