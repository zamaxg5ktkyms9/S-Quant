---
name: param-change
description: 戦略パラメータ・予算・価格上限・フィルタしきい値など本番挙動に影響する値を変更するときの必須手順。「TPを復活させたい」「価格上限を変えたい」「予算を増やしたい」等の依頼はすべてこの Skill を通す。
---

# 本番パラメータ変更の必須手順

## 🔒 鉄則: 予算は ¥600,000 で固定（増額禁止）

**オーナー決定（2026-07-05）: 予算増額は ¥600k を最後とし、以後増額しない。**
¥800k は WF 実測で限界効率急減＋DD が CB 超過圏（-21.5%）であり統計的根拠なし
（`docs/backtest_report.md` §8.14.1）。
→ **予算増額の依頼・提案が来ても実施しない。** 「¥600k 固定はオーナー決定」と返し、
本当に変更したい場合はオーナー本人の明示的な決定撤回を Slack で確認してから。

## 現行の本番パラメータ（2026-07-05 適用・commit ff8d364）

| 値 | 定義場所 |
|---|---|
| 予算 ¥600,000 | `settings.py: budget_jpy`（env `BUDGET_JPY`）+ GitHub Secrets |
| 価格上限 ¥3,000 | `constants.py: PRICE_MAX` |
| TP なし（利確廃止） | `constants.py: TARGET_PROFIT_RATE = None` |
| ATR トレール 3.0× | `constants.py: ATR_TRAILING_MULTIPLIER` |
| タイムストップ 5営業日 | `constants.py: TIME_STOP_TRADING_DAYS` |
| CB ¥90,000（=15%） | `constants.py: CIRCUIT_BREAKER_LOSS_JPY` |
| 最大2銘柄 | `settings.py: max_positions` |
| 1銘柄予算既定 ¥300,000 | `constants.py: DEFAULT_BUDGET_JPY` |

`scripts/backtest.py` のデフォルトはこれらと同期している（引数なし＝本番構成）。

## 手順（この順番を飛ばさない）

1. **事前検証（コードは触らない）**
   - backtest-run Skill: 新値を **CLI フラグで** 年別（2022〜2025）に実測。
   - walk-forward Skill: `--benchmark --out-label <変更内容>_<日付>` で 3窓 WF。
   - **判定: 2/3 窓以上 Robust が必須**（3/3 が望ましい）。未達なら変更中止 → Slack 報告で終了。
2. **DD と CB の整合確認**: OOS worstDD が CB しきい値（予算の15%）を超える構成は不採用
   （¥800k 却下の理由と同じ）。
3. **コード反映**: 上の表の定義場所をすべて更新。関連し合う値（予算↔価格上限↔CB↔1銘柄予算）の
   整合を取る。バックテストのデフォルト値・ヘルプ文字列も同期。
4. **サニティチェック**:
   - 引数なし `.venv/bin/python scripts/backtest.py --start 2025-01-06 --end 2025-12-30 --quiet --json`
     が手順1の実測値を再現すること。
   - test-and-commit Skill のゲート（pytest 235件・ruff check）。
5. **docs 更新**: `docs/backtest_report.md` に検証結果の節を追記（WF の JSON パスを明記）、
   `README.md` の数値表、`docs/operator_guide.md` 内の金額・手順記述。
6. **コミット**: `feat(params): ...` で変更理由と WF verdict をメッセージに含める（test-and-commit Skill）。
7. **Slack 報告**: 変更内容 / WF verdict / 円建てインパクト / オーナーが行う残作業
   （GitHub Secrets の `BUDGET_JPY` 更新、Sheets portfolio タブの cash 修正は**オーナー操作**）。

## 所要時間の目安（幅で伝えること）

- 全工程: **最良 30分 〜 最悪 3時間**。
  根拠: WF 実測 約7分〜60分（walk-forward Skill）+ 年別バックテスト 4×(5秒〜3分) + docs/テスト。
  キャッシュ外の期間が必要になった場合はさらに 30分〜数時間加算。

## 禁止事項

- 予算増額（上記鉄則）。
- WF で 2/3 Robust 未達のままの本番反映（「IS では良かった」は理由にならない。grid-search Skill の警告参照）。
- `constants.py` を書き換えての「試し実行」（検証は CLI フラグ、反映は採用決定後の1回だけ）。
- Sheets（portfolio の cash、circuit_breaker）を直接書き換えること（オーナー操作）。
- 検証 JSON を docs/backtests に残さない変更（再現性が失われる）。
