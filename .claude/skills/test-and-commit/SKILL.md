---
name: test-and-commit
description: コード変更後のテスト→コミット→push の定型フロー。ファイルを変更した作業の締めくくりで必ず使う。「コミットして」「pushして」と頼まれたときも使う。
---

# テスト → コミット → push

## Step 1: 必須ゲート（両方 PASS するまでコミット禁止）

```bash
# ゲート1: 全テスト（実測 6〜7秒 / 最悪1分。235件全PASSが基準）
.venv/bin/python -m pytest tests/ -q --tb=short

# ゲート2: ruff チェック（src/ tests/ は現状 PASS が基準。scripts/ は対象外＝既存15件のエラーあり）
.venv/bin/ruff check src/ tests/

# scripts/ 配下を変更した場合は、変更したファイルだけを個別にチェック
.venv/bin/ruff check scripts/<変更したファイル>
```

- if テスト FAIL → コミットせず修正する。自分の変更と無関係の FAIL なら、その事実と
  出力を添えて Slack 報告し、コミットを保留する。
- if ruff FAIL → `--fix` で直るものは `.venv/bin/ruff check --fix <変更したファイルのみ>`。
  scripts/ の既存15件（backtest.py/grid_search.py/walk_forward.py の F541/N812/B905 等）は
  既知。まとめて直そうとしない。

## 既知の失敗するチェック（ゲートにしない・直そうとしない）

| コマンド | 現状 | 扱い |
|---|---|---|
| `make lint` 内の `ruff format --check` | 32ファイル要整形で FAIL（既知） | 全面整形は**禁止**（巨大diff化）。自分が触ったファイルだけ整形可 |
| `make typecheck`（mypy） | 55エラーで FAIL（既知） | ゲートにしない。新規コードで新たなエラーを増やさない |

## Step 2: コミット

```bash
git status && git diff --stat   # 意図しないファイルが混ざっていないか目視
git add <変更したファイルを明示列挙>
git commit -m "<type>(<scope>): <summary>"
```

- メッセージ規約（git log の実例準拠・英語）:
  `feat:` / `feat(backtest):` / `fix(idle_pipeline):` / `docs:` / `analysis:` / `chore(ops):`
  例: `feat(backtest): add --price-max override + validate budget-expansion option`
- `auto:` プレフィックスは Stop hook 専用。手動コミットで使わない。
- ハーネスが指定する Co-Authored-By トレーラーがあれば付ける。

## Step 3: push

```bash
git push origin master
```

このリポジトリは master 直 push 運用（PR フローなし）。

## Stop hook との関係（重要）

セッション終了時に `.claude/post_session_hook.sh` が自動で
「未コミット変更があれば pytest → `auto: post-session test+push` でコミット → push」する。
つまり**コミットし忘れた変更は無意味なメッセージで自動コミットされる**。
意味のある変更は必ずこの Skill で明示的にコミットしてから作業を終えること。

## 禁止事項

- `git add -A` / `git add .`（意図しないファイル混入。ファイルを明示列挙する）。
- `.env`・`*.key`・`service_account.json` 等の機密を add すること（.gitignore 済みだが、
  `git status` で Untracked に機密らしきファイルが見えたら add せず Slack 報告）。
- `git push --force` / 履歴改変 / `git branch -D`（承認制。CLAUDE.md）。
- テスト FAIL のままのコミット・push。
- リポジトリ全体の一括 format / mypy 一括修正。
