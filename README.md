# S-Quant

日本株の自動スクリーニング・シグナル検出システム。平日 20:15 JST に GitHub Actions で自動実行される。

詳細ドキュメント: [要件](docs/requirements.md) / [設計](docs/design.md)

## 概要

- **対象**: 東証上場銘柄（S株単元 = 1株ずつ売買）
- **予算**: ¥100,000
- **運用モデル**: 半自動（シグナル検出 → オペレーター確認 → 手動発注）
- **データソース**: J-Quants API v2（価格・ファンダメンタルズ、RPM=50）

## 処理フロー

```
ユニバース読み込み（282銘柄）
  → 接続確認（J-Quants / Google Sheets）
  → ファンダメンタルスクリーニング（時価総額・流動性・PBR・自己資本比率・株価）
  → テクニカルシグナル検出（トレンド・RSI・ボラティリティ・出来高）
  → 上位1銘柄を選出（RSI昇順 → 出来高サージ降順 → PBR昇順）
  → Slack通知 + Google Sheets に記録
```

保有中は毎営業日の終値で出口ルールを評価し、決済シグナルがあれば Slack で通知する。

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # APIキーを設定
```

### 必要な環境変数

| 変数名 | 用途 | デフォルト |
|---|---|---|
| `JQUANTS_API_KEY` | J-Quants API v2 キー | — |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook | — |
| `SPREADSHEET_ID` | Google Sheets ID | — |
| `GCP_SA_KEY_JSON` | GCP サービスアカウントキー（JSON文字列） | — |
| `BUDGET_JPY` | 1回あたりの投資上限 | `100000` |
| `STOP_LOSS_RATE` | ハードストップロス率 | `0.025` |
| `GAP_UP_THRESHOLD` | ギャップアップキャンセル閾値 | `0.02` |
| `TIME_STOP_DAYS` | タイムストップ日数 | `5` |
| `CIRCUIT_BREAKER_LOSS_JPY` | サーキットブレーカー発動損失額 | `30000` |
| `JQUANTS_RPM` | J-Quants APIレート上限（req/min） | `50` |
| `DRY_RUN` | 書き込みなし・通知なしのテストモード | `false` |

## 実行

```bash
# ローカルテスト（書き込みなし）
DRY_RUN=true python -m squant.main

# 本番（GitHub Actions で自動実行）
# .github/workflows/daily_run.yml — 平日 20:15 JST (11:15 UTC)
# ※ 20:00 JST 以前のローカル実行は execution_time_guard でスキップされる
```

## バックテスト

```bash
# 2024年分（初回はJ-Quants APIからデータ取得、以降はキャッシュ使用）
python scripts/backtest.py --start 2024-01-04 --end 2024-12-31

# RPMを下げて実行（タイムアウト回避）
python scripts/backtest.py --start 2024-01-04 --end 2024-12-31 --rpm 20

# 詳細ログ（毎日のフィルタ結果表示）
python scripts/backtest.py --start 2024-01-04 --end 2024-12-31 --verbose
```

取得データは `.backtest_cache/` にキャッシュされる。再実行時はAPIコールなしで即座に完了する。

## テスト

```bash
pytest
pytest -m "not network"  # ネットワーク不要なテストのみ
```

## プロジェクト構成

```
docs/
├── requirements.md          # 要件（投資方針・リスク制約・運用制約）
└── design.md                # 設計（戦略・スクリーニング・シグナル・出口ルール）
src/squant/
├── application/
│   ├── daily_runner.py          # 状態機械ディスパッチャ
│   └── pipelines/
│       ├── idle_pipeline.py     # シグナル検出フロー
│       ├── holding_pipeline.py  # 出口評価フロー
│       └── settling_pipeline.py # 決済確認フロー
├── config/
│   ├── constants.py             # 全パラメータ定数
│   └── settings.py              # 環境変数設定
├── domain/
│   ├── screener.py              # ファンダメンタルフィルタ
│   ├── signal_engine.py         # テクニカルシグナル検出
│   ├── ranking.py               # 候補ランキング
│   ├── position_manager.py      # 出口ルール評価
│   └── quantity_calculator.py   # 株数・利確価格計算
└── infrastructure/
    ├── jquants_client.py        # J-Quants API v2 クライアント
    ├── yfinance_client.py       # yfinance フォールバック
    ├── sheets_client.py         # Google Sheets クライアント
    └── sheets_repository.py     # 状態永続化
scripts/
└── backtest.py                  # バックテストスクリプト
```
