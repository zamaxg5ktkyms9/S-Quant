---
name: grid-search
description: パラメータ探索（Grid Search）の実行手順。「パラメータを探索して」「ベストな組合せを探して」と頼まれたときに使う。結果は必ず walk-forward 検証とセットで扱う（単独での採用判断は禁止）。
---

# Grid Search（grid_search.py）

## ⚠ 最重要: Grid Search の結果だけで判断しない

**2026-05 の実例**: フル期間 Grid Search のベストが月+0.40%（PF 1.20）を示したが、
Walk-Forward 検証では IS 月-0.06%（PF 0.96）/ OOS 月-0.21%（PF 0.77）で
**Verdict ❌ Overfitted** だった（`docs/backtest_report.md` §8.3〜8.5）。
単年 IS ベストは常に過大評価される（例: 2024年単窓ベストは月+4.25%/PF2.03 だが、
これはその年へのフィッティング）。

**ルール: Grid Search の好成績を報告するときは、必ず「WF 未検証」であることを明記し、
採用判断には walk-forward Skill での 2/3 窓以上 Robust を必須とする。**

## 前提（毎回確認）

- `docs/operational_notes.md` を読む（§0: in-process デフォルト、§1〜3 の落とし穴）。
- キャッシュ `.backtest_cache/data_2021-07-08_2025-12-30.pkl` があること
  （2022-01-04〜2025-12-30 の窓のみ即実行可。範囲外はキャッシュ生成 30分〜数時間が先）。

## 実行手順

```bash
# 1年窓・96 combos（拡張グリッド: TP 4〜10%/なし × ATR 1.5〜3.0 × TS 5〜15 × RSI）
.venv/bin/python scripts/grid_search.py --start 2024-01-04 --end 2024-12-31 --top 5
```

- デフォルトは in-process シリアル実行（推奨のまま変えない）。
- 予算・銘柄数・価格上限を変えて探索するときは `--budget` `--max-positions` `--price-max`
  `--signal` フラグで渡す。`constants.py` の書き換えは禁止。

## 所要時間（幅で伝えること）

- 1年窓・キャッシュ有: **最良 40秒 〜 最悪 10分**。
  根拠: 実測 2026-07-05 = 37秒（2024年窓・96 combos・約0.3〜1.3s/combo。年により変動、
  2022年窓は重い）。
- 2窓以上まとめて回す・キャッシュがない等の大規模実行は、先に walk-forward Skill の
  `--benchmark` 相当の小規模実測をしてから（CLAUDE.md ルール: 180 combos×3窓以上はベンチ必須）。

## 結果の読み方と保存

- 標準出力に上位 N 件（`--top`）のパラメータとメトリクスが出る。
- 全結果は `.backtest_cache/grid_search_results.json` に保存されるが、
  **このディレクトリは gitignore されており、次回実行で上書きされる**。
  残す価値がある結果は `docs/backtests/grid_<目的>_<YYYY-MM-DD>.json` にコピーする:

```bash
cp .backtest_cache/grid_search_results.json docs/backtests/grid_<目的>_<YYYY-MM-DD>.json
```

## 次の工程

1. 上位パラメータを控える。
2. **必ず** walk-forward Skill で 3窓検証する（IS ベスト抽出は walk_forward.py が
   内部で grid search を回すので、通常は最初から walk_forward.py だけで足りる。
   単独の grid_search.py は「特定1窓の感度を見る」用途に限る）。
3. Slack 報告には「grid の数字は in-sample であり WF 未検証」と明記する。

## 禁止事項

- Grid Search の成績を根拠に constants.py / 本番パラメータを変更すること（WF 必須）。
- `--mode subprocess --workers 4`（過去 9時間詰まり。operational_notes §1）。
- 他のバックテスト系プロセスとの並列起動。
- 所要時間の断言（幅＋根拠で伝える）。
