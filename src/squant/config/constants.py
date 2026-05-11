from decimal import Decimal

# --- Universe filters ---
PRICE_MIN = Decimal("100")
PRICE_MAX = Decimal("900")
MARKET_CAP_MIN_JPY = 10_000_000_000        # ¥10 billion
LIQUIDITY_MIN_JPY = 100_000_000            # ¥100 million (5-day avg trading value)
PBR_MIN = 0.5
PBR_MAX = 1.2
EQUITY_RATIO_MIN = 0.30                    # 30%
EARNINGS_BLACKOUT_DAYS = 3                 # ±3 business days around earnings

# --- Signal parameters ---
MA_LONG = 75                               # long-term trend MA
MA_SHORT = 5                               # short-term MA for reversal
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 45.0
VOLATILITY_WINDOW = 20                     # 20-day std dev window
HISTORY_DAYS_REQUIRED = 90                 # need 90 calendar days (~75 trading days) of data

# --- Execution ---
GAP_UP_CANCEL_THRESHOLD = Decimal("0.02")  # cancel if open > prev_close * 1.02
SLIPPAGE_BUFFER = Decimal("0.02")          # 2% — same as gap-up threshold

# --- Exit rules ---
STOP_LOSS_RATE = Decimal("0.025")          # -2.5% from entry
ATR_TRAILING_MULTIPLIER = Decimal("1.5")   # 1.5× ATR trailing stop
TIME_STOP_TRADING_DAYS = 5                 # force exit after 5 trading days
TARGET_PROFIT_RATE = Decimal("0.07")       # +7.0% take-profit (informational)

# --- Risk management ---
CIRCUIT_BREAKER_LOSS_JPY = Decimal("30000")

# --- Capital ---
DEFAULT_BUDGET_JPY = Decimal("100000")

# --- Data validation ---
ANOMALY_PRICE_CHANGE_MAX = 0.30            # ±30% daily move → skip ticker
VOLUME_SPIKE_MAX_RATIO = 50.0              # > 50× 20-day median → skip
NAN_RATIO_MAX = 0.01                       # > 1% NaN → skip

# --- S-share (S株) specific ---
SSHARE_SPREAD_RATE = Decimal("0.005")          # 0.5% implicit spread (ask/bid) at SBI S株

# --- Execution guard ---
EXECUTION_GUARD_HOUR_JST = 20                  # only run at or after 20:00 JST

# --- Settlement ---
T2_SETTLEMENT_DAYS = 2

# --- Sheets tab names ---
SHEET_PORTFOLIO = "portfolio"
SHEET_TRADES = "trades"
SHEET_CIRCUIT_BREAKER = "circuit_breaker"
SHEET_RUN_LOG = "run_log"
SHEET_PENDING_SIGNALS = "pending_signals"
SHEET_RECENT_SALES = "recent_sales"
SHEET_SNAPSHOTS = "snapshots"
