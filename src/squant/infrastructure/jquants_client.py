"""J-Quants (JPX official API) market data client — replaces yfinance."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import httpx
import pandas as pd

from squant.utils.logging import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.jquants.com/v1"
_MAX_WORKERS = 8  # concurrent requests; stay well within J-Quants rate limits


class JQuantsClient:
    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._id_token: str | None = None
        self._token_expires_at: float = 0.0
        # Cache full daily_quotes DataFrame per ticker within one run;
        # used by fetch_fundamentals to derive TurnoverValue stats.
        self._ohlcv_cache: dict[str, pd.DataFrame] = {}

    # ── Authentication ─────────────────────────────────────────────────────────

    def _ensure_token(self) -> str:
        if self._id_token and time.monotonic() < self._token_expires_at:
            return self._id_token
        self._id_token = self._acquire_id_token()
        self._token_expires_at = time.monotonic() + 23 * 3600  # 24 h validity; refresh 1 h early
        logger.info("J-Quants ID token acquired")
        return self._id_token

    def _acquire_id_token(self) -> str:
        r1 = httpx.post(
            f"{_BASE_URL}/token/auth_user",
            json={"mailaddress": self._email, "password": self._password},
            timeout=30,
        )
        r1.raise_for_status()
        refresh_token: str = r1.json()["refreshToken"]

        r2 = httpx.post(
            f"{_BASE_URL}/token/auth_refresh",
            params={"refreshtoken": refresh_token},
            timeout=30,
        )
        r2.raise_for_status()
        return r2.json()["idToken"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # ── Ticker format ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_code(ticker: str) -> str:
        """'7203.T' → '72030' (JPX 5-digit code with trailing '0' for common stock)."""
        return ticker.replace(".T", "") + "0"

    # ── Price data ─────────────────────────────────────────────────────────────

    def _fetch_daily_quotes(self, ticker: str, start: date, end: date) -> pd.DataFrame | None:
        code = self._to_code(ticker)
        rows: list[dict] = []
        pagination_key: str | None = None

        while True:
            params: dict = {"code": code, "from": start.isoformat(), "to": end.isoformat()}
            if pagination_key:
                params["pagination_key"] = pagination_key

            try:
                resp = httpx.get(
                    f"{_BASE_URL}/prices/daily_quotes",
                    params=params,
                    headers=self._headers(),
                    timeout=30,
                )
            except Exception as e:
                logger.debug(f"J-Quants network error for {ticker}: {e}")
                return None

            if resp.status_code in (429, 503):
                logger.warning(f"J-Quants rate limit ({resp.status_code}) for {ticker}")
                return None
            if not resp.is_success:
                logger.debug(f"J-Quants HTTP {resp.status_code} for {ticker}")
                return None

            body = resp.json()
            rows.extend(body.get("daily_quotes", []))
            pagination_key = body.get("pagination_key") or None
            if not pagination_key:
                break

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["Date"])
        return df.set_index("Date").sort_index()

    def fetch_ohlcv(
        self, tickers: list[str], start: date, end: date
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (adj_close_df, volume_df) — tickers as columns, dates as index."""
        self._ensure_token()
        self._ohlcv_cache.clear()

        adj_close_map: dict[str, pd.Series] = {}
        volume_map: dict[str, pd.Series] = {}

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._fetch_daily_quotes, t, start, end): t
                for t in tickers
            }
            for future in as_completed(futures):
                ticker = futures[future]
                df = future.result()
                if df is None or df.empty:
                    continue
                self._ohlcv_cache[ticker] = df

                adj_col = "AdjustmentClose" if "AdjustmentClose" in df.columns else "Close"
                adj_close_map[ticker] = df[adj_col].rename(ticker)

                vol_col = "AdjustmentVolume" if "AdjustmentVolume" in df.columns else "Volume"
                volume_map[ticker] = df[vol_col].rename(ticker)

        if not adj_close_map:
            return pd.DataFrame(), pd.DataFrame()

        adj_close = pd.DataFrame(adj_close_map)
        volume = pd.DataFrame(volume_map)
        return adj_close, volume

    def fetch_ohlcv_full(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        return pd.DataFrame()

    # ── Fundamentals ───────────────────────────────────────────────────────────

    def _fetch_latest_statement(self, ticker: str) -> dict:
        """Return most-recently-disclosed financial statement dict (or {})."""
        code = self._to_code(ticker)
        try:
            resp = httpx.get(
                f"{_BASE_URL}/fins/statements",
                params={"code": code},
                headers=self._headers(),
                timeout=30,
            )
            if not resp.is_success:
                return {}
            stmts: list[dict] = resp.json().get("statements", [])
            if not stmts:
                return {}
            return max(stmts, key=lambda s: s.get("DisclosedDate", ""))
        except Exception as e:
            logger.debug(f"J-Quants statements error for {ticker}: {e}")
            return {}

    def fetch_fundamentals(self, tickers: list[str]) -> pd.DataFrame:
        """Derive market_cap, PBR, equity_ratio, avg_5d_trading_value from J-Quants."""
        self._ensure_token()

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._fetch_latest_statement, t): t for t in tickers
            }
            stmt_map: dict[str, dict] = {
                futures[f]: f.result() for f in as_completed(futures)
            }

        records = []
        for ticker in tickers:
            stmt = stmt_map.get(ticker, {})
            cached = self._ohlcv_cache.get(ticker)

            # ── Liquidity: avg of last 5 days TurnoverValue (yen) ──
            avg_5d_tv = 0.0
            if cached is not None and "TurnoverValue" in cached.columns:
                avg_5d_tv = float(cached["TurnoverValue"].tail(5).mean() or 0)

            # ── Financial statement values ──
            # J-Quants monetary fields are in JPY (yen).
            # NetAssets / TotalAssets are balance-sheet totals.
            net_assets = float(stmt.get("NetAssets", 0) or 0)
            total_assets = float(stmt.get("TotalAssets", 0) or 0)
            shares = float(
                stmt.get("NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYear", 0) or 0
            )
            bvps = float(stmt.get("BookValuePerShare", 0) or 0)

            equity_ratio = net_assets / total_assets if total_assets > 0 else 0.0

            # ── Market cap and PBR (need current close) ──
            last_close = 0.0
            if cached is not None and not cached.empty:
                close_col = "AdjustmentClose" if "AdjustmentClose" in cached.columns else "Close"
                last_close = float(cached[close_col].iloc[-1])

            market_cap = last_close * shares if shares > 0 and last_close > 0 else 0.0
            pbr = last_close / bvps if bvps > 0 and last_close > 0 else 0.0

            records.append({
                "ticker": ticker,
                "market_cap_jpy": market_cap,
                "pbr": pbr,
                "equity_ratio": equity_ratio,
                "avg_5d_trading_value_jpy": avg_5d_tv,
            })

        df = pd.DataFrame(records)
        return df.set_index("ticker") if not df.empty else df

    # ── Connectivity ───────────────────────────────────────────────────────────

    def check_connectivity(self) -> bool:
        try:
            self._ensure_token()
            return True
        except Exception as e:
            logger.error(f"J-Quants connectivity check failed: {e}")
            return False
