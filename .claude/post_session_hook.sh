#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Capture the Stop-hook payload (JSON on stdin) once, then forward it to the
# Slack summariser so the last assistant reply is pushed to Slack — lets the
# human read session results on their phone when away from the Mac. Best-effort:
# never let it block the session from ending.
INPUT="$(cat || true)"
printf '%s' "$INPUT" | .venv/bin/python .claude/session_to_slack.py >/dev/null 2>&1 || true

# Nothing changed → skip silently
if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  echo '{"systemMessage": "変更なし — テスト・pushをスキップ（Slackサマリは送信済み）"}'
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
