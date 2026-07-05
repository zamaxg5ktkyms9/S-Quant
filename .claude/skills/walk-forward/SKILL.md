---
name: walk-forward
description: Walk-Forward（IS/OOS）検証の実行と Robust 判定の読み方。パラメータ・予算・戦略の変更を検証するとき、「WFして」「Robustか確認して」と頼まれたとき、grid-search の結果を採用判断に進めるときに必ず使う。
---

# Walk-Forward 検証（walk_forward.py）

## 目的

Grid Search の in-sample ベストが out-of-sample でも通用するか（過剰最適化でないか）を検証する。
**本番パラメータ・予算の変更は、この検証で Robust 判定を得るまで適用禁止**（param-change Skill 参照）。

## 前提（毎回確認）

- `docs/operational_notes.md` を読む。特に §0（in-process がデフォルト）と §2（ベンチ必須）。
- キャッシュ `.backtest_cache/data_2021-07-08_2025-12-30.pkl` の存在確認。
  無ければ `FileNotFoundError` で即終了する → backtest-run Skill のキャッシュ生成手順（30分〜数時間）が先。

## 実行手順（この形のまま実行する）

```bash
.venv/bin/python scripts/walk_forward.py --benchmark --out-label <目的>_<YYYY-MM-DD>
```

- `--benchmark` は **必須**（CLAUDE.md ルール）。10 combos × 1窓で実測し、本実行の ETA を出力
  してから**そのまま本実行に続く**（ベンチだけで止まる機能はない）。
- `--out-label` は **必須**。省略すると `walkforward_rolling3.json` に固定され、
  過去の検証結果を上書きする恐れがある。日付入りラベルを付ける（例: `pmax3500_2026-07-10`）。
- デフォルトで rolling 3窓（IS/OOS 各1年: 2022→2023, 2023→2024, 2024→2025）、
  in-process シリアル実行、全体タイムアウト 3600秒（超過で自動 abort）。
- パラメータ変更の検証は `--budget` `--price-max` `--signal` `--max-positions` フラグで渡す。

## 所要時間（幅で伝えること）

- **最良 7分 〜 最悪 60分**（60分で `--max-wall-clock-seconds` デフォルトにより自動 abort）。
- 根拠: 実測 2026-07-05 = ベンチ 13秒（1.27s/combo）→ 96 combos × 3窓 + OOS で全体約7分。
- ベンチ出力の「全 3 窓推定」が 30分を超えていたら、本実行を続けさせず一度 Ctrl-C 相当で止めて
  設定を見直す（過去に subprocess モードで 9時間かかった事例あり。operational_notes §1）。

## Robust 判定の読み方

各窓: OOS PF ≥ 1.0 **かつ** OOS 月次リターン ≥ +0.1% で PASS。

| ロバスト窓数 | Verdict | 取るべき行動 |
|---|---|---|
| 3/3 または 2/3 | ✅ Robust | 採用検討可。結果を docs に記録して param-change の次工程へ |
| 1/3 | ⚠ Marginal | 採用禁止。時期依存。追加検証の提案をオーナーに Slack |
| 0/3 | ❌ Overfitted | 採用禁止。撤退・再設計をオーナーに Slack |

参考: 現行本番構成（¥600k/上限¥3,000/拡張グリッド）の直近結果は **3/3 Robust**
（W1 +0.32%/PF1.08、W2 +0.98%/PF1.22、W3 +1.78%/PF1.42。2026-07-05 再現確認済み）。

## 結果の保存と報告

- 出力: `docs/backtests/walkforward_<ラベル>.json`（自動保存。git 管理対象）。
- サマリ表・Verdict・所要時間の実測を Slack 報告に含める。
- 採用判断に使った場合は `docs/backtest_report.md` に節を追記する。

## 禁止事項

- `--out-label` なしの実行（既存結果の上書きリスク）。
- `--mode subprocess` の使用（同値性検証という明確な目的がある場合を除く）。
  subprocess モードで `--workers 4` は過去 9時間詰まりの実績があり特に禁止。
- WF 実行中に別のバックテスト／grid search を並列起動すること。
- Marginal / Overfitted の結果を「概ね良好」などと言い換えて採用に進めること。
