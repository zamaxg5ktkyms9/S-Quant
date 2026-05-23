# S-Quant

日本株の自動スクリーニング・シグナル検出システム。平日 20:15 JST に GitHub Actions で自動実行される。

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

## スクリーニング条件

| 項目 | 条件 |
|---|---|
| 株価 | ¥100 〜 ¥3,000 |
| 時価総額 | ¥30億以上 |
| 流動性 | 5日平均売買代金 ¥1億以上 |
| PBR | 0.5 〜 2.0 倍 |
| 自己資本比率 | 30%以上 |
| 決算ブラックアウト | 決算発表日 ±3営業日は除外 |

## シグナル条件（4条件すべて必要）

| 条件 | 内容 |
|---|---|
| トレンド | 終値 > 75日移動平均（長期上昇トレンド） |
| RSI(5) | RSI < 45（短期押し目） |
| ボラティリティ | 20日間標準偏差 < 過去平均（収縮局面） |
| 短期 + 出来高 | 終値 > 5日移動平均 かつ 当日出来高 > 前日出来高 |

候補が複数の場合は **RSI昇順 → 出来高サージ率降順 → PBR昇順** でランキングし、上位1銘柄を選出。

## 発注数量

```
最悪ケース執行価格 = 前日終値 × (1 + 2%)  ← ギャップアップ上限
株数 = floor(min(残高, ¥100,000) / 最悪ケース執行価格)
```

始値が前日終値 × 1.02 を超えた場合はオペレーターがキャンセルする。

## 出口ルール（優先順位順）

保有中は毎営業日の終値で以下の順に評価し、最初に該当したものが発動する。

### 1. タイムストップ（最優先）
保有 5営業日経過で強制決済。損益に関わらず発動する。

### 2. ハードストップロス
```
損切価格 = エントリー価格 × (1 - 2.5%)
終値 ≤ 損切価格 → 売り
```

### 3. トレーリングストップ
```
ATR(14) = 過去14日の真の値幅の平均
新ストップ = 終値 - 1.5 × ATR(14)
有効ストップ = max(ハードストップ価格, 新ストップ)  # 下がらないラチェット
終値 ≤ 有効ストップ → 売り
```
株価上昇に連動してストップが切り上がる。下落時は下がらない。

### 4. 利確
```
利確価格 = エントリー価格 × (1 + 0.5%) × (1 + 4%) / (1 - 0.5%)
         ≈ エントリー価格 × 1.051
終値 ≥ 利確価格 → 売り
```
S株の売買スプレッド（片道0.5%）を加味した**純利益ベースの +4%** で算出。

### 実質最大損失
```
最大損失 ≈ 投資額 × 3.5%（スプレッド込み）
         = ¥100,000 × 3.5% = ¥3,500
```
-2.5%ストップ ＋ 両端スプレッド0.5%×2 = 実質 -3.5%。

## リスク管理

### サーキットブレーカー
累積実現損失が ¥30,000 を超えると全取引を停止し、Slack に通知する。手動リセットが必要。

### state machine
```
IDLE → PENDING_SIGNAL → HOLDING → SETTLING → IDLE
```
各状態は Google Sheets で永続化される。二重エントリーはべき等性ガードで防止。

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
