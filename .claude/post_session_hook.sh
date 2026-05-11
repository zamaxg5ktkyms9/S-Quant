#!/usr/bin/env bash
set -euo pipefail

REPO="/Users/tozawanobuharu/Desktop/GoogleDrive/04 dev/claude/S-Quant"
cd "$REPO"

# Nothing changed → skip silently
if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  echo '{"systemMessage": "変更なし — テスト・pushをスキップ"}'
  exit 0
fi

# Run tests
if .venv/bin/python -m pytest tests/ -q --tb=short 2>&1; then
  git add -A
  git commit -m "auto: post-session test+push $(date '+%Y-%m-%d %H:%M JST')"
  git push origin master 2>&1
  echo '{"systemMessage": "✅ テストPASS → pushしました"}'
else
  echo '{"systemMessage": "❌ テストFAIL → pushを中止しました。テスト結果を確認してください"}'
  exit 1
fi
