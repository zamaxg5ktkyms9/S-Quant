"""IDLE state pipeline: screen → signal → rank → notify."""

from datetime import date, timedelta

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
from squant.presentation.slack_formatter import format_buy_signal, format_no_signal
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
        adj_close, volume = self._data.fetch_ohlcv(self._universe, start, today)

        # System-level freshness check
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
            return portfolio

        # Fetch fundamentals for valid tickers
        fundamentals = self._data.fetch_fundamentals(valid_tickers)

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
        if filtered_df.empty:
            logger.info("No candidates after fundamental screening")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            return portfolio

        filtered_df = screener.exclude_recent_sales(filtered_df, forbidden)
        if filtered_df.empty:
            logger.info("No candidates after recent-sales exclusion")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            return portfolio

        filtered_tickers = filtered_df["ticker"].tolist()
        logger.info(f"Candidates after screening: {len(filtered_tickers)}")

        # Signal detection
        # Merge volume columns so signal_engine can find {ticker}_vol
        ohlcv_for_signals = adj_close.copy()
        for col in volume.columns:
            ohlcv_for_signals[f"{col}_vol"] = volume[col]

        candidates = signal_engine.detect_signals(
            filtered_tickers, ohlcv_for_signals, fundamentals, today
        )
        if not candidates:
            logger.info("No buy signals detected")
            text, blocks = format_no_signal()
            self._notifier.send(text, blocks)
            return portfolio

        # Ranking: pick top-1
        top = ranking.rank(candidates, top_n=1)
        best = top[0]
        logger.info(f"Top candidate: {best.ticker} RSI={best.rsi14:.1f}")

        # Quantity calculation
        try:
            shares = compute_quantity(
                available_cash=portfolio.cash_jpy,
                prev_close=best.close,
                gap_up_threshold=self._settings.gap_up_threshold,
                budget=self._settings.budget_jpy,
            )
        except InsufficientCapitalError as e:
            logger.error(str(e))
            self._notifier.send_error("資金不足", str(e))
            return portfolio

        cancel_price = compute_cancel_threshold(best.close, self._settings.gap_up_threshold)
        stop_price = compute_stop_loss_price(best.close, self._settings.stop_loss_rate)

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

        pending = PendingSignal(signal=signal)

        if not self._settings.dry_run:
            self._repo.save_pending_signal(pending)

        text, blocks = format_buy_signal(signal)
        self._notifier.send(text, blocks)
        logger.info(f"BUY signal sent: {signal.ticker} ×{signal.shares}")

        # Transition to SIGNAL_SENT state (awaiting operator confirmation)

        from squant.domain.enums import SystemState

        new_portfolio = PortfolioState(
            state=SystemState.SIGNAL_SENT,
            cash_jpy=portfolio.cash_jpy,
            last_run_id=run_id,
            cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
        )

        if not self._settings.dry_run:
            self._repo.save_portfolio(new_portfolio)

        return new_portfolio
