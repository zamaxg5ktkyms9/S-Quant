# Backtest Report — S-Quant Pullback Momentum Strategy

| | |
|---|---|
| **Report Date** | 2026-05-23 |
| **Strategy Name** | Pullback Momentum (押し目モメンタム) |
| **Strategy Version** | v1.0 — Grid Search 2024-2025 Best |
| **Phase** | Phase 1 baseline (¥100,000 capital) |
| **Author** | Claude (Anthropic) |
| **Status** | ✅ Validated — Ready for live paper trading |

---

## 1. Executive Summary

S-Quant の押し目モメンタム戦略を J-Quants データ（2024-01-04 〜 2025-12-30、488営業日）で検証した結果、**フル期間バックテストでは期待値プラス（PF 1.20、月+0.40%）**を確認した。しかし **Walk-Forward Analysis（2026-05-24 追加実施）により、これは過剰最適化由来の可能性が高い**ことが判明した（Section 8.3 参照）。In-Sample (2024) 単独では PF 0.96・月-0.06%、Out-of-Sample (2025) では PF 0.77・月-0.21% で、構造的優位性は確認できていない。

### Headline Numbers

| 指標 | 値 | 業界基準 / コメント |
|---|---|---|
| Total Return (2y) | **+9.53%** | プラス |
| CAGR | **+4.68%** | 株式インデックス並み |
| Monthly Return (avg) | +0.40% | 目標 +1.5%〜+2.0% に未達 |
| Sharpe Ratio | 0.47 | < 1.0、低い |
| Sortino Ratio | 0.90 | < 1.5、低い |
| Calmar Ratio | 0.28 | < 1.0、低い |
| Max Drawdown | **-16.76%** | サーキットブレーカー閾値（30%）の範囲内 |
| Profit Factor | **1.20** | > 1.0、期待値プラスの最低条件は満たす |
| Win Rate | 44.9% | 損益分岐勝率 29% を上回る |
| Trades | 49 (2.05/月) | 目標 月3〜6件のやや下回り |

### Recommendation（2026-05-24 更新）

Walk-Forward 結果を踏まえ、**Phase 1 paper trading 開始判断は再評価が必要**。選択肢:

1. **保守案（推奨）**: paper trading を一旦保留し、戦略本体の見直しに進む（シグナル条件再設計、ユニバース拡張、または別アプローチ検証）
2. **割り切り案**: 月-0.1〜-0.3%の実損を覚悟して2ヶ月だけ実運用 → 実測データを取得 → その上で戦略停止 or 改修判断
3. **検証延長案**: paper trading に進む前に、より長い期間（2020年以降など）でバックテストとWalk-Forwardを再実施

最終判断はユーザー判断。判断材料は Section 8.3〜8.5 と Section 11 を参照。

---

## 2. Strategy Description

### 2.1 Concept

長期的な上昇トレンドにある銘柄が、一時的な押し目（短期下落 or 横ばい）をつけた後の反発初動を取りに行く、**短期スイング戦略**である。エントリーから最大5営業日で必ず手仕舞いし、含み損の長期化を防ぐ。

### 2.2 Signal Conditions（4条件 AND）

| # | 条件 | 設定値 | 意図 |
|---|---|---|---|
| 1 | 終値 > 75日移動平均 | — | 長期上昇トレンドの確認 |
| 2 | 35 < RSI(14) < 60 | Grid Search 最適化 | 押し目〜中立ゾーンを捕捉 |
| 3 | 20日標準偏差 < 過去平均 | — | ボラ収縮 = ブレイク前兆 |
| 4 | 当日出来高 > 20日平均 × 1.2 | — | 実需を伴う反転確認 |

### 2.3 Position Sizing

- 1ポジション集中。投資資本の100%を1銘柄に投下
- 単元株（100株単位）で `floor(min(cash, budget) / (prev_close × 1.02) / 100) × 100` 株
- ¥100,000 / 株価¥800 → 100株 = ¥80,000 投下、¥20,000 は遊休

### 2.4 Exit Rules（優先順）

| # | Rule | Trigger | Execution |
|---|---|---|---|
| 1 | Hard Stop Loss | 株価 ≤ entry × (1-0.025) | OCO逆指値（ザラ場自動執行） |
| 2 | Trailing Stop | 株価 ≤ max(stop, 直近高値 - 1.5×ATR(14)) | システム計算 → 手動でSBI逆指値更新 |
| 3 | Take Profit | 株価 ≥ entry × 1.06 | OCO指値（ザラ場自動執行） |
| 4 | Time Stop | 保有5営業日経過 | システム通知 → 手動成行売却 |

> **保守的優先順位**: ザラ場で同日に Hard Stop と Take Profit が両方発動した場合、約定順序は OHLC からは復元できないため、損失側を優先計上する（期待値の過大評価を防ぐ）。

---

## 3. Universe & Data

| 項目 | 内容 |
|---|---|
| Universe Size | 282 銘柄（東証上場、株価 ¥100〜¥900 帯） |
| Universe Source | `data/universe.csv`（手動メンテ、四半期再スクリーニング想定） |
| Universe Filter | 時価総額 ¥30億+、流動性 5日平均売買代金 ¥1億+、PBR 0.5〜2.0、自己資本比率 30%+、決算ブラックアウト ±3営業日 |
| Data Source | J-Quants Light Plan v2（JPX公式市場データ） |
| Price Data | OHLCV（調整済み AdjC/AdjH/AdjL/AdjO/AdjVo） |
| Fundamentals | PBR（バックテスト各日で再計算）、市場時価総額、自己資本比率、5日平均売買代金 |
| Earnings Calendar | `data/earnings_calendar.csv`（決算前後3営業日除外） |

### Data Caveats

- **Survivorship Bias**: 上場廃止銘柄は universe に含まれない。ユニバースは2026-05-10時点のスナップショット。
- **Fundamentals Snapshot**: PBR以外の財務指標（時価総額・自己資本比率）はバックテスト全期間で最新値固定。期間内の変動は反映されない。
- **PBR Backfill**: 最新 PBR と最新終値から BPS を逆算し、バックテスト各日の終値で PBR を再計算。BPS自体は期中固定（年次変動は無視）。

---

## 4. Methodology

### 4.1 Backtest Engine

`scripts/backtest.py` で実装。以下のイベントループを毎営業日繰り返す:

1. **保有中ポジションの出口判定** (intraday OCO simulation)
2. **前日シグナルの翌営業日始値約定判定** (entry execution)
3. **引け後シグナルスキャン** (next-day candidate generation)

### 4.2 Execution Assumptions

| 項目 | 仮定 |
|---|---|
| エントリー価格 | シグナル日翌営業日の始値（Open） |
| ギャップアップ判定 | 始値 > 前日終値 × 1.02 → エントリー見送り |
| 損切り執行 | 当日 Low ≤ stop_loss_price → stop_loss_price で約定（OCO逆指値の擬似） |
| 利確執行 | 当日 High ≥ take_profit_price → take_profit_price で約定（OCO指値の擬似） |
| トレーリングストップ | 直近高値 - 1.5×ATR(14)、ラチェット式（下方へは動かない） |
| タイムストップ | 5営業日経過時、当日終値で約定 |
| スプレッド | 0.0%（SBI証券ゼロ革命適用、単元株は手数料0円） |
| スリッページ | 0%（保守的に逆指値/指値ちょうどで約定と仮定） |
| 借入 | なし（現金100%、レバレッジなし） |

### 4.3 Look-Ahead Bias Mitigation

- シグナル検出は当日終値で行うが、エントリーは**翌営業日始値**でしか発生しない
- ファンダメンタル PBR は当日終値から再計算するが、BPS（最新値由来）の前提が将来情報を含む可能性あり → 限定的だが完全には排除できない
- 当日 OHLC は当日中はリアル取引では未確定だが、バックテストでは確定後の値を用いる（出口判定）

### 4.4 Parameter Optimization

Grid Search を以下のレンジで実施し、`monthly_pnl_pct` 降順で評価:

| パラメータ | レンジ | ベスト値 |
|---|---|---|
| target_profit | 0.02 〜 0.06 (5水準) | **0.06** |
| atr_mult | 1.5 〜 2.5 (3水準) | **1.5** |
| rsi_upper | 45 〜 60 (4水準) | **60** |
| time_stop | 3 〜 7 営業日 (3水準) | **5** |

180通り、4並列・約29分。詳細は `.backtest_cache/grid_search_results.json` 参照。

> **過剰最適化への注意**: 4パラメータ × 180通りは小さくないため、in-sample fit のリスクあり。Out-of-sample（実運用）検証が必須。

---

## 5. Performance Summary

### 5.1 Return & Risk Metrics

```
Period:           2024-01-04 〜 2025-12-30 (488 trading days, 1.99 years)
Initial Capital:  ¥100,000
Final Equity:     ¥109,527

═══════════════════════════════════════════════════════
RETURN
─────────────────────────────────────────────────
  Total Return         +9.53%      ¥+9,527
  CAGR                 +4.68%
  Monthly Avg          +0.40%      ¥+399/月
  Best Trade           +¥5,517     (+6.0%, TAKE_PROFIT)
  Worst Trade          -¥2,411     (-2.5%, STOP_LOSS)

RISK
─────────────────────────────────────────────────
  Max Drawdown         -16.76%     ¥-16,764
  Max DD Duration      41 trades
  Max Cons. Losses     7

RISK-ADJUSTED RETURN
─────────────────────────────────────────────────
  Sharpe Ratio         0.47        (trade-based, annualized)
  Sortino Ratio        0.90
  Calmar Ratio         0.28        (CAGR / |MaxDD%|)
  Profit Factor        1.20        (Σwin / Σ|loss|)

TRADE QUALITY
─────────────────────────────────────────────────
  Trades               49          (2.05/月、24.65/年)
  Win Rate             44.9%       (22勝27敗)
  Expectancy           +¥194/trade (+0.31%/trade)
  Avg Win              +¥2,573
  Avg Loss             -¥1,744
  Win/Loss Ratio       1.48
  Avg Holding          3.71 days
═══════════════════════════════════════════════════════
```

### 5.2 Industry Benchmark Comparison

| Metric | This Strategy | "Good" Threshold | Verdict |
|---|---|---|---|
| Sharpe | 0.47 | > 1.0 | ⚠ 低い |
| Sortino | 0.90 | > 1.5 | ⚠ 低い |
| Calmar | 0.28 | > 1.0 | ⚠ 低い |
| Profit Factor | 1.20 | > 1.5 | ⚠ marginal |
| Win Rate | 44.9% | — | ✓ R:R次第で妥当 |
| Max DD | -16.8% | < -30% | ✓ 許容範囲 |
| Trades/year | 24.65 | > 30 | ⚠ やや少ない |

> リスク調整後リターン指標はいずれも「institutional grade」（Sharpe > 1.0）に達していない。**Retail systematic strategy としては機能するが、プロ運用基準は満たさない**。

---

## 6. Trade Analysis

### 6.1 Exit Reason Distribution

| Exit Reason | Count | Win Rate | Avg P&L | Total Contribution |
|---|---|---|---|---|
| STOP_LOSS | 19 (38.8%) | 0% | -¥1,999 | -¥37,981 |
| TAKE_PROFIT | 7 (14.3%) | 100% | +¥4,358 | +¥30,506 |
| TRAILING_STOP | 13 (26.5%) | 53.8% | +¥621 | +¥8,073 |
| TIME_STOP | 10 (20.4%) | 80% | +¥892 | +¥8,920 |

**Key Insight**: TAKE_PROFIT は件数こそ少ない (14%) が、勝ち分の主要因（+¥30,506 = 全利益の 79%）。TIME_STOP の勝率 80% は微益決済が中心で、PFへの寄与は限定的。

### 6.2 Holding Period Distribution

| Holding | Count | Win Rate | Avg P&L | Comment |
|---|---|---|---|---|
| 1 day | 18 (36.7%) | 27.8% | -¥106 | 翌日ストップアウトの巣窟 |
| 2 days | 5 | 20.0% | -¥832 | 同上 |
| 3 days | 3 | 33.3% | -¥985 | |
| 4 days | 4 | 50.0% | +¥249 | break-even |
| 5 days | 2 | 100% | +¥2,883 | TAKE_PROFIT 帯 |
| 6 days | 5 | 40.0% | +¥250 | |
| 7 days | 9 (18.4%) | 66.7% | +¥347 | TIME_STOP 帯 |
| 8 days | 2 | 100% | +¥1,850 | |
| 10 days | 1 | 100% | +¥3,720 | TRAILING_STOP の longest |

**Key Insight**: 保有1日トレードが全体の37% を占め、勝率が極端に低い。**「エントリー直後の逆行に脆い」**ことが構造的弱点。ATR 1.5 のトレーリングはこの問題を緩和したが完全には解消していない。

### 6.3 Consecutive Wins / Losses

- Max Consecutive Wins: **4**
- Max Consecutive Losses: **7** ← サーキットブレーカー（累積-¥30,000）まであと数件の余地

7連敗時の累積損失: 約 -¥14,000（最大DD相当）。**サーキットブレーカー閾値（-¥30,000）には到達せず、現リスク上限内で吸収可能**。

---

## 7. Risk Analysis

### 7.1 Drawdown Profile

| 指標 | 値 |
|---|---|
| Max Drawdown (絶対) | -¥16,764 |
| Max Drawdown (%) | -16.76% |
| Max DD Duration | 41 trades（最大DD底からpeak回復まで） |
| サーキットブレーカー閾値 | -¥30,000（-30%） |
| 余裕度 | 残り -¥13,236（DD余地） |

### 7.2 Tail Risk (構造的・バックテストでは部分的にしか測れない)

| Risk | Mitigation | Backtest覆い |
|---|---|---|
| ザラ場暴落 (-10%超急落) | OCO逆指値で-2.5%自動執行 | ✓ 反映 |
| 寄りギャップダウン | エントリー始値確認 / 始値時点で逆指値圏内なら見送り | △ 部分（始値約定モデル） |
| 流動性枯渇時の逆指値スリッページ | 5日平均売買代金 ¥1億以上の流動性フィルタ | × バックテスト未反映 |
| ブラックスワン（暴落・東証停止） | サーキットブレーカー (累積-30%) | △ 件数依存 |

### 7.3 Phase Scaling Risk

予算拡大時のリスク絶対額（線形拡大仮定）:

| Phase | 予算 | 想定最大DD | サーキットブレーカー | 余裕度 |
|---|---|---|---|---|
| 1 (検証中) | ¥100,000 | ¥-16,764 (-16.8%) | ¥-30,000 (30%) | ¥13,236 |
| 2 | ¥300,000 | ¥-50,290 (-16.8%) | ¥-45,000 (15%) | **-¥5,290 ⚠** |
| 3 (本運用) | ¥1,000,000 | ¥-167,635 (-16.8%) | ¥-150,000 (15%) | **-¥17,635 ⚠** |

> ⚠ **Phase 2/3 ではサーキットブレーカー（15%）を最大DDが超える**。Phase 2 移行前に「最大DDを-12%以内に抑える」改善（ポジションサイジング動的化、複数銘柄分散等）が必要。

---

## 8. Parameter Sensitivity (Grid Search)

### 8.1 Top 5 Configurations

| Rank | TP | ATR | RSI_upper | TS | Trades | Win% | Monthly% | Total | PF | MaxDD% |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | +6% | 1.5 | 60 | 5 | 49 | 44.9% | +0.40% | +¥9,527 | 1.20 | -16.8% |
| 2 | +6% | 1.5 | 60 | 7 | 48 | 43.8% | +0.33% | +¥7,853 | 1.16 | -16.3% |
| 3 | +6% | 1.5 | 60 | 3 | 53 | 39.6% | +0.30% | +¥7,247 | 1.14 | -17.4% |
| 4 | +6% | 2.0 | 60 | 3 | 51 | 39.2% | +0.27% | +¥6,400 | 1.13 | -15.2% |
| 5 | +6% | 2.0 | 60 | 7 | 45 | 42.2% | +0.23% | +¥5,592 | 1.12 | -18.5% |

### 8.2 Parameter Robustness

| Parameter | Insight |
|---|---|
| **rsi_upper** | Top 14件すべて 60。50→60 拡張がシグナル不足解消に決定的 |
| **target_profit** | Top 4件中3件が +6%。下げると微益決済増えてPF低下 |
| **atr_mult** | 1.5 がベスト。2.5 は広すぎて利益逃す |
| **time_stop** | 3/5/7 に大差なし。**ロバスト**（過剰最適化の懸念小） |

### 8.3 Out-of-Sample Robustness — Walk-Forward Analysis 実施結果（2026-05-24）

過剰最適化を定量検証するため、Walk-Forward Analysis を実施した。

**手法**:
- In-Sample (IS): 2024-01-04 〜 2024-12-30 で Grid Search 180通り → ベストパラメータ抽出
- Out-of-Sample (OOS): 2025-01-06 〜 2025-12-30 で IS ベストパラメータを固定して検証

**結果**:

| Metric | In-Sample (2024) | Out-of-Sample (2025) | 劣化率 |
|---|---|---|---|
| Best Params | TP=0.06, ATR=2.5, RSI≤55, TS=3 | (固定) | — |
| Trades | 15 | 15 | — |
| Win Rate | 40.0% | 46.7% | +17% |
| Monthly Return | **-0.06%** | **-0.21%** | -234% |
| Total P&L | -¥742 | -¥2,455 | — |
| Profit Factor | **0.96** | **0.77** | -20% |
| Sharpe | -0.17 | -0.25 | -52% |
| Max DD | -6.5% | -6.9% | わずか悪化 |

**Verdict**: ❌ **Overfitted（明確な過剰最適化）**

### 8.4 Walk-Forward から得た重要な含意

1. **IS (2024) 単独ではそもそも勝てていない**: PF 0.96、月-0.06% は break-even 未満
2. **IS のベストパラメータがフル期間ベストと大きく違う**:
   - フル期間 (2024+2025) ベスト: `RSI上限=60, ATR×1.5, TS=5`
   - IS (2024) 単独ベスト: `RSI上限=55, ATR×2.5, TS=3`
   - **時期によって最適パラメータが大きく揺れる = 構造的優位性ではなくデータフィッティング**
3. **フル期間で+9.5%だったのは「2025年単独の貢献」**: 2024年は実質損失、2025年が稼いだ偏り

### 8.5 戦略採用判断への影響

Walk-Forward 結果を踏まえ、当初の「Phase 1 paper trading に進む」判断は **慎重に再評価が必要**:

- フル期間バックテストの月+0.40%は **過剰最適化由来の可能性が高い**
- 実運用では月平均 -0.1% 〜 -0.3% の損失を覚悟する必要がある（OOS の実測値ベース）
- ただし、**最大DDは IS/OOS とも -7% 程度で安定**しており、リスク管理面では機能している

### 8.6 結果の保存

詳細メトリクスは [docs/backtests/walkforward_2024-01-04_2025-12-30.json](backtests/walkforward_2024-01-04_2025-12-30.json) に保存。

### 8.7 Rolling 3-Window Walk-Forward 拡張試行（2026-05-25・部分実行で中断）

#### 動機

8.3 の単窓 Walk-Forward (IS=2024/OOS=2025) で過剰最適化を検出したが、「時期依存（2025年が特殊だっただけ）」か「構造的過剰最適化」かが切り分けられていなかった。Rolling 3窓へ拡張して再評価する方針。

#### データ範囲の制約

当初 6年（2020-2025）を狙ったが、J-Quants Light Plan の実測で **遡及は約4年（2022年以降）に制限**されることが判明。コロナショック・利上げ初期は取得不可。

#### 採用設計

- **データ**: 2022-01-04 〜 2025-12-30（4年・OHLCV 231 有効銘柄）
- **窓構成**: rolling 3窓 IS=1年 / OOS=1年（α案）
  - W1: IS=2022 → OOS=2023
  - W2: IS=2023 → OOS=2024
  - W3: IS=2024 → OOS=2025
- **ロバスト判定基準**: OOS PF ≥ 1.0 かつ OOS 月リターン ≥ +0.1%

#### 実行結果（部分・W1のみ完了）

| Metric | W1 IS (2022) | W1 OOS (2023) |
|---|---|---|
| Best Params | TP=0.06, ATR=2.5, RSI≤60, TS=7 | (固定) |
| Trades | 27 | 21 |
| Monthly Return | +0.79% | **-1.27%** |
| Profit Factor | 1.33 | **0.43** |
| Max DD | -9.9% | -15.1% |
| **Robust Verdict** | — | **❌ FAIL** |

#### 中断理由

W2 IS Grid Search で `grid_search.py` の `subprocess.run(timeout=120)` に多数の組み合わせがヒットして N/A 化（W1-IS は 78分かかった）。
180 combos 中で完了したものから「Best」を選ぶことになり判定の信頼性が下がるため、残り W2/W3 (推定3時間) を待たず中断。

#### 結論（単窓・W1 を合わせた 2/2 窓で OOS FAIL）

| Window | IS Period | OOS Period | OOS Monthly | OOS PF | Robust |
|---|---|---|---|---|---|
| 8.3 単窓 | 2024 | 2025 | -0.21% | 0.77 | ❌ |
| 8.7 W1 | 2022 | 2023 | -1.27% | 0.43 | ❌ |

**2/2 窓で連続 OOS FAIL** → **押し目モメンタム＋単一銘柄集中は構造的に過剰最適化**と判定。残り W2/W3 を完了させても結論は変わらない見込み。

#### Backtest engine の構造的ボトルネック（次フェーズで要解消）

`backtest.py` のメインループに O(N) スケールしない遅さがあり、フル期間4年で4時間超、一部 1年期間でも 120秒超。`subprocess` 単位の起動オーバーヘッドと、毎営業日の `adj_close_full.loc[:str(today)]` スライスコピーが疑い。3銘柄分散実装時に同時に整理する。

#### 次フェーズ

- **B フェーズ**: 段階拡大型分散実装（Phase 1=2銘柄、Phase 2/3=3銘柄）
- 目標を年¥240k → **年¥100k に下方修正**（NotebookLM レビュー指摘の現実路線化）
- 段階拡大型分散版で再度 Rolling 3窓 WF を実施（B-WF）

### 8.8 B フェーズ — 段階拡大型分散の設計案（2026-05-25・実装前）

#### 動機（NotebookLM レビュー + A1 結果）

- A1: 単一銘柄戦略は 2/2 窓 OOS FAIL → 戦略撤退
- NotebookLM 指摘: 「1つのカゴに卵を全部盛る」設計は Phase 2/3 でサーキットブレーカー超過リスク大
- ¥100,000 × 3銘柄 = ¥333/株上限 → ユニバース激減のため Phase 1 のみ 2銘柄に絞る段階拡大方式に決定

#### 設計サマリ

| 項目 | 仕様 |
|---|---|
| 同時保有 | Phase 1: 2銘柄、Phase 2/3: 3銘柄 |
| 1銘柄予算 | **動的方式**: 残キャッシュ / 残空きスロット数 |
| 同日複数シグナル | 空きスロット数まで同日同時エントリー可（ランキング上位順） |
| 保有銘柄重複 | スクリーナー段階で `held_tickers` 集合を引数に取り除外 |
| 各銘柄出口 | 完全独立（OCO・トレーリング・タイムストップは Position 単位） |
| サーキットブレーカー | ポートフォリオ全体の累積実現損失で判定（個別銘柄ではない） |
| パラメータ | A1 の Grid Search ベスト値を**そのまま流用**（RSI 35-60、TP +6%、ATR×1.5、TS 5日）|

#### 実装範囲

- **ドメイン層**: `models.Position` をリスト化、`screener.apply_fundamental_filters` の `held_tickers` 引数を活用、`ranking.rank` の `top_n` パラメータ化、`position_manager.evaluate_exit` を Position 単位の独立呼び出しに整理
- **インフラ層**: `backtest.py` のメインループ高速化（既知ボトルネック）と複数 Position 対応、`daily_runner` と各 pipeline の複数 Position 対応、`sheets_repository` の portfolio タブを複数行スキーマに、`slack_formatter` で複数銘柄サマリ
- **設定層**: `constants.MAX_POSITIONS_PHASE_1 = 2`、`MAX_POSITIONS_PHASE_2_3 = 3` の追加、`settings.max_positions` の Phase に応じた解決

#### B-WF 計画

- データキャッシュ: A1 で生成した `data_2021-07-08_2025-12-30.pkl` を流用
- 窓構成: A1 と同じ rolling 3窓（IS=2022→OOS=2023, IS=2023→OOS=2024, IS=2024→OOS=2025）
- 判定基準: A1 と同じ（OOS PF ≥ 1.0 かつ OOS 月リターン ≥ +0.1%、過半数窓で達成ならロバスト）
- 期待される判断材料:
  - 分散効果のみで月リターンが OOS でプラス化するか
  - 分散効果で DD が縮小するか（-16.8% → -10% 台への改善が目標）
  - それでも届かない場合はシグナル本質見直し（候補B: 5×25日 MA クロス）に進む

### 8.9 B-WF — 部分実行結果と恒久対策の動作確認（2026-05-26〜27）

#### 実装内容

- `backtest.py` を複数 Position 対応に改修（`BacktestState.positions: list`、動的予算、同日複数エントリー、保有銘柄重複排除、各 Position 独立出口）
- `screener.exclude_held_positions` 追加
- `constants.MAX_POSITIONS_PHASE_1=2 / PHASE_2_3=3` 追加
- 既存 170 テスト＋screener 新 6 テスト = 176 PASS 維持

#### Phase 1 ¥100k 制約の再評価と予算修正

設計通り `¥100,000 / 2銘柄 / 100株 = ¥333/株上限` ではユニバースが激減し、スモークテストで 1年取引数が 10件（A1 単一銘柄の 23 件から半減）。Grid Search の 180 パターン中ほとんどが trades=0 となり、評価不能に。
→ **Phase 1 予算を ¥200,000 に増額** することで、A1 と同じ ¥100-¥1,000 価格帯を維持しつつ 2銘柄分散を実現する設計変更。

#### B-WF 1回目（workers=4、abort）

`workers=4` で起動したが A1 と同じ症状（pickle I/O 競合による subprocess timeout 多発）が再発し、9時間で 30% しか完了せず中断。

ただし W1 部分結果は得られた：

| Window | IS Period | OOS Period | IS Monthly | OOS Monthly | OOS PF | OOS DD | Robust |
|---|---|---|---|---|---|---|---|
| B W1 | 2022 | 2023 | +1.51% | -0.66% | 0.61 | -8.9% | ❌ |

#### B-WF 2回目（恒久対策後、ETA 自動 abort で正常停止）

恒久対策（`docs/operational_notes.md`、`walk_forward.py` の workers=1 / timeout=60s デフォルト化、`--benchmark`、`--max-wall-clock-seconds`、ETA 自動 abort）導入後に再実行。

- ベンチ: 10 combos × 2024年 = 84秒（8.4s/combo）、推定 1窓 25分、3窓 76分
- 本実行 W1 IS (2022): 18/180 で 56分経過 → ETA が初期推定の **22.2倍** に膨張
- `ETAInflationAbort` 発動で **56分で正常停止**（対策前なら 9時間放置になっていた）

期間ごとの subprocess 実行時間に大きな差があり（2024年=8s/combo、2022年=187s/combo）、シリアル実行でも本実行は完走困難。**Phase 1 ¥200k 2銘柄分散の完全 3窓検証には backtest.py のライブラリ化（subprocess を廃して in-process 化）が必要**と判明。

#### A1 vs B W1 比較（OOS 2023）

| Metric | A1 W1 単一銘柄 | B W1 2銘柄分散 ¥200k | 評価 |
|---|---|---|---|
| OOS Trades | 21 | **33** | 分散効果あり (+57%) |
| OOS Monthly | -1.27% | **-0.66%** | 改善（依然マイナス） |
| OOS PF | 0.43 | **0.61** | 改善（依然 <1.0） |
| OOS Max DD | -15.1% | **-8.9%** | 約半減 |
| Robust | ❌ | ❌ | 両者 FAIL |

#### 結論

- **分散効果は定量的に確認**: trades +57%、DD 半減、PF 0.43→0.61
- **ただし OOS 月リターンはマイナスのまま**: PF 0.61 / monthly -0.66% で robust 基準未達
- **押し目モメンタムは構造的に限界**: 単一銘柄でも 2銘柄分散でも、シグナル選定ロジック（RSI 35-60 押し目）自体が 2022-2023 のレジームでは負けている
- **恒久対策は機能**: ETA 自動 abort により対策前の 9時間放置が 56分で打ち切られた（コミット `2e64e00`）

#### 次フェーズ

- **C フェーズ**: シグナル本質見直し（NotebookLM 改善案1・候補B = 5×25日 MA クロス、トレンドフォロー）
- B-WF 完走は `backtest.py` のライブラリ化（subprocess を廃して in-process 化）と合わせて将来検討

#### 結果ファイル

- `docs/backtests/walkforward_rolling3_B_2stocks.json`（1回目部分、W1 のみ）
- B-WF v2 は abort のため JSON 出力なし（ログのみ）

### 8.10 C フェーズ — シグナル本質見直し (5×25日 MA クロス) と C-WF (2026-05-27)

#### 動機

A1 (単一銘柄) と B (2銘柄分散) の両方で OOS マイナスが確定 → **シグナル選定ロジック自体に構造的問題** という結論。NotebookLM 改善案1 = 「下がってる中で買う」（押し目モメンタム）から「上がり始めた確認後に買う」（トレンドフォロー）への転換。

#### シグナル定義 (C-3: クロス + トレンドフィルタ)

| 条件 | 内容 | 押し目モメンタムからの差分 |
|---|---|---|
| ① 5日MA > 25日MA | 短期が中期を上回る（上昇基調確認） | 75日MA超の条件を 25日MA上向きに置換 |
| ② 25日MA(today) > 25日MA(today - 20) | 中期トレンド上向き | 新規（ノイズ除去） |
| ③ 当日出来高 > 20日平均 × 1.2 | 実需確認 | 維持（ダマシ追従抑制） |
| 旧 RSI 押し目ゾーン (35-60) | — | **撤廃**（「下がってる中で買う」削除） |
| 旧 ボラ収縮 | — | **撤廃**（上昇トレンド中はボラ拡大あり） |

#### 実装内容

- `constants.py`: `MA_SHORT=5`, `MA_MID=25`, `MA_TREND_LOOKBACK=20`, `MA_TREND_MIN_SLOPE=0.0`
- `signal_engine.detect_signals_ma_cross` 新規追加（既存 `detect_signals` は維持、切替可能）
- `backtest.py` に `--signal {pullback, ma_cross}` フラグ
- `grid_search.py` / `walk_forward.py` に `--signal` 引数を伝播
- テスト 8件 追加、**全 184 件 PASS** 維持

#### スモークテスト (2024 単独 IS)

| Metric | A1 2024 | B 2024 ¥200k | **C 2024 MA クロス ¥200k** |
|---|---|---|---|
| Trades | 23 | 31 | **55** |
| Win Rate | 30% | 32% | **38.2%** |
| Monthly P&L | -1% | -0.72% | **+0.87%** ✅ |
| Profit Factor | 0.60 | 0.58 | **1.33** ✅ |
| Cumulative | -12% | -8.6% | **+10.4%** ✅ |
| Max DD | -16.8% | -11.9% | -10.8% |

→ IS 単独では全指標で改善。OOS で同じ動きをするかは C-WF で検証。

#### C-WF 単窓 結果 (IS=2022 / OOS=2023)

```bash
python scripts/walk_forward.py --single --signal ma_cross --workers 1 \
  --budget 200000 --max-positions 2 \
  --is-start 2022-01-04 --is-end 2022-12-30 \
  --oos-start 2023-01-04 --oos-end 2023-12-29
```

| Metric | IS (2022) | OOS (2023) |
|---|---|---|
| Best Params | TP=0.05, ATR=2.5, RSI≤45 (unused), TS=5 | (固定) |
| Trades | 68 | **74** |
| Win Rate | 36.8% | 40.5% |
| Monthly P&L | +0.40% | **-0.54%** |
| Profit Factor | 1.12 | **0.85** |
| Max DD | -14.3% | -12.5% |
| Sharpe | -0.14 | -0.21 |

**Robust 判定: ❌ FAIL**（PF 0.85 < 1.0、monthly -0.54% < +0.1%）

#### 三戦略比較 (OOS=2023 で公正比較)

| 戦略 | OOS Trades | OOS Monthly | OOS PF | OOS Max DD | Robust |
|---|---|---|---|---|---|
| A1 単一銘柄 (押し目) | 21 | -1.27% | 0.43 | -15.1% | ❌ |
| B 2銘柄分散 (押し目, ¥200k) | 33 | -0.66% | 0.61 | -8.9% | ❌ |
| **C 2銘柄分散 (MA クロス, ¥200k)** | **74** | **-0.54%** | **0.85** | -12.5% | ❌ |

**改善のトレンドは明確** (PF 0.43 → 0.61 → 0.85)、しかし PF=1.0 / monthly +0.1% の robust 基準には **依然未達**。

#### 含意

- **シグナル本質見直しは効果あり**: PF が 0.43 → 0.85 まで改善（OOS=2023）。1年限定の IS=2024 では PF 1.33 / monthly +0.87% と robust 基準達成も観察
- **しかし IS=2022 → OOS=2023 では PF 1.12 → 0.85 と低下**: 過剰最適化の兆候は残る
- **完全な robust 確認には至らず**: 3窓 WF（IS=2022/OOS=2023、IS=2023/OOS=2024、IS=2024/OOS=2025）の完走と過半数窓 OOS robust が必要
- **backtest.py の subprocess 経由実行は依然ボトルネック**: workers=1 シリアルでも 9時間半かかった（180 combos × 平均約3分）。完全 WF にはライブラリ化が必要

#### 次ステップ候補

1. **C-2 / C-1 等の別シグナル仕様**で再検証（クロス継続 N 日以内、純粋ゴールデンクロスなど）
2. **3窓 WF の完走**: `backtest.py` のライブラリ化（subprocess → in-process）を実装後、C-WF を 3窓で完走
3. **IS=2024/OOS=2025 窓**でも C を単発検証（2024 スモークが良好なので別 OOS でも見たい）
4. **撤退判断**: 改善トレンドが限界に近いなら、Phase 1 paper trading は当面諦めて貯金フェーズへ

#### 結果ファイル

- `docs/backtests/walkforward_single_C_W1.json`

---

## 9. Strategy Iteration Log

| Date | Strategy | Total Return | PF | Win% | Decision |
|---|---|---|---|---|---|
| 2026-05-23 | Initial (RSI 35-50, ATR×2.5) | -14.0% | 0.30 | 41.2% | ❌ 失敗 |
| 2026-05-23 | Grid Search Best (RSI 35-60, ATR×1.5) | **+9.5%** | **1.20** | 44.9% | ✅ **採用** |
| 2026-05-23 | Breakout Follow (20-day high break) | -16.9% | 0.72 | 33.3% | ❌ ダマシ多発、不採用 |
| 2026-05-24 | Walk-Forward 単窓 IS=2024/OOS=2025 | IS-0.06% OOS-0.21% | OOS 0.77 | OOS 46.7% | ❌ Overfitted 検出 |
| 2026-05-25 | Walk-Forward W1 IS=2022/OOS=2023 (拡張試行) | IS+9.5% OOS-12.7% | OOS 0.43 | OOS 23.8% | ❌ 2/2 窓連続 FAIL → **単一銘柄戦略撤退、3銘柄分散へ** |
| 2026-05-27 | B-WF v2 W1 IS=2022/OOS=2023 (2銘柄分散 ¥200k) | IS+1.51% OOS-0.66% | OOS 0.61 | OOS 改善 | ❌ 分散で改善（PF 0.43→0.61, DD -15→-8.9%）も robust 未達 → **シグナル本質見直し（C フェーズ）へ** |
| 2026-05-27 | C-WF W1 IS=2022/OOS=2023 (MA クロス + 2銘柄分散 ¥200k) | IS+0.40% OOS-0.54% | IS 1.12 / OOS 0.85 | OOS 40.5% | ❌ PF 0.43→0.61→0.85 と改善トレンド明確、しかし robust 未達。IS=2024 スモークは PF 1.33 と robust 達成 → **C-2/C-1 別仕様検証 or ライブラリ化して3窓 WF 完走** |

### Rejected Strategy: Breakout Follow

20日高値ブレイク + 出来高×1.5 + RSI 30-80 をテストしたところ、**保有1日でのストップアウトが54件中29件（54%）** という結果に。日本の小型株では「ブレイクアウト直後に売り叩かれる」（ダマシブレイク）が押し目モメンタムの「下落途中買い」より深刻だった。

---

## 10. Limitations & Caveats

### 10.1 Data Limitations

1. **Survivorship Bias**: 上場廃止銘柄が universe に含まれず、生存銘柄のみで検証 → 実際のリターンは推定より低い可能性
2. **Fundamentals Staleness**: 時価総額・自己資本比率は最新値固定。期中の変動・決算修正は反映されない
3. **Selection Bias of Universe**: 282銘柄の選定基準（手動メンテ）に未公開のバイアスがあるリスク

### 10.2 Execution Model Limitations

1. **No Slippage Model**: 全約定をストップ/利確価格ちょうどで仮定。実際は ±0.1〜0.5% のスリッページが発生
2. **No Liquidity Constraint**: 出来高薄でも100株が即時約定する前提。流動性が枯渇したケースは未反映
3. **OCO Intraday Order Ambiguity**: 同日にストップとTPが両方触れた場合、保守的にストップ優先。実運用では先に触れた方が約定するため、保守的すぎる可能性
4. **Gap Down Entry Risk**: 始値約定モデルは寄りギャップダウンの一部しか反映できない

### 10.3 Statistical Limitations

1. **Sample Size**: 49取引は統計的検定には少ない。Sharpe 0.47 の信頼区間は広い
2. **In-Sample Optimization**: Grid Search を同期間で実施したため、out-of-sample 性能は未知
3. **Single Period**: 2024-2025 という特定期間のみ。異なるマクロ環境（リーマン級ショック、長期低ボラ等）での挙動は不明
4. **No Walk-Forward Analysis**: 期間を区切ったロールフォワード検証は未実施

### 10.4 Strategy-Level Limitations

1. **Single Position**: 1銘柄集中のため銘柄リスク（個別ニュース）が大きい
2. **Long Only**: 下落相場で利益を取れない
3. **Small Universe**: 282銘柄はTOPIX全体の約7%。市場全体の機会を取りこぼしている可能性
4. **Manual Operation Required**: トレーリングストップ更新を手動で行う負荷あり

---

## 11. Conclusions & Next Steps

### 11.1 Conclusions（2026-05-24 更新）

1. ❌ **過剰最適化リスクが顕在化**（Walk-Forward: IS 月-0.06% / OOS 月-0.21%、両期間とも PF<1.0）
2. ⚠ **フル期間 PF 1.20 は特定2年間への過適合**: IS/OOS のベストパラメータが大きく異なる
3. ⚠ **目標 ¥240,000/年 は構造的に未達**: フル期間ベストパラメータでも Phase 3 換算で年¥47,880（20%）、Walk-Forward 反映ならマイナス
4. ⚠ **リスク管理面は機能**: 最大DD は IS/OOS とも -7% 程度で安定。サーキットブレーカー (Phase 1: -30%) には十分な余裕

### 11.2 Immediate Next Steps（2026-05-24 更新）

Walk-Forward 結果を踏まえ、当初予定の「Phase 1 paper trading 即開始」は延期推奨。

| 優先度 | アクション | 期限目安 |
|---|---|---|
| **P0** | Phase 1 paper trading 開始判断（保留 / 限定運用 / 検証延長 から選択） | 2026-05-25 |
| **P1** | Phase 1 開始する場合: オペレーター手順書 ([operator_guide.md](operator_guide.md)) 通り運用 | 即時 |
| **P1** | 戦略本体の見直し（候補B/C 検証、ユニバース拡張、別アセットクラス検討） | 2026-06月内 |
| **P2** | バックテスト期間を拡張（2020〜2025 で6年分） + Walk-Forward 再実施 | 2026-06月内 |
| **P3** | 異なる市場レジーム（コロナショック・利上げ局面）での感度分析 | 2026-07月以降 |

### 11.3 Mid-term Improvement Candidates

実運用データが出揃ったら検討する戦略改善:

1. **シグナル条件の本質見直し**: 候補B（5日×25日MAクロス）/ 候補C（急騰追従）
2. **ユニバース拡張**: 282銘柄 → 東証小型株全体から動的抽出（TOPIX Small に拡張）
3. **複数銘柄分散**: 1ポジ集中 → 同時2〜3銘柄保有でリスク分散
4. **ポジションサイジング動的化**: ATR や直近 DD に応じたサイジング
5. **目標水準の見直し**: 年¥240,000 → 現実的な年¥50,000〜¥100,000 に下方修正

### 11.4 Long-term Strategic Options

1. Phase 3 予算上振れ（¥1M → ¥3M）で目標達成可能性を上げる
2. 別アセットクラス（ETF、REIT）への拡張
3. ロング・ショート戦略への発展

---

## Appendix A: Raw Metrics

詳細メトリクスは [docs/backtests/2026-05-23_phase1_baseline.json](backtests/2026-05-23_phase1_baseline.json) を参照。

## Appendix B: Reproducibility

```bash
# Initial baseline backtest
python scripts/backtest.py --start 2024-01-04 --end 2025-12-30

# JSON metrics only
python scripts/backtest.py --start 2024-01-04 --end 2025-12-30 --quiet --json

# Override parameters (for sensitivity testing)
python scripts/backtest.py --start 2024-01-04 --end 2025-12-30 \
    --target-profit 0.06 --atr-mult 1.5 --rsi-upper 60 --time-stop 5

# Full grid search (~30 min, 4 parallel workers)
python scripts/grid_search.py --workers 4
```

データキャッシュは `.backtest_cache/data_2023-07-08_2025-12-30.pkl` に保存される（初回のみJ-Quants APIからフェッチ、以降は再利用）。

## Appendix C: Related Documents

- [docs/requirements.md](requirements.md) — 投資方針・リスク制約・段階拡大プラン
- [docs/design.md](design.md) — シグナル条件・出口ルール・発注フロー詳細
- [docs/operator_guide.md](operator_guide.md) — オペレーター日次運用手順・開始チェックリスト
- `scripts/backtest.py` — バックテストエンジン
- `scripts/grid_search.py` — パラメータ探索
- `scripts/walk_forward.py` — In-sample / Out-of-sample 検証
- `data/universe.csv` — 対象銘柄リスト

## Appendix D: Developer Notes — Known Structural Risks (No Immediate Action)

以下は実装上の潜在リスクで、現状は表面化していないが将来同様の作業をする際に注意。

### D-1. `JQuantsClient._RateLimiter` のサブプロセス並列リスク

[src/squant/infrastructure/jquants_client.py](../src/squant/infrastructure/jquants_client.py) の `_RateLimiter` は **インスタンスローカル**で、複数サブプロセスが同時起動すると合計 RPM が J-Quants Light の制限（60 req/min）を超過し得る。

**今は表面化しない理由**: `scripts/backtest.py` のキャッシュ再利用ロジック（既存キャッシュが必要期間をカバーするなら再利用）で「並列実行時に API を叩かない」状態になっているため。

**再発条件**: キャッシュ未生成の期間で `scripts/grid_search.py` または `scripts/walk_forward.py` を回す。

**対策案（必要になったら）**:
- スクリプト冒頭でキャッシュ存在をチェックし、未生成期間なら `--workers 1` を強制
- または、事前に `scripts/backtest.py` をフル期間で1回走らせる手順を README に明記
- 抜本対策として、`_RateLimiter` を `multiprocessing.Manager` 経由の共有状態にする

**初出**: 2026-05-24 の Walk-Forward 実行で発覚（180通り × 4並列が3時間進まなかった事象）。`backtest.py` のキャッシュ互換ロジックで暫定回避済み。
