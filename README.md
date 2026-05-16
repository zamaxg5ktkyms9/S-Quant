# S-Quant

日本株の自動スクリーニング・シグナル検出システム。平日20:15 JST にGitHub Actions で自動実行される。

## 概要

- **対象**: 東証上場銘柄（S株単元）
- **予算**: 10万円
- **運用モデル**: 半自動（シグナル検出 → オペレーター確認 → 手動発注）
- **データソース**: J-Quants API v2（価格・ファンダメンタルズ）

## 処理フロー

```
ユニバース取得
  → ファンダメンタルスクリーニング（時価総額・流動性・PBR・自己資本比率・株価）
  → テクニカルシグナル検出（トレンド・RSI・ボラティリティ・出来高）
  → Slack通知 + Google Sheetsへ記録
```

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # APIキーを設定
```

必要な環境変数：

| 変数名 | 用途 |
|---|---|
| `JQUANTS_API_KEY` | J-Quants API v2 キー |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook |
| `SPREADSHEET_ID` | Google Sheets ID |
| `GCP_SA_KEY_JSON` | GCP サービスアカウントキー（JSON文字列） |

## 実行

```bash
# ローカルテスト（書き込みなし）
DRY_RUN=true python -m squant.main

# 本番（GitHub Actionsで自動実行）
# .github/workflows/daily_run.yml — 平日 20:15 JST
```

## テスト

```bash
pytest
pytest -m "not network"  # ネットワーク不要なテストのみ
```
