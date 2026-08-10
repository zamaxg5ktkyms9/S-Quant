#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Drain the Stop-hook payload (JSON on stdin) so the writer never blocks on a
# full pipe. The Slack summariser used to consume it here; that forwarding was
# removed on the owner's request (2026-08-10) — this hook now only runs the
# post-session test+push.
cat >/dev/null || true

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
