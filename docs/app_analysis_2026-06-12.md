# S-Quant アプリケーション分析レポート

| | |
|---|---|
| **分析日** | 2026-06-12 |
| **分析対象** | S-Quant（日本株自動スクリーニング・シグナル検出システム） |
| **分析範囲** | README / docs（requirements, design, backtest_report, operational_notes）/ バックテスト結果 JSON / コード構成 / 未コミット変更 |
| **作成** | Claude (Anthropic) |

---

## 1. エグゼクティブサマリー

S-Quant は東証小型株を対象とした**半自動スイングトレードシステム**である。シグナル検出は GitHub Actions で毎営業日 20:15 JST に自動実行され、発注はオペレーターが手動で行う。

プロジェクトは A1（押し目モメンタム・単一銘柄）→ B（押し目・2銘柄分散）→ C（MAクロス・2銘柄分散）と**3世代の戦略イテレーション**を経て、C 戦略が Walk-Forward 三窓検証で 2/3 Robust ✅ を獲得。**Phase 1 paper trading（¥200,000・2銘柄分散）は 2026-06 上旬に運用入り済み**（cron 20:30 JST、複数 Position 対応の application 層はコミット `eb56853`→`1e91389` で実装完了）。

**総合評価: 検証プロセスは模範的、戦略のエッジは「あるが薄い」、コードは運用入り済みだが docs のステータス記述が実装に追いついていない。**

- ✅ 過剰最適化を Walk-Forward で自力検出し、戦略を撤退・転換した意思決定プロセスは定量的で健全
- ⚠️ C 戦略の OOS 平均月リターン +0.28% は薄く、2023年型レジームでは負ける（W1 FAIL）ことが既知
- ⚠️ requirements.md / design.md の冒頭バナーは「application 層実装が残作業」のままだが、実際は実装・運用開始済み。docs と実態の乖離が複数ある（§4.3）

---

## 2. システム概要

### 2.1 アーキテクチャ

クリーンアーキテクチャ風の3層構成（src/squant 全体で約6,000行、テスト220件）:

```
application/  daily_runner（状態機械） + idle/holding/settling の3パイプライン
domain/       screener, signal_engine, ranking, position_manager, quantity_calculator,
              circuit_breaker, indicators
infrastructure/ jquants_client（レートリミッタ内蔵）, yfinance フォールバック,
              sheets_client/repository（状態永続化）
config/       constants（全パラメータ）, settings（環境変数）
```

設計上の特徴:

- **状態は Google Sheets に永続化**（IDLE ⇄ ACTIVE → SETTLING の状態機械）。DB レスで運用コストが低い反面、Sheets が単一障害点
- **ドメイン層がインフラから独立**しており、バックテスト（scripts/backtest.py）が本番と同じ screener / signal_engine / position_manager を呼ぶ構造。バックテストと実運用のロジック乖離リスクが小さいのは良い設計
- DRY_RUN モード、execution_time_guard（20:00 JST 前のローカル実行スキップ）、決算ブラックアウト等の運用ガードが揃っている

### 2.2 現行戦略（C 戦略: 5×25日 MA クロス）

| 要素 | 内容 |
|---|---|
| シグナル | ① 5日MA > 25日MA ② 25日MA が20日前より上 ③ 出来高 > 20日平均×1.2 |
| 思想 | 「下がっている中で買う」（押し目）→「上がり始めを確認して買う」（トレンドフォロー）への転換 |
| ポジション | Phase 1: ¥200,000・最大2銘柄・動的予算（残キャッシュ÷残スロット） |
| 出口 | OCO（利確+6% / 損切り-2.5%）+ ATRトレーリング + 5営業日タイムストップ |
| リスク上限 | 1銘柄 -2.5%（約¥2,500）、ポートフォリオ累積 -15%（¥30,000）でサーキットブレーカー |

---

## 3. バックテスト結果の分析

### 3.1 戦略イテレーションの軌跡（OOS 2023 で公正比較）

| 戦略 | OOS Monthly | OOS PF | OOS MaxDD | 判定 |
|---|---|---|---|---|
| A1 押し目・単一銘柄 | -1.27% | 0.43 | -15.1% | ❌ 撤退 |
| B 押し目・2銘柄分散 | -0.66% | 0.61 | -8.9% | ❌ 撤退 |
| C MAクロス・2銘柄分散 | -0.54% | 0.85 | -12.5% | ❌（ただし他窓で✅） |

PF 0.43 → 0.61 → 0.85 と各改善ステップ（分散化・シグナル転換）の寄与が分離計測されており、**「何が効いたか」を説明できる形でイテレーションされている**点が優れている。

### 3.2 C 戦略の Walk-Forward 三窓統合（採用根拠）

| Window | IS → OOS | OOS Monthly | OOS PF | OOS MaxDD | Robust |
|---|---|---|---|---|---|
| C-W1 | 2022 → 2023 | -0.54% | 0.85 | -12.5% | ❌ |
| C-W2 | 2023 → 2024 | +0.54% | 1.21 | -13.0% | ✅ |
| C-W3 | 2024 → 2025 | +0.84% | 1.36 | -7.4% | ✅ |

**2/3 窓 Robust → verdict ✅ 確定**（基準: OOS PF ≥ 1.0 かつ月リターン ≥ +0.1%、過半数窓）。

特筆すべきは C-W2: **IS (2023) で PF 0.94 と赤字なのに OOS (2024) で PF 1.21** という「過剰最適化の逆パターン」が出ており、A1 で見られた「IS で勝ち OOS で負ける」典型的オーバーフィットとは質的に異なる。IS ベストパラメータも三窓で近接領域（TP 0.04〜0.06、ATR 2.0〜2.5、TS 3〜5）に収束しており、構造的優位性の根拠として一定の説得力がある。

### 3.3 残るリスクと弱点

1. **エッジが薄い**: OOS 平均月 +0.28%（Robust 窓のみで +0.69%）。Phase 3（¥1M）換算でも年 ¥82,800 で、下方修正後の目標 ¥100k にすら約20%未達。スリッページ・執行遅延ゼロの前提なので実測はさらに下振れし得る
2. **レジーム依存が既知**: 2023年型（横ばい・難レジーム）では月 -0.54% で負ける。3窓中1窓は負け年という前提でのサーキットブレーカー運用が必須
3. **W2/W3 の JSON verdict は単窓実行のため「⚠ Inconclusive」表記**: 三窓統合判定はレポート上の手動集計。統合スクリプトでの一括再実行（backtest_report.md §8.10 次ステップ4）が未了
4. **出口分布の偏り**: C-W3 OOS では TIME_STOP が 61件中31件（51%）。利確 +6% に到達するのは5件のみで、損益の源泉が「タイムストップ時の微益」に寄っている。TP/TS パラメータの感度が今後の改善余地
5. **データ起因の限界（レポート自身が明記）**: 生存バイアス（ユニバースは2026-05スナップショット）、ファンダ指標の期中固定、スリッページ・流動性制約の未モデル化。実リターンはバックテストより低い方向にバイアスがかかる

### 3.4 検証プロセスの評価（強み）

このプロジェクトで最も価値が高いのは戦略そのものより**検証規律**である:

- フル期間 Grid Search で PF 1.20 が出た時点で採用せず、Walk-Forward で過剰最適化を自力検出して撤退（A1）
- 同日にストップと利確が両方触れた場合は損失側を優先計上する保守的バックテスト
- 失敗の運用知見（並列実行の pickle I/O 競合、ETA 膨張、時間推定の伝え方）を operational_notes.md に恒久ルール化し、ETA 自動 abort などをコードに反映済み（9時間放置 → 56分で自動停止の実績）

---

## 4. 実装状態と残作業

### 4.1 実装状態（コード検証済み・2026-06-12 時点）

requirements.md / design.md の冒頭バナーは「残作業: application 層の複数 Position 対応」と記載しているが、**これは古い**。git 履歴とコードで以下を確認:

- 複数 Position 対応は 3 コミットで完了済み: `eb56853`（domain+app 基盤）→ `ca7d2da`（pending_signals/settle_dates 複数化）→ `1e91389`（sheets 複数行スキーマ + slack 複数銘柄サマリ + テスト）
- `Settings` のデフォルトが `max_positions=2` / `signal_strategy="ma_cross"` で C 戦略が本番系の既定値
- GitHub Actions cron は `30 11 * * 1-5`（20:30 JST、スケジュール遅延対策で 11:15 から移動済み）、workflow_dispatch にテスト用バイパス入力あり
- テストは 220 件（backtest_report.md 記載の 184 件から増加）

つまり **Phase 1 paper trading は技術的に開始可能な状態（運用入り済み）** であり、現在の主活動は実測データの蓄積と検証基盤の高速化に移っている。

### 4.2 未コミットの作業（進行中）

ワーキングツリーに backtest.py +179行 / grid_search.py +46行 の変更がある。内容は **バックテストのライブラリ化（in-process 化）**:

- `backtest.run_one_backtest()` / `load_cache()` を新設し、subprocess 起動と 33MB pickle の再ロードを排除（想定 10〜20倍高速化）
- `_apply_param_overrides` の冪等化（in-process ループでのモンキーパッチ多重ラップ防止）
- grid_search.py に `run_one_inprocess()` ブリッジ追加

これは backtest_report.md §8.9 で「W2 IS が 9.5時間」と特定された**最大の検証ボトルネック（subprocess 方式）への対策**であり、次ステップ候補2に合致する正しい投資。ただし walk_forward.py 側の接続と検証はまだ見えず、作業途中の状態。

### 4.3 既知の技術的負債

| 項目 | 内容 | 影響 |
|---|---|---|
| バックテストの非線形な遅さ | 期間依存で 8s〜187s/combo（`adj_close_full.loc[:str(today)]` スライスコピー疑い） | in-process 化だけでは解消しない可能性。プロファイリング推奨 |
| `_RateLimiter` のプロセスローカル性 | 並列 subprocess で合計 RPM が J-Quants 制限超過し得る（Appendix D-1） | キャッシュ未生成期間での並列実行時のみ顕在化 |
| docs と実装の乖離 | ① requirements.md / design.md 冒頭バナーの「残作業: application 層実装」は完了済みで古い ② design.md の「サーキットブレーカー 30%」と requirements.md の「15%」が不一致 ③ design.md 後半に Phase 1 ¥100,000 表記が残存（¥200,000 に増額済み） ④ design.md のバックテスト実測表が旧 A1 数値のまま ⑤ backtest_report.md のテスト数 184 件は現在 220 件 | 運用ミスと判断ミスの種。docs を実装状態に同期させる一括更新が必要 |
| Sheets 単一障害点 | 状態永続化が Google Sheets のみ | 障害時に状態復元手段がない。Phase 2 以降でローカルバックアップ検討余地 |

---

## 5. 推奨アクション（優先順）

1. **【P0】docs を実装状態に同期させる** — requirements.md / design.md の「残作業: application 層実装」バナーを削除し paper trading 運用中の実態を反映。サーキットブレーカー率（30%/15% の不一致）・design.md 残存の ¥100k 表記・旧 A1 実測表も C 戦略採用後の正に統一
2. **【P1】未コミットの in-process 化を完成・コミット** — walk_forward.py への接続、subprocess 経路との結果一致検証（同一パラメータで metrics が一致すること）をテスト化してからマージ
3. **【P1】rolling 3窓 WF を統合スクリプトで一括再実行** — 現状の「単窓3回 + 手動集計」を1コマンドで再現可能にし、verdict の Inconclusive 表記を解消。in-process 化が済めば現実的な実行時間（推定 1〜2時間 → 数分〜数十分）になる見込み
4. **【P1】paper trading の実測を requirements.md §利益目標の判定基準（実運用2〜3ヶ月で月平均 > +0.4%）で機械的に評価** — バックテストのエッジが薄いため、実測での裏取りが戦略の生死を決める。Phase 2 移行判断の目安は 2026-08〜09 月
5. **【P2】TIME_STOP 偏重の出口分布を分析** — OOS で出口の半分がタイムストップという事実は、TP +6% が遠すぎるか保有期間が短すぎる可能性を示唆。in-process 化後の高速 grid で TP/TS の再感度分析

---

## 6. 結論

S-Quant は「個人の小資本システムトレード」として**異例に規律の高い検証プロセス**を持つプロジェクトである。グリッドサーチの好成績を鵜呑みにせず Walk-Forward で2世代の戦略を撤退させ、改善寄与を分離計測しながら C 戦略に到達した過程は再現性のある方法論として確立している。

一方で、確認されたエッジは薄く（OOS 平均月 +0.28%、Robust 窓でも +0.69%）、負けるレジームの存在も既知である。したがって本システムの当面の価値は「利益額」ではなく、**Phase 1 paper trading で実測データを取得し、バックテストとの乖離を計測すること**にある。リスク上限（1銘柄 ¥2,500、全体 ¥30,000）は明確に制御されており、その学習コストとしては妥当な設計と評価する。

---

## Appendix: 参照ファイル

- [README.md](../README.md) — システム概要・セットアップ
- [docs/requirements.md](requirements.md) — 投資方針・段階拡大プラン（2026-05-29 C 戦略採用版）
- [docs/design.md](design.md) — C 戦略シグナル・出口ルール・分散構造
- [docs/backtest_report.md](backtest_report.md) — A1/B/C 全イテレーション記録（§8.3〜8.10）
- [docs/operational_notes.md](operational_notes.md) — 長時間処理の恒久対策
- [docs/backtests/walkforward_single_C_W1.json](backtests/walkforward_single_C_W1.json) / [W2](backtests/walkforward_single_C_W2.json) / [W3](backtests/walkforward_single_C_W3.json) — C-WF 生データ
- 未コミット変更: scripts/backtest.py（+179行）, scripts/grid_search.py（+46行）— in-process 化（進行中）
