# docs/backtests — 検証結果ファイルの索引

各 JSON は実行時点のスナップショット（追記・改変しない）。分析の文脈は [../backtest_report.md](../backtest_report.md) の対応セクションを参照。

## 現行構成の根拠（¥600k・noTP/ATR3.0/TS5 採用、2026-07-05）

| ファイル | 内容 | 結果 | 参照 |
|---|---|---|---|
| `wf_extgrid_600k_pmax3000_2026-07-04.json` | 拡張グリッド WF（TP4-10%/なし・ATR≤3.0・TS≤15日、96 combos×3窓） | **3/3 Robust、OOS平均 +1.03%/月**（基準グリッド +0.64%） | §8.14.2 |
| `candidate_fixed_600k_2026-07-05.json` | 固定パラメータ候補6セットの4年通し評価 | **noTP/ATR2.5〜3.0 系が優位** → noTP/ATR3.0/TS5 採用 | §8.14.3 |
| `walkforward_rolling3_C_600k_pmax3000.json` | ¥600k/上限¥3,000 の基準グリッド WF | ✅ Robust 3/3、OOS平均 +0.64%/月 | §8.14.1 |

## 予算ティア比較（松竹梅、2026-07-04）

| ファイル | 内容 | 結果 |
|---|---|---|
| `walkforward_rolling3_C_400k_pmax2000.json` | ¥400k/上限¥2,000 WF | ✅ 2/3、OOS平均 +0.39%/月 |
| `walkforward_rolling3_C_800k_pmax4000.json` | ¥800k/上限¥4,000 WF | ✅ 3/3 だが効率頭打ち・worstDD -21.5%（CB超過圏）→ 不採用 |
| `w1best_400k_pmax2000_2026-07-04.json` | 旧W1best固定の¥400k転用可否 | ❌ 転用不可（平均+0.17%/月、2024 DD -22%） |

## 旧本番パラメータの検証（W1best 採用の根拠、2026-07-04）

| ファイル | 内容 | 結果 | 参照 |
|---|---|---|---|
| `production_param_sensitivity_2026-07-04.json` | 旧本番値（TP6/ATR1.5/TS5）の年次grid順位 | ❌ 4年中3年マイナス、2025年 172/180位 | §8.12.2 |
| `candidate_fixed_params_2026-07-04.json` | ¥200k時代の固定候補5セット比較 | W1best（TP5/ATR2.5/TS5）優位 → 一時採用（後に noTP へ） | §8.12.2 |
| `walkforward_rolling3_C_inprocess.json` | rolling 3窓の一括再実行（in-process 初回） | ✅ Robust 2/3 を統合 JSON で確定 | §8.12.1 |

## C 戦略採用時の単窓記録（2026-05-27〜29、歴史的資料）

| ファイル | 内容 | 結果 |
|---|---|---|
| `walkforward_single_C_W1.json` | IS=2022→OOS=2023（¥200k） | ❌ PF 0.85 |
| `walkforward_single_C_W2.json` | IS=2023→OOS=2024（¥200k） | ✅ PF 1.21 |
| `walkforward_single_C_W3.json` | IS=2024→OOS=2025（¥200k） | ✅ PF 1.36 |
| `walkforward_2024-01-04_2025-12-30.json` | A1 時代の単窓 WF（過剰最適化検出） | ❌ Overfitted | 
| `2026-05-23_phase1_baseline.json` | A1 初期ベースライン（¥100k） | 歴史的資料 |

## 真正 OOS（選定に未使用のデータでの検証）

| ファイル | 内容 | 結果 | 参照 |
|---|---|---|---|
| `oos_2026H1_600k_2026-07-10.json` | 2026 H1（全選定の後の完全未使用期間）に現行構成を適用 | **+3.46%/月、PF 2.30、DD -5.5%**（好レジームの1サンプルである点に注意） | §8.16 |

## その他

| ファイル | 内容 |
|---|---|
| `walkforward_skillcheck_2026-07-05.json` | walk-forward Skill の動作確認用（分析には未使用） |

## 再現方法

```bash
# 現行構成の WF（in-process、実測 3〜5分）
python scripts/walk_forward.py                      # デフォルト = 本番構成

# ティア比較の再現例
python scripts/walk_forward.py --budget 400000 --price-max 2000 --out-label repro_400k
```
