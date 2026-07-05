---
name: backtest-run
description: バックテストの実行手順。「バックテストして」「〜年の成績を実測して」「このパラメータで何%になるか」と頼まれたとき、または param-change / walk-forward の前段で単発の成績実測が必要なときに使う。
---

# バックテスト実行（backtest.py）

## 前提（毎回確認）

- 作業ディレクトリはリポジトリルート。Python は必ず `.venv/bin/python` を使う（`python` 直呼び禁止）。
- 実行前に `docs/operational_notes.md` を読む（長時間処理の既知の落とし穴集）。
- キャッシュ確認: `ls -la .backtest_cache/`
  - `data_2021-07-08_2025-12-30.pkl` があれば **2022-01-04〜2025-12-30 の範囲はキャッシュで即実行できる**。
  - この範囲外（例: 2026年）を指定するとJ-Quants からのデータ取得＝キャッシュ生成が走る。
    **所要 30分〜数時間**（`docs/operational_notes.md` §6）。生成は必ず単独プロセスで行い、並列実行しない。

## 本番パラメータでの実行（デフォルト）

引数なしのデフォルトが本番採用値（2026-07-05 適用: 予算¥600,000 / noTP / ATR3.0 / TS5営業日 /
価格上限¥3,000 / 2銘柄 / ma_cross）。**パラメータフラグを付けなければ本番構成**になる。

```bash
# 1年分の成績実測（人間向け出力）
.venv/bin/python scripts/backtest.py --start 2025-01-06 --end 2025-12-30

# 機械可読（メトリクスJSONが最終行に __METRICS_JSON__ 付きで出る）
.venv/bin/python scripts/backtest.py --start 2025-01-06 --end 2025-12-30 --quiet --json
```

検証済みの期待値（2026-07-05 実測）: 2025年は `monthly_pnl_pct=+1.93 / PF=1.44 / trades=96`。
引数なし実行がこれを再現しなければ本番パラメータが壊れている → 作業を止めてオーナーに Slack 報告。

## 所要時間（幅で伝えること）

| ケース | 最良〜最悪 | 根拠 |
|---|---|---|
| 1年・キャッシュ有 | 5秒〜3分 | 実測 4.8秒（2025年・2026-07-05）。operational_notes §5 |
| 4年を1回で直接実行 | **禁止** | O(N²)的な遅さで4時間超の実績。年別4回に分割する |
| キャッシュ生成を伴う | 30分〜数時間 | operational_notes §6。過去に数時間かかった事例あり |

4年分の成績が要るときは 2022 / 2023 / 2024 / 2025 を**1年ずつ順番に**実行して集計する。

## パラメータを変えて実測するとき

必ず CLI フラグで渡す。例:

```bash
.venv/bin/python scripts/backtest.py --start 2024-01-04 --end 2024-12-30 \
  --target-profit 0.06 --atr-mult 2.5 --time-stop 7 --quiet --json
```

- 使えるフラグ: `--budget` `--target-profit` `--atr-mult` `--rsi-upper` `--rsi-lower`
  `--time-stop` `--price-max` `--max-positions` `--signal {pullback,ma_cross}`
- 単発バックテストの結果だけで採用判断をしてはいけない → 必ず walk-forward Skill に進む。

## 禁止事項

- `src/squant/config/constants.py` を書き換えて「試し実行」すること（本番コードが汚れる。フラグで渡す）。
- 複数のバックテスト／キャッシュ生成プロセスの同時起動（pickle I/O 競合。operational_notes §1・§6）。
- 4年フル期間の直接実行。
- 所要時間を単一の数字で断言すること（必ず「最良〜最悪」＋根拠の式）。

## 結果の記録

採用判断に関わる結果は `docs/backtests/` に日付入りファイル名で JSON を保存し、
`docs/backtest_report.md` に節を追記する（param-change Skill 参照）。
