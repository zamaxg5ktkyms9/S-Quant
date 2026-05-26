"""Buy signal detection — 押し目モメンタム / Grid Search 2024-2025 ベスト採用.

戦略選定の経緯（2026-05-23）:
1. 初回（RSI(14) 35-50・ボラ収縮・出来高サージ） → 月-0.59%、PF 0.30
2. Grid Search 180通り → ベスト: RSI上限60、ATR×1.5 → 月+0.40%、PF 1.20
3. ブレイクアウト追従への転換実験 → 月-0.71%、PF 0.72（悪化）
4. → Grid Search ベスト（押し目モメンタム）に確定、実運用フェーズへ
"""

from datetime import date
from decimal import Decimal

import pandas as pd

from squant.config.constants import (
    MA_LONG,
    MA_MID,
    MA_SHORT,
    MA_TREND_LOOKBACK,
    MA_TREND_MIN_SLOPE,
    RSI_BUY_LOWER,
    RSI_BUY_UPPER,
    RSI_PERIOD,
    VOLATILITY_WINDOW,
    VOLUME_SURGE_MULTIPLIER,
    VOLUME_SURGE_WINDOW,
)
from squant.domain.indicators import rolling_std, rsi, sma, volume_surge_ratio
from squant.domain.models import Candidate


def detect_signals(
    filtered_tickers: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
) -> list[Candidate]:
    """4条件すべてを満たした銘柄を返す（押し目モメンタム）:
    ① 終値 > 75日MA（長期上昇トレンド）
    ② RSI_BUY_LOWER < RSI(14) < RSI_BUY_UPPER（押し目〜中立ゾーン）
    ③ 20日標準偏差 < 過去平均（ボラ収縮 = ブレイク前兆）
    ④ 当日出来高 > 20日平均 × VOLUME_SURGE_MULTIPLIER（実需を伴う反転）
    """
    candidates: list[Candidate] = []
    dropped = {
        "no_data": 0,
        "cond1_trend": 0,
        "cond2_rsi": 0,
        "cond3_volatility": 0,
        "cond4_volume": 0,
    }

    for ticker in filtered_tickers:
        if ticker not in ohlcv.columns:
            dropped["no_data"] += 1
            continue

        close = ohlcv[ticker].dropna()
        if len(close) < MA_LONG + 10:
            dropped["no_data"] += 1
            continue

        # ① トレンド: 終値 > 75日MA
        ma_long = sma(close, MA_LONG)
        if pd.isna(ma_long.iloc[-1]) or close.iloc[-1] <= ma_long.iloc[-1]:
            dropped["cond1_trend"] += 1
            continue

        # ② RSI(14) 押し目ゾーン
        rsi_series = rsi(close, RSI_PERIOD)
        last_rsi = rsi_series.iloc[-1]
        if pd.isna(last_rsi) or last_rsi <= RSI_BUY_LOWER or last_rsi >= RSI_BUY_UPPER:
            dropped["cond2_rsi"] += 1
            continue

        # ③ ボラ収縮
        std_series = rolling_std(close, VOLATILITY_WINDOW)
        last_std = std_series.iloc[-1]
        hist_mean_std = std_series.iloc[:-1].mean()
        if pd.isna(last_std) or pd.isna(hist_mean_std) or last_std > hist_mean_std:
            dropped["cond3_volatility"] += 1
            continue

        # ④ 出来高サージ: 当日出来高 > 20日平均 × 1.2
        vol_col = f"{ticker}_vol"
        if vol_col in ohlcv.columns:
            vol = ohlcv[vol_col]
        elif hasattr(ohlcv, "volume") and ticker in ohlcv.volume.columns:
            vol = ohlcv.volume[ticker]
        else:
            dropped["cond4_volume"] += 1
            continue

        vol_clean = vol.dropna()
        if len(vol_clean) < VOLUME_SURGE_WINDOW + 1:
            dropped["cond4_volume"] += 1
            continue
        vol_surge = volume_surge_ratio(vol_clean, window=VOLUME_SURGE_WINDOW)
        last_surge = vol_surge.iloc[-1]
        if pd.isna(last_surge) or float(last_surge) < VOLUME_SURGE_MULTIPLIER:
            dropped["cond4_volume"] += 1
            continue

        fund = fundamentals.loc[ticker] if ticker in fundamentals.index else None
        pbr = float(fund.get("pbr", 99.0)) if fund is not None else 99.0
        mcap = float(fund.get("market_cap_jpy", 0)) if fund is not None else 0.0

        candidates.append(
            Candidate(
                ticker=ticker,
                close=Decimal(str(round(close.iloc[-1], 1))),
                rsi14=float(last_rsi),
                volume_surge_ratio=float(last_surge),
                pbr=pbr,
                market_cap_jpy=mcap,
            )
        )

    import logging as _logging
    _logging.getLogger(__name__).info(
        f"Signal filter counts (dropped): "
        f"no_data={dropped['no_data']} "
        f"cond1_trend={dropped['cond1_trend']} "
        f"cond2_rsi={dropped['cond2_rsi']} "
        f"cond3_volatility={dropped['cond3_volatility']} "
        f"cond4_volume={dropped['cond4_volume']} "
        f"passed={len(candidates)}"
    )
    return candidates


def detect_signals_ma_cross(
    filtered_tickers: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
) -> list[Candidate]:
    """C フェーズ・候補B = MA クロス + トレンドフィルタ（トレンドフォロー型）.

    条件:
    ① 5日MA > 25日MA（短期が中期を上回る = 上昇基調確認）
    ② 25日MA(today) > 25日MA(today - MA_TREND_LOOKBACK)（中期トレンドが上向き）
    ③ 出来高サージ: 当日出来高 > 20日平均 × VOLUME_SURGE_MULTIPLIER（実需確認）

    押し目モメンタムからの差分:
    - RSI 押し目ゾーン制約を撤廃（「下がってる中で買う」ロジック削除）
    - 75日MA 上の条件を 25日MA 上向き条件に置換（短〜中期トレンドへ）
    - ボラ収縮条件は撤廃（上昇トレンド中はボラが拡大することもある）
    - 出来高サージは維持（ダマシ追従の抑制）
    """
    candidates: list[Candidate] = []
    dropped = {
        "no_data": 0,
        "cond1_cross": 0,
        "cond2_trend": 0,
        "cond3_volume": 0,
    }

    for ticker in filtered_tickers:
        if ticker not in ohlcv.columns:
            dropped["no_data"] += 1
            continue

        close = ohlcv[ticker].dropna()
        if len(close) < MA_MID + MA_TREND_LOOKBACK + 5:
            dropped["no_data"] += 1
            continue

        # ① 5日MA > 25日MA
        ma_short = sma(close, MA_SHORT)
        ma_mid = sma(close, MA_MID)
        if pd.isna(ma_short.iloc[-1]) or pd.isna(ma_mid.iloc[-1]):
            dropped["cond1_cross"] += 1
            continue
        if ma_short.iloc[-1] <= ma_mid.iloc[-1]:
            dropped["cond1_cross"] += 1
            continue

        # ② 25日MA 上向き: MA_MID(today) > MA_MID(today - LOOKBACK) + MIN_SLOPE
        if len(ma_mid.dropna()) < MA_TREND_LOOKBACK + 1:
            dropped["cond2_trend"] += 1
            continue
        ma_mid_today = ma_mid.iloc[-1]
        ma_mid_past = ma_mid.iloc[-1 - MA_TREND_LOOKBACK]
        if pd.isna(ma_mid_past) or (ma_mid_today - ma_mid_past) <= MA_TREND_MIN_SLOPE:
            dropped["cond2_trend"] += 1
            continue

        # ③ 出来高サージ
        vol_col = f"{ticker}_vol"
        if vol_col in ohlcv.columns:
            vol = ohlcv[vol_col]
        elif hasattr(ohlcv, "volume") and ticker in ohlcv.volume.columns:
            vol = ohlcv.volume[ticker]
        else:
            dropped["cond3_volume"] += 1
            continue

        vol_clean = vol.dropna()
        if len(vol_clean) < VOLUME_SURGE_WINDOW + 1:
            dropped["cond3_volume"] += 1
            continue
        vol_surge = volume_surge_ratio(vol_clean, window=VOLUME_SURGE_WINDOW)
        last_surge = vol_surge.iloc[-1]
        if pd.isna(last_surge) or float(last_surge) < VOLUME_SURGE_MULTIPLIER:
            dropped["cond3_volume"] += 1
            continue

        # ranking のため RSI も計算（ただしフィルタには使わない）
        rsi_series = rsi(close, RSI_PERIOD)
        last_rsi = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0

        fund = fundamentals.loc[ticker] if ticker in fundamentals.index else None
        pbr = float(fund.get("pbr", 99.0)) if fund is not None else 99.0
        mcap = float(fund.get("market_cap_jpy", 0)) if fund is not None else 0.0

        candidates.append(
            Candidate(
                ticker=ticker,
                close=Decimal(str(round(close.iloc[-1], 1))),
                rsi14=last_rsi,
                volume_surge_ratio=float(last_surge),
                pbr=pbr,
                market_cap_jpy=mcap,
            )
        )

    import logging as _logging
    _logging.getLogger(__name__).info(
        f"MA-cross signal counts (dropped): "
        f"no_data={dropped['no_data']} "
        f"cond1_cross={dropped['cond1_cross']} "
        f"cond2_trend={dropped['cond2_trend']} "
        f"cond3_volume={dropped['cond3_volume']} "
        f"passed={len(candidates)}"
    )
    return candidates
