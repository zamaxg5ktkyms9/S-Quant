# 運用ノート — 既知の落とし穴と恒久対策

長時間処理（バックテスト・Walk-Forward・Grid Search）で繰り返し発生した
ハマりどころと、その回避策を集約する。**ここに書いてある制約は守ること**。

## 0. 【2026-07-03】in-process 経路がデフォルトになった

`grid_search.py` / `walk_forward.py` は **`--mode inprocess`（デフォルト）** でキャッシュを
1回だけロードし、日次候補を precompute してグリッド全セルで再利用する。
subprocess を一切起動しないため、**§1〜3 の落とし穴（pickle I/O 競合・subprocess timeout・
並列詰まり）は in-process モードでは発生しない**。§1〜3 は `--mode subprocess`
（同値性検証用に温存）を使う場合のみ適用される。

- 実測: 8.12s/combo（subprocess）→ **1.41s/combo（in-process + precompute、5.8x）**。
  遅い期間（2022年 = 187s/combo）ほど効果大（1窓 9.5h → 推定 10分前後、実測は未取得）
- 同値性は `tests/integration/test_inprocess_equivalence.py` で保証
  （subprocess vs in-process vs precompute の metrics 完全一致）
- in-process はシリアル実行（`_apply_param_overrides` がモジュール定数を書き換えるため
  スレッド安全ではない）。`--workers` は subprocess モードのみ有効
- ETA 監視・`--max-wall-clock-seconds`・`--benchmark` は両モード共通で機能する

## 1. `walk_forward.py` / `grid_search.py` の並列実行（`--mode subprocess` のみ）

### 既知の失敗パターン

| 日付 | 設定 | 症状 | 経過時間 |
|---|---|---|---|
| 2026-05-25 朝 (A1) | `--workers 4` | 最初 60 combos は 60秒で進むが、その後 30 combos に 56分。subprocess timeout 多発 | 中断時点で 1時間20分 / 想定 16分 |
| 2026-05-26 朝 (B-WF 1回目) | `--workers 4` | W1 IS が 130分（最後の 30 combos が遅い）、W2 IS は 90/180 で 6.8時間 | 中断時点で 9時間 / 想定 20分 |

### 真因

- `.backtest_cache/data_*.pkl` (約33MB) を **複数サブプロセスが同時にロード**
- macOS の `spawn` 起動オーバーヘッド × 並列度
- 結果として subprocess の実行時間が 9秒 → 120秒超まで膨れ、`subprocess.run(timeout=120)` で N/A になる組合せが量産される

### 必須ルール

- **デフォルトは `--workers 1` （シリアル実行）** にする
- `--workers 2` までは許容（pickle ロードの I/O 競合は中程度）
- `--workers 4` は **明示的に「速度優先で詰まるリスク受容」と判断したときのみ**
- `--subprocess-timeout` は `60` を推奨（速い組合せは数秒、遅い組合せは打ち切ってよい）

## 2. 大規模実行前のベンチマーク

### 失敗パターン

「単発 9秒 × 540 combos / 4並列 = 約 20分」の単純計算で本実行を開始
→ 実態は数時間〜数日

### 必須ルール

- **180 combos × 3窓 以上の規模を回す前に、必ず `--benchmark` で実測**
- `--benchmark` は 10 combos × 1窓 で実行時間を計測し、本実行の所要時間を実測ベースで返す
- ベンチ結果が「想定の 2倍以上」なら **設定を見直してから本実行**

## 3. 全体タイムアウトと ETA 監視

### 失敗パターン

`subprocess.run(timeout=120)` で個別 subprocess は止まるが、`walk_forward.py` 全体の
タイムアウトがなく、9時間放置されることがあった。

### 必須ルール

- `walk_forward.py --max-wall-clock-seconds N` で全体上限を指定
- デフォルト 3600 秒（1時間）。1時間を超える本実行は明示的に指定する
- 進捗 10% 時点で実 ETA を再計算し、初期推定の **2倍を超えたら警告**、**5倍を超えたら abort**

## 4. 推定値の伝え方（コミュニケーション）

### 失敗パターン

「20分で終わる」と断言 → 実際は数時間 → ユーザーが期待値を持って指摘するまで放置

### 必須ルール

- **時間推定は必ず幅で表現**: 「最良 20分 〜 最悪 2時間（並列I/O次第）」
- 過去に同種実行で失敗していれば、**その経緯を明示**: 「A1 で同じ条件で 9時間かかったので、今回はベンチ→本実行とする」
- 推定の根拠（単発時間 × パターン数 / 並列数 など）を**式で出す**

## 5. バックテスト本体の構造的遅さ

### 既知の事実

- `backtest.py` のメインループ（`_run_loop` 内の `_process_signal_scan`）に O(N²) 的な遅さがある可能性
- フル期間 4年 で 4時間以上、1年期間で 8秒。期間に対して線形ではない

### 対応状況（2026-07-03 更新）

- **グリッド/WF 用途は §0 の precompute で解消済み**（スキャンをセル間で共有）
- 単発 CLI 実行（`python scripts/backtest.py`）は従来どおりスキャンが走る。
  1年期間なら数秒〜3分程度で収まるが、フル期間（4年）の直接実行は引き続き避ける
- 残改修候補: `adj_close_full.loc[:str(today)]` のスライスコピー削減、
  `ohlcv_sig` 組み立ての `pd.concat` 化（断片化 PerformanceWarning）

## 6. キャッシュファイル

### ファイル名規約

`.backtest_cache/data_{fetch_start}_{end}.pkl`

- `fetch_start` = `start - 180日`（インディケータ ウォームアップ用）
- `end` = バックテスト終了日

### 再利用ルール

- `backtest.py` は **必要期間をカバーするキャッシュ** を自動検出して再利用する
- 新しい期間で初回実行する場合、キャッシュ生成に **30分〜数時間** かかることがある
- キャッシュ生成は **必ず単独で実行**（並列化しない）

## 7. メモリ参照と自動チェック

過去の失敗を `/Users/tozawanobuharu/.claude/projects/.../memory/` の
`project_backtest_*.md` に記録しているが、**実行時に自動で参照されない**。

### 必須ルール

- 長時間処理を起動する直前に、必ずこのファイル (`docs/operational_notes.md`)
  と `project_backtest_*.md` を確認する
- 「過去にこの設定で詰まった」事実を踏まえて、**安全な設定から開始**する

## 8. パラメータ・戦略の採用判断ルール（2026-07-10 追加・独立レビュー F-3 対応）

### 失敗パターン（実例）

2026-07-04〜05 の noTP/ATR3.0/TS5 採用: 「目標未達（+0.64%）」→ グリッド拡張 →
全期間成績で候補選択 → 採用、という**目標駆動の in-sample 選択**を約7時間で実施。
採用値の 4年平均 +1.40% は選択に使ったデータ上の値であり、採用時点では
未使用データでの検証がなかった（採用値が必要値 +1.39% をわずかに上回るのは
この手順の帰結）。独立レビュー（docs/review_2026-07-10_fable.md）が指摘。

### 必須ルール

1. **採用候補の選択に使ったデータで、その候補の期待値を語らない**。
   選択に使った期間の成績は「参考」。期待値の根拠は**選択後に初めて触れる
   未使用期間**（真正 OOS）の成績のみ。
2. パラメータ・戦略の本番採用前に、**未使用期間での固定値検証を必須**とする
   （選定後期間での検証、または選定から時間を置いた前向き検証）。
3. 「目標に届く候補を探す」形の探索をしたら、その旨を明記する（多重検定の告白）。
   目標値と採用値が近接しているときは特に疑う。
4. 約定モデル・コスト仮定を変えたら、**過去の採用判断の数字はすべて旧モデル値**として
   扱い、正準数字を再計測してから比較する（2026-07-10 のギャップ約定現実化が実例）。

---

## 9. Mutation testing（テスト品質の定量化・V-3 検証強化パッケージ）

「テストが 341件 PASS」は「テストが通る」ことしか保証しない。資金計算ロジックの
テストが**実際にバグを検出できるか**を mutation testing で定量化する。対象は
`src/squant/domain/` の資金系5ファイルに限定（circuit_breaker / position_manager /
quantity_calculator / signal_engine / slippage）。

**CI には組み込まない**（実行時間が長く、等価変異の判定に人手が要るため）。
四半期ごと、または domain 層の資金ロジックを変更したときにローカルで手動実行する。

### ローカル実行手順

```bash
# 依存は dev extras に含まれる（mutmut, hypothesis）
pip install -e ".[dev]"

# 対象・テスト選択は pyproject.toml の [tool.mutmut] に定義済み。
# source_paths=src/squant（editable+src レイアウトのため package 全体をコピーする必要がある）、
# only_mutate で上記5ファイルに限定、テストは tests/unit/domain のみ選択。
rm -rf mutants                 # 前回の変異ツリーを破棄（クリーン計測）
TERM=dumb mutmut run           # 全変異を生成しテスト実行（数分）

# 結果の読み方
mutmut results                 # survived / no-tests の一覧（killed は表示されない）
mutmut show <mutant_name>      # 個別の変異 diff を表示
```

`mutmut run` は `mutants/` に一時ツリーを作る（.gitignore 済み・コミット禁止）。

### kill rate の見方と判断基準

- kill rate = killed /（killed + survived）。no-tests は分母から除く。
- **資金クリティカル4ファイル（signal_engine を除く）を最重要指標とする**。
  signal_engine は診断カウンタ（`dropped[...] += 1`）とログ f-string の変異が
  大量に生き残るが、これらは返り値（Candidate リスト）に影響しない**等価変異**で、
  kill rate を機械的に押し下げるだけ。資金保全の観点では4ファイルを見る。
- 2026-07-11 V-3 実施時点の基準値（テスト追加後）:
  circuit_breaker / slippage = 100%、quantity_calculator ≈ 94%、
  position_manager ≈ 80%。domain 全体（signal_engine 込み）≈ 55%。
- **資金4ファイルの kill rate がこの基準を下回ったら、テストの穴を疑う**。

### 生き残りを「実害あり/軽微/等価」に分類する

すべての survived を潰す必要はない。3分類して判断する:

1. **実害あり**（資金計算・出口判定・CB 発動が狂う）→ テストを追加して kill する。
2. **実害軽微**（人間可読な note 文字列、決済枝で捨てられる返り値、本番無効な
   TP 経路の表示 round 等）→ 記録して残してよい。
3. **等価変異**（意味的に元コードと同一。例: `qty <= 0` → `qty <= 1` は qty が
   常に単元100株の倍数ゆえ到達不能／`spread_rate == 0` → `== 1` は spread=0 の
   本番では同値）→ 残す。テストで殺そうとしない。

判断に迷う変異は「よりリスクの低い方」＝テストを足す側に倒す。

---

## 10. 日次パリティ照合（V-1 検証強化パッケージ）

`scripts/parity_check.py` が毎晩（Daily Parity Check workflow、火〜土 01:20 JST）
直近の本番 success ラン日 D についてバックテストエンジン（モデル）を影実行し、
本番が Sheets に記録した実挙動と照合する。Sheets は読み取りのみ・本番影響ゼロ。

### 照合ドメインと severity

| ドメイン | 照合内容 | alert になる条件 |
|---|---|---|
| exit | 保有ポジションをエントリー日から D までモデル再生（ザラ場 OCO・F-2 ギャップ考慮約定）し、本番の HOLD/EXIT・トレーリング値・決済理由/価格と照合 | 決済判断の不一致（モデル EXIT vs 本番 HOLD 等）、トレーリング乖離 > 1% |
| entry | D エントリー分の寄付約定・ギャップ見送り判定 | モデルが「見送り」なのに実エントリー |
| scan | IDLE スキャン日のみ。独立に J-Quants 再取得して本番スクリーニングを再実行し、funnel_log の4件数 + pending_signals の銘柄・参照価格を照合 | 件数・銘柄・参照価格の不一致 |

- 結果は `parity/parity_log.csv` に全行追記（GHA が bot コミット）。alert があった日だけ
  Slack 通知 + workflow 赤。info は既知の定義差の記録（例: モデルはザラ場高値、
  本番は終値で highest を更新するためトレーリングは常に微小ドリフトする）。
- **「モデル EXIT vs 本番 HOLD」alert の意味**: 本番夜ランは終値でしか出口判定しないが、
  SBI の実逆指値はザラ場で約定する。この alert は「実際には売れているのに帳簿が
  HOLDING のまま」の可能性を示す → オーナーに SBI 約定履歴の確認を依頼し、約定して
  いれば **`scripts/record_manual_exit.py`**（dry-run プレビュー → `--apply`）で
  帳簿本体（trades/recent_sales/CB/portfolio/slippage）を実約定価格で反映する。
  夜ランが既に出口検知済み（trades に SELL 行あり）の場合のみ `confirm_exit.py`
  （記録のみ）を使う。実例: 2026-07-10 の 2201.T — 初回パリティで検出、
  実約定 ¥2,663（想定ストップ比 -50.7bps 有利）で帳簿反映済み。
- scan parity の所要はユニバース全取得（OHLCV+ファンダ）を伴うため長い。保有中/CB 日は
  exit/entry のみで数十秒。手動実行: `python scripts/parity_check.py --dry-run`（記録なし）、
  `--date YYYY-MM-DD` で過去日、`--force-scan` で funnel 行が無い日の scan 実測。

---

## 改訂履歴

- 2026-05-26: 初版作成（A1 / B-WF 1回目の失敗を受けて）
- 2026-07-03: §0 追加（in-process + precompute がデフォルト化、§1〜3 は subprocess モード限定に）、§5 更新
- 2026-07-10: §8 追加（独立レビュー F-3: 採用判断の in-sample 選択バイアス対策）
- 2026-07-11: §9 追加（V-3 mutation testing のローカル実行手順・kill rate 判断基準・変異3分類）
- 2026-07-12: §10 追加（V-1 日次パリティ照合の読み方・alert の意味・手動実行手順）
