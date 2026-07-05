# スクリーニング・ファネル診断の詳細

## フィルタの並び（src/squant/domain/screener.py `apply_fundamental_filters`）

ユニバースの各銘柄は以下の順で判定され、**最初に引っかかった段のカウンタ**に落ちる
（`filter_counts`。後段の条件は評価されない点に注意）:

| カウンタ | 条件（落ちる場合） | 定数（constants.py） |
|---|---|---|
| `no_fundamentals` | ファンダデータなし | — |
| `market_cap` | 時価総額 < 下限 | `MARKET_CAP_MIN_JPY`（¥3B） |
| `liquidity` | 5日平均売買代金 < 下限 | `LIQUIDITY_MIN_JPY`（¥100M/日） |
| `pbr` | PBR が範囲外 | `PBR_MIN`〜`PBR_MAX` |
| `equity_ratio` | 自己資本比率 < 下限 | `EQUITY_RATIO_MIN`（30%） |
| `price` | 終値が ¥100〜¥3,000 外 or 価格データなし | `PRICE_MIN`/`PRICE_MAX` |
| `blackout` | 決算発表ブラックアウト中 | `data/earnings_calendar.csv` |

ログ行の形式（GHA ログ / dry_run 出力に出る）:

```
Screener filter counts (dropped): no_fund=0 market_cap=47 liquidity=90 pbr=30 equity_ratio=4 price=30 blackout=0 passed=1
```

この後さらに `recent_sales`（差金決済防止）と保有中銘柄の除外があり、
残った銘柄だけが MA クロス判定（5日MA>25日MA・25日MA上向き・出来高サージ）に進む。

## 過去の診断実績（基準値として使う）

### 2026-06-30 診断（当時: 予算¥200k・価格上限¥1,000）

- 203 有効銘柄 → 通過 **1銘柄**。逐次脱落: market_cap -47 / liquidity -90 / pbr -30 /
  equity_ratio -4 / price -30。
- 単独通過率: 時価総額 155/203、**流動性 79/203**、PBR 98/203、自己資本 146/203、
  **価格¥100〜1,000 75/203**。最も絞るのは流動性と価格上限で、両者は相関（安い小型株は低流動）。
- **真因は価格上限¥1,000**: ファンダ全通過31銘柄のうち30銘柄が¥1,000超（¥1,140〜¥6,044）。
  市場上昇で優良銘柄が価格帯から抜けた（バグではない）。

### 2026-07-05 以降（予算¥600k・価格上限¥3,000）

- 上限¥3,000 化により通過数は回復しているはず。通過が再び 0〜2 に落ちたら
  「市場上昇で¥3,000 超え銘柄が増えた」再発を疑い、同じ手法で診断する。

## 診断の実施方法（過去に使った手順）

1. 通過数の時系列は Slack のラン通知（「スクリーニング通過: N銘柄」）を数日分並べる。
2. フィルタ別内訳は GHA ログの `Screener filter counts` 行（gh 未認証のためオーナーに依頼）。
3. 過去期間の日次通過数は `scripts/backtest.py --verbose` で再現できる
   （キャッシュ範囲 2022〜2025 のみ。`--price-max` で仮定を変えて感度も見られる）。

## 重要な但し書き

- しきい値（流動性・PBR・価格上限）は Walk-Forward Robust 判定の前提。
  **緩和・変更は必ず param-change Skill（WF 再検証）経由**。診断だけでフィルタを触らない。
- バックテスト実測では、日次通過6〜8銘柄あってもシグナルは稀（月4〜6件が正常水準）。
  「シグナルなし＝故障」ではない。
