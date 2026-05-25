from decimal import Decimal

# --- Universe filters (Phase 1) ---
PRICE_MIN = Decimal("100")
PRICE_MAX = Decimal("1000")                # Phase 1: 単元株100株×¥1,000 = ¥100,000 制約
MARKET_CAP_MIN_JPY = 3_000_000_000         # ¥3 billion
LIQUIDITY_MIN_JPY = 100_000_000            # ¥100 million (5-day avg trading value)
PBR_MIN = 0.5
PBR_MAX = 2.0
EQUITY_RATIO_MIN = 0.30                    # 30%
EARNINGS_BLACKOUT_DAYS = 3                 # ±3 business days around earnings

# --- Signal parameters (押し目モメンタム / Grid Search 2024-2025 ベスト採用) ---
# ※ 2026-05-23 にブレイクアウト追従型を試したが PF 0.72 と悪化したため、
#   Grid Search ベスト（押し目モメンタム）に戻して実運用フェーズへ進む。
MA_LONG = 75                               # long-term trend MA
RSI_PERIOD = 14                            # 中期RSI
RSI_BUY_LOWER = 35.0                       # 下限: これ未満は下落圧力強すぎ
RSI_BUY_UPPER = 60.0                       # 上限: grid search ベスト（50→60で月+0.40%最大化）
BREAKOUT_WINDOW = 20                       # （未使用・候補Aのアーカイブ用に保持）
VOLUME_SURGE_WINDOW = 20                   # 出来高サージ判定の平均期間
VOLUME_SURGE_MULTIPLIER = 1.2              # 当日出来高 > 20日平均 × 1.2
VOLATILITY_WINDOW = 20                     # 20-day std dev window
HISTORY_DAYS_REQUIRED = 90                 # need 90 calendar days (~75 trading days) of data

# --- Execution ---
GAP_UP_CANCEL_THRESHOLD = Decimal("0.02")  # cancel if open > prev_close * 1.02
SLIPPAGE_BUFFER = Decimal("0.02")          # 2% — same as gap-up threshold
SHARES_PER_UNIT = 100                      # 単元株（100株単位）

# --- Exit rules (Grid Search 2024-2025 ベスト採用) ---
STOP_LOSS_RATE = Decimal("0.025")          # -2.5% from entry (OCO逆指値で執行)
ATR_TRAILING_MULTIPLIER = Decimal("1.5")   # 1.5× ATR（grid searchで2.5→1.5。狭めの方が利益確保が早い）
ATR_PERIOD = 14
TIME_STOP_TRADING_DAYS = 5                 # 3/5/7で大差なし、5日が安定
TARGET_PROFIT_RATE = Decimal("0.06")       # +6.0% take-profit (単元株・SBIゼロ革命で手数料0)

# --- Risk management ---
CIRCUIT_BREAKER_LOSS_JPY = Decimal("30000")    # Phase 1: 投資資本×30%
CIRCUIT_BREAKER_LOSS_RATE = Decimal("0.30")    # Phase 2/3 は 0.15 に厳格化

# --- Capital ---
DEFAULT_BUDGET_JPY = Decimal("100000")

# --- Diversification (B phase, 2026-05-25) ---
# Phase 1 keeps 2 to preserve a usable universe under ¥100k / 100sh / 2 = ¥500 price cap.
# Phase 2 / Phase 3 widen to 3 once the per-stock budget can carry a ¥1,000+ price cap.
MAX_POSITIONS_PHASE_1 = 2
MAX_POSITIONS_PHASE_2_3 = 3
DEFAULT_MAX_POSITIONS = MAX_POSITIONS_PHASE_1

# --- Data validation ---
ANOMALY_PRICE_CHANGE_MAX = 0.30            # ±30% daily move → skip ticker
VOLUME_SPIKE_MAX_RATIO = 50.0              # > 50× 20-day median → skip
NAN_RATIO_MAX = 0.01                       # > 1% NaN → skip

# --- 単元株（単元株取引）---
# 旧S株のスプレッド設定は撤去。SBI証券ゼロ革命適用で国内株式手数料は0円、
# 単元株は板取引のため買値/売値の事実上のスプレッドも極小（流動性フィルタで担保）。
EXECUTION_SPREAD_RATE = Decimal("0.0")

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
