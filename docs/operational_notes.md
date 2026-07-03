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

---

## 改訂履歴

- 2026-05-26: 初版作成（A1 / B-WF 1回目の失敗を受けて）
- 2026-07-03: §0 追加（in-process + precompute がデフォルト化、§1〜3 は subprocess モード限定に）、§5 更新
