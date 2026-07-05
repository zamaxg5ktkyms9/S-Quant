---
name: daily-ops
description: デイリー運用確認。「今日の運用状態を確認して」「昨夜のランは成功した？」「シグナルが出ない」「サーキットブレーカーの状態は？」と聞かれたとき、および毎日の定期確認タスクで使う。
---

# デイリー運用確認

本番は GitHub Actions が平日 20:30 JST（cron `30 11 * * 1-5` UTC）に実行する。
GHA のスケジュール遅延があるため、**実際の完走は 22:00〜24:00 JST 頃**が多い（run_log 実績）。

## Step 1: 運用状態ダンプ（読み取り専用・常に最初にやる）

```bash
.venv/bin/python scripts/ops_status.py
```

出力の確認ポイント（上から順に）:

1. **circuit_breaker**: `is_tripped` が `True` なら最優先事項。即オーナーに Slack 報告
   （新規エントリー停止中。リセットはオーナーが Sheets を手動編集する。勝手にリセットしない）。
2. **run_log の最終行**: **直近の平日（東証営業日）** の日付で `status=success` か。
   - if 今日が土日・祝日 → 当日の行が無いのは正常（cron は平日のみ）。直近の平日の行を見る。
   - if 直近平日の行が無い/failed → Step 2 へ。
   - if success → Step 3 へ。
3. **portfolio**: `state`（IDLE/保有中）と `cash_jpy`。
4. **pending_signals**: 未消化シグナルの有無（あればオーナーの発注待ち。約定報告の催促を報告に含める）。

## Step 2: ランが失敗・欠落しているとき

- `gh` CLI は**未認証**（2026-07-05 時点）。GHA ログはローカルから見られない。
- if 前営業日 23:30 JST を過ぎても run_log に行が無い → GHA 未実行/失敗の疑い。
  Slack に「daily_run が未完走。GitHub → Actions → Daily Trading Run のログ確認が必要」と報告する。
  よくある原因: J-Quants 認証エラー、レートリミット（operator_guide §3.3）。
- ローカルのパイプライン健全性は synthetic ドライランで切り分ける（実測 約1秒、外部書き込みなし）:

```bash
BYPASS_EXECUTION_TIME_GUARD=true BYPASS_TRADING_DAY_CHECK=true \
  .venv/bin/python scripts/dry_run.py --synthetic
```

`Result: OK` ならコードは健全＝原因は GHA/データソース側。FAILED ならトレースバックを添えて報告。

## Step 3: 「シグナルなし」の診断（ファネル確認）

シグナルなしは**多くの日で正常**（C戦略の実測は月4〜6件。バックテストで日次通過6〜8銘柄でも
シグナルは週1件程度）。連日続くときだけ以下のファネルで「どこで絞られたか」を特定する。

ファネル: ユニバース282 → OHLCV有効銘柄 → ファンダ通過（市場: 時価総額→流動性→PBR→自己資本比率→**価格¥100〜3,000**→決算ブラックアウト）→ MAクロス条件 → シグナル。

- 通過数の一次情報は Slack の当日ラン通知（「有効銘柄 X/282」「スクリーニング通過: N銘柄」）。
  **Slack は送信専用で自分では読めない** → 必要なら「当日の通知本文の転記」を Slack でオーナーに依頼する。
- フィルタ別の脱落数（`Screener filter counts` ログ行）は GHA ログにのみ出る（gh 未認証のため
  現状はオーナーに Web 確認を依頼）。
- if スクリーニング通過が 0〜2銘柄 → 価格上限が最有力容疑（2026-06 に上限¥1,000で 203→1 まで
  絞られた実績。¥3,000 化で回復済みのはず）。詳細は [references/funnel.md](references/funnel.md)。
  是正案（フィルタ緩和・価格上限変更）は**必ず param-change Skill 経由**（WF 再検証必須）。
- if 通過銘柄が数件あるのにシグナルなしが2週間以上 → 市況要因の可能性。
  バックテストで直近実績を確認し（backtest-run Skill）、オーナーに判断材料を報告。

## Step 4: 報告

確認結果を Slack に報告する（slack-report Skill の様式）。異常なしでも「異常なし」と一行報告する。

## 禁止事項

- `python -m squant.main` を `DRY_RUN=true` なしで実行（本番 Sheets 書き込み＋Slack 通知が飛ぶ）。
- `scripts/dry_run.py --notify`（本物の Slack 通知が飛ぶ）。
- `scripts/bootstrap_sheet.py --apply`（本番シート初期化。オーナー明示指示なしで実行禁止）。
- 実データ（yfinance）での `dry_run.py` を診断根拠にすること
  （ローカルはレート制限で失敗しがち。実測: 2分42秒かけて失敗）。
- Sheets の手動編集（circuit_breaker リセット・cash 変更はオーナー操作）。
