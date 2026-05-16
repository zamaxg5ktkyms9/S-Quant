"""J-Quants v2 (JPX official API) market data client.

Auth: API key via x-api-key header (v2, registered 2025-12-22+).
Docs: https://jpx-jquants.com/spec

Rate limiting: J-Quants counts per clock-minute (not sliding window).
Default RPM=50 keeps 17% below the Light-plan 60/min limit, preventing
the 429 → 5-minute block cascade.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import httpx
import pandas as pd

from squant.utils.logging import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.jquants.com/v2"
_MAX_WORKERS = 8
_429_BACKOFF_SECONDS = 65.0  # slightly over 1 minute to clear the clock-minute window


class _RateLimiter:
    """Thread-safe per-request rate limiter with global 429 backoff."""

    def __init__(self, rpm: int) -> None:
        self._interval = 60.0 / max(rpm, 1)
        self._lock = threading.Lock()
        self._last: float = 0.0
        self._backoff_until: float = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            # Honour global backoff (triggered by any 429 response)
            if now < self._backoff_until:
                wait_for = self._backoff_until - now
                logger.info(f"J-Quants rate backoff: waiting {wait_for:.0f}s")
                time.sleep(wait_for)

            now = time.monotonic()
            gap = self._interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

    def set_backoff(self, seconds: float = _429_BACKOFF_SECONDS) -> None:
        """Called by any worker that receives a 429; all workers will then pause."""
        with self._lock:
            until = time.monotonic() + seconds
            if until > self._backoff_until:
                self._backoff_until = until
                logger.warning(f"J-Quants 429 — setting global backoff for {seconds:.0f}s")


class JQuantsClient:
    def __init__(self, api_key: str, requests_per_minute: int = 50) -> None:
        self._api_key = api_key
        self._limiter = _RateLimiter(requests_per_minute)
        # Cache full daily DataFrame per ticker within one run;
        # used by fetch_fundamentals to derive Va (trading value) stats.
        self._ohlcv_cache: dict[str, pd.DataFrame] = {}

    # ── Headers ────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    # ── Ticker format ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_code(ticker: str) -> str:
        """'7203.T' → '72030' (JPX 5-digit code with trailing '0' for common stock)."""
        return ticker.replace(".T", "") + "0"

    # ── Price data ─────────────────────────────────────────────────────────────

    def _fetch_daily_quotes(self, ticker: str, start: date, end: date) -> pd.DataFrame | None:
        code = self._to_code(ticker)

        for attempt in range(2):  # one retry after a 429 backoff
            rows: list[dict] = []
            pagination_key: str | None = None
            ok = True

            while True:
                params: dict = {"code": code, "from": start.isoformat(), "to": end.isoformat()}
                if pagination_key:
                    params["pagination_key"] = pagination_key

                self._limiter.wait()
                try:
                    resp = httpx.get(
                        f"{_BASE_URL}/equities/bars/daily",
                        params=params,
                        headers=self._headers(),
                        timeout=30,
                    )
                except Exception as e:
                    logger.debug(f"J-Quants network error for {ticker}: {e}")
                    return None

                if resp.status_code == 429:
                    self._limiter.set_backoff()
                    ok = False
                    break  # break pagination loop → retry outer loop
                if resp.status_code in (401, 403):
                    logger.error(f"J-Quants API key rejected ({resp.status_code}): {resp.text[:200]}")
                    return None
                if not resp.is_success:
                    logger.debug(f"J-Quants HTTP {resp.status_code} for {ticker}")
                    return None

                body = resp.json()
                rows.extend(body.get("data", []))
                pagination_key = body.get("pagination_key") or None
                if not pagination_key:
                    break

            if ok:
                break  # success — no need to retry

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["Date"])
        return df.set_index("Date").sort_index()

    def fetch_ohlcv(
        self, tickers: list[str], start: date, end: date
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (adj_close_df, volume_df) — tickers as columns, dates as index."""
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

                # v2 column names: AdjC (adjusted close), AdjVo (adjusted volume)
                adj_col = "AdjC" if "AdjC" in df.columns else "C"
                adj_close_map[ticker] = df[adj_col].rename(ticker)

                vol_col = "AdjVo" if "AdjVo" in df.columns else "Vo"
                volume_map[ticker] = df[vol_col].rename(ticker)

        if not adj_close_map:
            return pd.DataFrame(), pd.DataFrame()

        return pd.DataFrame(adj_close_map), pd.DataFrame(volume_map)

    def fetch_ohlcv_full(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        """Return flat-column OHLCV DataFrame for holding_pipeline exit evaluation.

        Columns: "Adj Close", "High", "Low", "Volume" — matches holding_pipeline expectations.
        Only the first ticker is used (HOLDING always holds a single position).
        """
        if not tickers:
            return pd.DataFrame()
        df = self._fetch_daily_quotes(tickers[0], start, end)
        if df is None or df.empty:
            return pd.DataFrame()

        close_col = "AdjC" if "AdjC" in df.columns else "C"
        high_col = "AdjH" if "AdjH" in df.columns else "H"
        low_col = "AdjL" if "AdjL" in df.columns else "L"
        vol_col = "AdjVo" if "AdjVo" in df.columns else "Vo"

        cols = [c for c in [close_col, high_col, low_col, vol_col] if c in df.columns]
        return df[cols].rename(columns={
            close_col: "Adj Close",
            high_col: "High",
            low_col: "Low",
            vol_col: "Volume",
        })

    # ── Fundamentals ───────────────────────────────────────────────────────────

    def _fetch_latest_fins_summary(self, ticker: str) -> dict:
        """Return most-recently-disclosed fins/summary dict (or {})."""
        code = self._to_code(ticker)
        for attempt in range(2):  # one retry after a 429 backoff
            try:
                self._limiter.wait()
                resp = httpx.get(
                    f"{_BASE_URL}/fins/summary",
                    params={"code": code},
                    headers=self._headers(),
                    timeout=30,
                )
            except Exception as e:
                logger.debug(f"J-Quants fins/summary error for {ticker}: {e}")
                return {}

            if resp.status_code == 429:
                self._limiter.set_backoff()
                if attempt == 0:
                    continue
                return {}
            if not resp.is_success:
                return {}

            records: list[dict] = resp.json().get("data", [])
            if not records:
                return {}
            return max(records, key=lambda r: r.get("DiscDate", "") or "")

        return {}

    def fetch_fundamentals(self, tickers: list[str]) -> pd.DataFrame:
        """Derive market_cap, PBR, equity_ratio, avg_5d_trading_value from J-Quants v2."""
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._fetch_latest_fins_summary, t): t for t in tickers
            }
            stmt_map: dict[str, dict] = {
                futures[f]: f.result() for f in as_completed(futures)
            }

        records = []
        for ticker in tickers:
            stmt = stmt_map.get(ticker, {})
            cached = self._ohlcv_cache.get(ticker)

            # Liquidity: avg of last 5 days Va (trading value in yen, v2 field name)
            avg_5d_tv = 0.0
            if cached is not None and "Va" in cached.columns:
                try:
                    avg_5d_tv = float(pd.to_numeric(cached["Va"], errors="coerce").tail(5).mean() or 0)
                except (TypeError, ValueError):
                    pass

            # v2 fins/summary balance sheet fields (short-form keys)
            equity_ratio = float(stmt.get("EqAR", 0) or 0)
            bvps = float(stmt.get("BPS", 0) or 0)
            equity = float(stmt.get("Eq", 0) or 0)
            shares = float(stmt.get("ShOutFY", 0) or 0)
            # BPS is sometimes '' for companies that don't report it directly;
            # derive from balance sheet when missing.
            if bvps == 0.0 and equity > 0 and shares > 0:
                bvps = equity / shares

            # Last adjusted close from OHLCV cache
            last_close = 0.0
            if cached is not None and not cached.empty:
                close_col = "AdjC" if "AdjC" in cached.columns else "C"
                close_vals = cached[close_col].dropna()
                if not close_vals.empty:
                    try:
                        last_close = float(close_vals.iloc[-1])
                    except (TypeError, ValueError):
                        pass

            pbr = last_close / bvps if bvps > 0 and last_close > 0 else 0.0
            # prefer direct shares × price; fall back to pbr × equity derivation
            if shares > 0 and last_close > 0:
                market_cap = shares * last_close
            elif pbr > 0 and equity > 0:
                market_cap = pbr * equity
            else:
                market_cap = 0.0

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
        """Verify API key is valid using a known historical date (available on all plans)."""
        try:
            self._limiter.wait()
            resp = httpx.get(
                f"{_BASE_URL}/equities/bars/daily",
                params={"code": "72030", "from": "2024-01-04", "to": "2024-01-05"},
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                return True
            logger.error(
                f"J-Quants API key check failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )
            return False
        except Exception as e:
            logger.error(f"J-Quants connectivity check failed: {e}")
            return False
