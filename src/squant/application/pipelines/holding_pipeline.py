"""HOLDING state pipeline: evaluate exit rules and notify."""

from datetime import timedelta
from decimal import Decimal

from squant.config.settings import Settings
from squant.domain import circuit_breaker as cb_module
from squant.domain.enums import OrderSide, SystemState
from squant.domain.models import (
    PortfolioState,
    Position,
    RecentSale,
    TradeRecord,
)
from squant.domain.position_manager import evaluate_exit
from squant.infrastructure.data_validator import DataValidator
from squant.infrastructure.interfaces import IClock, IMarketDataClient, INotifier, IStateRepository
from squant.presentation.slack_formatter import format_exit_signal, format_hold_status
from squant.utils.jst import calculate_settlement_date, count_trading_days
from squant.utils.logging import get_logger

logger = get_logger(__name__)


class HoldingPipeline:
    def __init__(
        self,
        state_repo: IStateRepository,
        market_data: IMarketDataClient,
        notifier: INotifier,
        validator: DataValidator,
        clock: IClock,
        settings: Settings,
    ) -> None:
        self._repo = state_repo
        self._data = market_data
        self._notifier = notifier
        self._validator = validator
        self._clock = clock
        self._settings = settings

    def run(self, portfolio: PortfolioState, run_id: str) -> PortfolioState:
        """Evaluate exit rules for each held Position independently.

        Multi-position support (2026-05-30): when portfolio has multiple positions,
        process each through the same single-position pipeline sequentially, then
        aggregate the results into one PortfolioState.
        """
        if not portfolio.positions:
            logger.error("HOLDING state but no positions — resetting to IDLE")
            return PortfolioState(
                state=SystemState.IDLE,
                cash_jpy=portfolio.cash_jpy,
                pending_signals=portfolio.pending_signals,
                settle_dates=portfolio.settle_dates,
                last_run_id=run_id,
                cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            )

        # Multi-position: run the single-position loop body for each position,
        # threading the cash / settle_dates / cumulative_pnl through iterations.
        if len(portfolio.positions) > 1:
            current = portfolio
            for pos in portfolio.positions:
                # Build a single-position view for the legacy body, preserving the
                # accumulating fields, and merge results back into `current`.
                single_view = PortfolioState(
                    state=SystemState.HOLDING,
                    cash_jpy=current.cash_jpy,
                    positions=(pos,),
                    pending_signals=(),
                    settle_dates=(),
                    last_run_id=current.last_run_id,
                    cumulative_pnl_jpy=current.cumulative_pnl_jpy,
                )
                result = self._run_single(single_view, run_id)
                # Aggregate
                current = PortfolioState(
                    state=result.state if result.positions else current.state,
                    cash_jpy=result.cash_jpy,
                    positions=current.positions[:current.positions.index(pos)]
                              + result.positions
                              + current.positions[current.positions.index(pos)+1:],
                    pending_signals=current.pending_signals,
                    settle_dates=current.settle_dates + result.settle_dates,
                    last_run_id=run_id,
                    cumulative_pnl_jpy=result.cumulative_pnl_jpy,
                )
            # Final state: HOLDING if any positions remain, else SETTLING/IDLE
            remaining = tuple(p for p in current.positions if p is not None)
            if remaining:
                final_state = SystemState.HOLDING
            elif current.settle_dates:
                final_state = SystemState.SETTLING
            else:
                final_state = SystemState.IDLE
            return PortfolioState(
                state=final_state,
                cash_jpy=current.cash_jpy,
                positions=remaining,
                pending_signals=current.pending_signals,
                settle_dates=current.settle_dates,
                last_run_id=run_id,
                cumulative_pnl_jpy=current.cumulative_pnl_jpy,
            )

        return self._run_single(portfolio, run_id)

    def _run_single(self, portfolio: PortfolioState, run_id: str) -> PortfolioState:
        today = self._clock.today_jst()
        position = portfolio.positions[0]

        # Fetch OHLCV for the held ticker.
        # 160 calendar days ≈ 115 trading days — idle_pipeline と同じ窓。
        # 旧値 30日 (≈23営業日) は validator の HISTORY_DAYS_REQUIRED=90 を
        # 常に下回り、本番初 HOLDING (2026-07-10 2201.T) で「データ品質警告 →
        # 出口評価スキップ」が毎晩発生していた（トレーリング更新・タイムストップ
        # 判定が不動作）。ATR(14) 計算にも 90日超の窓で品質が安定する。
        start = today - timedelta(days=160)
        raw = self._data.fetch_ohlcv_full([position.ticker], start, today)

        if raw.empty:
            logger.warning(f"No data for held ticker {position.ticker} — skipping exit check")
            self._notifier.send_error(
                "データ取得失敗",
                f"保有銘柄 {position.ticker} のデータが取得できませんでした。手動で確認してください。",
            )
            return portfolio

        # Extract OHLCV series
        if isinstance(raw.columns, tuple.__class__):
            raw = raw  # already handled
        try:
            if isinstance(raw.columns, type(raw.columns)) and hasattr(raw.columns, "levels"):
                # MultiIndex
                close = raw["Adj Close"][position.ticker].dropna()
                high = raw["High"][position.ticker].dropna()
                low = raw["Low"][position.ticker].dropna()
                _ = raw["Volume"][position.ticker].dropna()
            else:
                close = raw["Adj Close"].dropna() if "Adj Close" in raw.columns else raw["Close"].dropna()
                high = raw["High"].dropna()
                low = raw["Low"].dropna()
                _ = raw["Volume"].dropna()
        except (KeyError, TypeError) as e:
            logger.warning(f"Data extraction failed for {position.ticker}: {e}")
            return portfolio

        # Validate
        result = self._validator.validate_close_series(position.ticker, close, today)
        if not result.ok:
            logger.warning(f"Data quality issue for {position.ticker}: {result.issues}")
            self._notifier.send_error(
                "データ品質警告",
                f"{position.ticker}: {', '.join(result.issues)}\n手動で価格を確認してください。",
            )
            return portfolio

        latest_close = Decimal(str(round(float(close.iloc[-1]), 1)))

        # Evaluate exit
        exit_decision = evaluate_exit(
            position=position,
            today=today,
            latest_close=latest_close,
            high_series=high,
            low_series=low,
            close_series=close,
        )

        days_held = count_trading_days(position.entry_date, today)

        if not exit_decision.should_exit:
            # Update trailing stop and notify HOLD status
            trailing = exit_decision.updated_trailing_stop or position.trailing_stop_price

            updated_position = Position(
                ticker=position.ticker,
                shares=position.shares,
                entry_price=position.entry_price,
                intended_entry_price=position.intended_entry_price,
                entry_date=position.entry_date,
                stop_loss_price=position.stop_loss_price,
                trailing_stop_price=trailing,
                highest_price_since_entry=max(position.highest_price_since_entry, latest_close),
                time_stop_date=position.time_stop_date,
            )
            new_portfolio = PortfolioState(
                state=SystemState.HOLDING,
                cash_jpy=portfolio.cash_jpy,
                positions=(updated_position,),
                last_run_id=run_id,
                cumulative_pnl_jpy=portfolio.cumulative_pnl_jpy,
            )
            if not self._settings.dry_run:
                self._repo.save_portfolio(new_portfolio)

            text, blocks = format_hold_status(position.ticker, days_held, latest_close, trailing)
            self._notifier.send(text, blocks)
            return new_portfolio

        # Exit triggered — compute P&L and transition to SETTLING
        pnl = (latest_close - position.entry_price) * position.shares
        settle_date = calculate_settlement_date(today)

        trade = TradeRecord(
            ticker=position.ticker,
            side=OrderSide.SELL,
            shares=position.shares,
            price=latest_close,
            executed_at=self._clock.now_jst(),
            pnl_jpy=pnl,
            exit_reason=exit_decision.reason,
            run_id=run_id,
        )

        # Update circuit breaker
        cb_status = self._repo.load_circuit_breaker()
        new_cb = cb_module.update_after_trade(cb_status, trade)

        new_cash = portfolio.cash_jpy + latest_close * position.shares
        new_cumulative = portfolio.cumulative_pnl_jpy + pnl

        new_portfolio = PortfolioState(
            state=SystemState.SETTLING,
            cash_jpy=new_cash,
            positions=(),
            settle_dates=(settle_date,),
            last_run_id=run_id,
            cumulative_pnl_jpy=new_cumulative,
        )

        if not self._settings.dry_run:
            self._repo.append_trade(trade)
            self._repo.append_recent_sale(
                RecentSale(
                    ticker=position.ticker,
                    sell_date=today,
                    settlement_date=settle_date,
                )
            )
            self._repo.save_circuit_breaker(new_cb)
            self._repo.save_portfolio(new_portfolio)

        text, blocks = format_exit_signal(position.ticker, exit_decision, latest_close)
        self._notifier.send(text, blocks)

        pnl_sign = "+" if pnl >= 0 else ""
        logger.info(
            f"EXIT {position.ticker} ×{position.shares} "
            f"P&L={pnl_sign}¥{int(pnl)} reason={exit_decision.reason}"
        )
        return new_portfolio
