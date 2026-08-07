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


def get_signal_func(strategy: str):
    """signal_strategy に対応する検出関数を返す。

    "ma_cross" | "pullback"（本番/旧）に加え、Plan B の探索用シグナルを dispatch する。
    本番 (Settings.signal_strategy) は "ma_cross" 固定のため、探索用シグナルの追加は
    ライブ挙動に影響しない（backtest.py の --signal からのみ到達する）。
    """
    if strategy == "ma_cross":
        return detect_signals_ma_cross
    if strategy == "reversal":
        return detect_signals_reversal
    if strategy == "value":
        return detect_signals_value
    if strategy == "high52":
        return detect_signals_high52
    return detect_signals  # default: pullback (A1 戦略、後方互換)


def with_volume_columns(adj_close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """価格と出来高を signal 関数が期待する単一フレーム（列 "<ticker>_vol"）に結合する。

    旧実装の per-column 代入は DataFrame 断片化の PerformanceWarning を毎ラン・
    毎バックテストで出していた（改善提案 B-2）。reindex は旧代入と同じ
    「価格側のインデックスに整列」の挙動を保つ。
    """
    vol = volume.reindex(adj_close.index)
    vol.columns = [f"{c}_vol" for c in vol.columns]
    return pd.concat([adj_close, vol], axis=1)


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


# Plan B 探索用しきい値（本番 constants には昇格しない・backtest --signal reversal 専用）
REVERSAL_RSI_MAX = 30.0  # RSI(14) ≤ この値 = 強い売られすぎ


def detect_signals_reversal(
    filtered_tickers: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
) -> list[Candidate]:
    """Plan B P1 — 短期リバーサル（逆張り）.

    仮説: 小型株の短期的な過剰反応（売られすぎ）は数日〜数週間で平均回帰する。
    失敗した pullback / ma_cross（モメンタム/トレンド追従）の鏡像。

    条件（ロングオンリー・翌日寄付エントリー）:
    ① RSI(14) ≤ REVERSAL_RSI_MAX（強い売られすぎ）

    ランキングは ranking.rank の rsi 昇順に委ねる（最も売られすぎを優先）。
    ボラ収縮・出来高サージ・トレンドフィルタは *あえて課さない*（純粋な逆張りの検証）。
    """
    candidates: list[Candidate] = []
    dropped = {"no_data": 0, "cond1_rsi": 0}

    for ticker in filtered_tickers:
        if ticker not in ohlcv.columns:
            dropped["no_data"] += 1
            continue

        close = ohlcv[ticker].dropna()
        if len(close) < RSI_PERIOD + 10:
            dropped["no_data"] += 1
            continue

        rsi_series = rsi(close, RSI_PERIOD)
        last_rsi = rsi_series.iloc[-1]
        if pd.isna(last_rsi) or float(last_rsi) > REVERSAL_RSI_MAX:
            dropped["cond1_rsi"] += 1
            continue

        # ランキング補助情報（フィルタには使わない）
        vol_col = f"{ticker}_vol"
        last_surge = 1.0
        if vol_col in ohlcv.columns:
            vol_clean = ohlcv[vol_col].dropna()
            if len(vol_clean) >= VOLUME_SURGE_WINDOW + 1:
                s = volume_surge_ratio(vol_clean, window=VOLUME_SURGE_WINDOW).iloc[-1]
                if not pd.isna(s):
                    last_surge = float(s)

        fund = fundamentals.loc[ticker] if ticker in fundamentals.index else None
        pbr = float(fund.get("pbr", 99.0)) if fund is not None else 99.0
        mcap = float(fund.get("market_cap_jpy", 0)) if fund is not None else 0.0

        candidates.append(
            Candidate(
                ticker=ticker,
                close=Decimal(str(round(close.iloc[-1], 1))),
                rsi14=float(last_rsi),
                volume_surge_ratio=last_surge,
                pbr=pbr,
                market_cap_jpy=mcap,
            )
        )

    import logging as _logging
    _logging.getLogger(__name__).info(
        f"Reversal signal counts (dropped): "
        f"no_data={dropped['no_data']} "
        f"cond1_rsi={dropped['cond1_rsi']} "
        f"passed={len(candidates)}"
    )
    return candidates


# Plan B 探索用しきい値
VALUE_PBR_MAX = 1.0        # PBR ≤ この値 = 割安
HIGH52_WINDOW = 252        # 52週高値の営業日数
HIGH52_PROXIMITY = 0.95    # 終値 ≥ この比率 × 52週高値 で「高値近接」


def detect_signals_value(
    filtered_tickers: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
) -> list[Candidate]:
    """Plan B P2 — バリュー傾斜（低PBR）.

    仮説: 日本のバリュープレミアム。PBR ∈ (0, VALUE_PBR_MAX] の割安銘柄を買う。
    注記: 本番出口(TS5)はバリューの発現期間と不整合。horizon 起因の敗北かを区別するため
    参考値として単発のみ実施。

    ランキング: 最割安（PBR 最小）を優先。既存 ranking.rank（rsi 昇順が主キー）に合わせ
    rsi14 = pbr*50 のプロキシで PBR 昇順＝rsi 昇順にマップ（出力 RSI は意味を持たない）。
    """
    candidates: list[Candidate] = []
    dropped = {"no_data": 0, "cond1_pbr": 0}

    for ticker in filtered_tickers:
        if ticker not in ohlcv.columns:
            dropped["no_data"] += 1
            continue
        close = ohlcv[ticker].dropna()
        if len(close) < 5:
            dropped["no_data"] += 1
            continue

        fund = fundamentals.loc[ticker] if ticker in fundamentals.index else None
        pbr = float(fund.get("pbr", 99.0)) if fund is not None else 99.0
        if pbr <= 0 or pbr > VALUE_PBR_MAX:
            dropped["cond1_pbr"] += 1
            continue
        mcap = float(fund.get("market_cap_jpy", 0)) if fund is not None else 0.0

        candidates.append(
            Candidate(
                ticker=ticker,
                close=Decimal(str(round(close.iloc[-1], 1))),
                rsi14=pbr * 50.0,  # ランキング用プロキシ（PBR 昇順 = 最割安優先）
                volume_surge_ratio=1.0,
                pbr=pbr,
                market_cap_jpy=mcap,
            )
        )

    import logging as _logging
    _logging.getLogger(__name__).info(
        f"Value signal counts (dropped): "
        f"no_data={dropped['no_data']} cond1_pbr={dropped['cond1_pbr']} "
        f"passed={len(candidates)}"
    )
    return candidates


def detect_signals_high52(
    filtered_tickers: list[str],
    ohlcv: pd.DataFrame,
    fundamentals: pd.DataFrame,
    as_of: date,
) -> list[Candidate]:
    """Plan B P4 — 52週高値近接（アンカリング・George & Hwang 2004）.

    仮説: 52週高値近接銘柄はアンカリングで過小評価され、ブレイク後に緩慢上昇する。

    条件: 終値 ≥ HIGH52_PROXIMITY × 直近 HIGH52_WINDOW 営業日高値。
    ランキング: 高値に近いほど優先。rsi14 = (1 - close/high52)*1000 のプロキシで
    近接度 昇順＝rsi 昇順にマップ（近いほど小さい値＝優先）。
    """
    candidates: list[Candidate] = []
    dropped = {"no_data": 0, "cond1_prox": 0}

    for ticker in filtered_tickers:
        if ticker not in ohlcv.columns:
            dropped["no_data"] += 1
            continue
        close = ohlcv[ticker].dropna()
        if len(close) < HIGH52_WINDOW:
            dropped["no_data"] += 1
            continue

        last_close = float(close.iloc[-1])
        high52 = float(close.iloc[-HIGH52_WINDOW:].max())
        if high52 <= 0 or last_close < HIGH52_PROXIMITY * high52:
            dropped["cond1_prox"] += 1
            continue

        fund = fundamentals.loc[ticker] if ticker in fundamentals.index else None
        pbr = float(fund.get("pbr", 99.0)) if fund is not None else 99.0
        mcap = float(fund.get("market_cap_jpy", 0)) if fund is not None else 0.0

        candidates.append(
            Candidate(
                ticker=ticker,
                close=Decimal(str(round(last_close, 1))),
                rsi14=(1.0 - last_close / high52) * 1000.0,  # 近接度プロキシ（近い=小）
                volume_surge_ratio=1.0,
                pbr=pbr,
                market_cap_jpy=mcap,
            )
        )

    import logging as _logging
    _logging.getLogger(__name__).info(
        f"High52 signal counts (dropped): "
        f"no_data={dropped['no_data']} cond1_prox={dropped['cond1_prox']} "
        f"passed={len(candidates)}"
    )
    return candidates
