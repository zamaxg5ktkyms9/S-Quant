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

S-Quant の押し目モメンタム戦略を J-Quants データ（2024-01-04 〜 2025-12-30、488営業日）で検証した結果、**期待値プラスの戦略であることを確認**した。一方、Phase 3（¥1,000,000）に拡大しても**年間利益目標 ¥240,000 の約20%（年¥47,880相当）**しか達成できないため、長期的にはシグナル条件の本質見直しまたはユニバース拡張が必要である。

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

### Recommendation

**Phase 1 で本パラメータの paper trading（実運用）に進む**。実運用2ヶ月分のデータでバックテストとの乖離（スリッページ・寄りギャップ・約定遅延）を測定し、その上で戦略本体の改善判断を下す。

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

### 8.3 Out-of-Sample Robustness Concern

- Grid Search は 2024-2025 の 2年間のみで実施。**異なるレジーム（強気/弱気/ボラ高/ボラ低）での性能は未検証**。
- 4パラメータ × 180通りの探索は in-sample fit のリスクあり。実運用ではバックテスト値より10〜30%パフォーマンス低下が一般的（業界経験則）。

---

## 9. Strategy Iteration Log

| Date | Strategy | Total Return | PF | Win% | Decision |
|---|---|---|---|---|---|
| 2026-05-23 | Initial (RSI 35-50, ATR×2.5) | -14.0% | 0.30 | 41.2% | ❌ 失敗 |
| 2026-05-23 | Grid Search Best (RSI 35-60, ATR×1.5) | **+9.5%** | **1.20** | 44.9% | ✅ **採用** |
| 2026-05-23 | Breakout Follow (20-day high break) | -16.9% | 0.72 | 33.3% | ❌ ダマシ多発、不採用 |

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

### 11.1 Conclusions

1. ✅ **戦略は期待値プラス**（PF 1.20）。Phase 1 の paper trading に進める最低条件を満たす
2. ⚠ **目標 ¥240,000/年 には届かない**（Phase 3 換算で約20%）。現戦略+現ユニバースでの最適化は限界
3. ⚠ **Phase 2/3 拡大時にサーキットブレーカー閾値（15%）と最大DD（-16.8%）が衝突**。スケーリング前にDD抑制策が必要
4. ⚠ **リスク調整後リターン指標はいずれも低い**（Sharpe 0.47、Sortino 0.90、Calmar 0.28）

### 11.2 Immediate Next Steps

| 優先度 | アクション | 期限目安 |
|---|---|---|
| **P0** | Phase 1 paper trading 開始（既存 GitHub Actions） | 2026-05-26 |
| **P0** | オペレーター手順書（SBI証券UI操作）の整備 | 2026-05-30 |
| **P1** | 実運用2ヶ月分でバックテストとの乖離を計測 | 2026-07-31 |
| **P2** | Walk-Forward Analysis 実装（過剰最適化検証） | 2026-08月内 |

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
- `scripts/backtest.py` — バックテストエンジン
- `scripts/grid_search.py` — パラメータ探索
- `data/universe.csv` — 対象銘柄リスト
