#!/usr/bin/env python3
"""Post the last assistant message of a Claude Code session to Slack.

Invoked by the Stop hook so that, when away from the Mac, the human can read
Claude's final reply / findings on their phone via the existing Slack channel.

Reads the hook payload (JSON) on stdin to find the session transcript, extracts
the last assistant text, and POSTs it to SLACK_WEBHOOK_URL (from .env).
Stdlib only — no third-party deps. Fails silently (exit 0) so it never blocks
the session from ending.
"""
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_CHARS = 2800  # Slack practical limit per message block


def load_webhook() -> str | None:
    env_path = os.path.join(REPO, ".env")
    if not os.path.exists(env_path):
        return os.environ.get("SLACK_WEBHOOK_URL")
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("SLACK_WEBHOOK_URL="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return os.environ.get("SLACK_WEBHOOK_URL")


def find_transcript(payload: dict) -> str | None:
    tp = payload.get("transcript_path")
    if tp and os.path.exists(tp):
        return tp
    sid = payload.get("session_id") or payload.get("sessionId")
    if not sid:
        return None
    # ~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl
    sanitized = REPO.replace("/", "-")
    cand = os.path.expanduser(f"~/.claude/projects/{sanitized}/{sid}.jsonl")
    return cand if os.path.exists(cand) else None


def last_assistant_text(transcript_path: str) -> str:
    text = ""
    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message", {})
            content = msg.get("content", [])
            parts = []
            if isinstance(content, str):
                parts.append(content)
            else:
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
            joined = "\n".join(p for p in parts if p).strip()
            if joined:  # keep overwriting -> ends on the final assistant text turn
                text = joined
    return text


def post(webhook: str, text: str) -> None:
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…（以下省略。全文はMacのセッションで確認）"
    body = json.dumps({"text": f":robot_face: *Claude セッション完了サマリ*\n\n{text}"}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    webhook = load_webhook()
    if not webhook:
        return 0
    tp = find_transcript(payload)
    if not tp:
        return 0
    text = last_assistant_text(tp)
    if not text:
        return 0
    try:
        post(webhook, text)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
