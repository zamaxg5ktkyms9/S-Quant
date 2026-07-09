#!/usr/bin/env python3
"""Post the last assistant message of a Claude Code session to Slack.

Invoked by the Stop hook so that, when away from the Mac, the human can read
Claude's final reply / findings on their phone via the existing Slack channel.

Reads the hook payload (JSON) on stdin to find the session transcript, extracts
the last assistant text, and posts it to Slack.

Destination (2026-07-09 改定):
- SLACK_REPORT_CHANNEL_ID + SLACK_BRIDGE_BOT_TOKEN が .env にあれば、
  bot の chat.postMessage で**専用レポートチャンネル**へ送る（トレード通知と分離）。
- 無ければ従来どおり SLACK_WEBHOOK_URL（トレードチャンネル）へフォールバック。

Stdlib only — no third-party deps. Fails silently (exit 0) so it never blocks
the session from ending.
"""
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_CHARS = 2800  # Slack practical limit per message block


def load_env_key(key: str) -> str | None:
    env_path = os.path.join(REPO, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return os.environ.get(key)


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


def _truncate(text: str) -> str:
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + "\n…（以下省略。全文はMacのセッションで確認）"
    return text


def post_webhook(webhook: str, text: str) -> None:
    body = json.dumps({"text": f":robot_face: *Claude セッション完了サマリ*\n\n{_truncate(text)}"}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def post_channel(token: str, channel: str, text: str) -> bool:
    """chat.postMessage で専用チャンネルへ。成功時 True（失敗時は webhook にフォールバック）。"""
    body = json.dumps({
        "channel": channel,
        "text": f":robot_face: *Claude セッション完了サマリ*\n\n{_truncate(text)}",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return bool(resp.get("ok"))


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    tp = find_transcript(payload)
    if not tp:
        return 0
    text = last_assistant_text(tp)
    if not text:
        return 0

    # 1) 専用レポートチャンネル（設定時のみ）
    token = load_env_key("SLACK_BRIDGE_BOT_TOKEN")
    report_channel = load_env_key("SLACK_REPORT_CHANNEL_ID")
    if token and report_channel:
        try:
            if post_channel(token, report_channel, text):
                return 0
        except Exception:
            pass  # フォールバックへ

    # 2) フォールバック: 従来の webhook（トレードチャンネル）
    webhook = load_env_key("SLACK_WEBHOOK_URL")
    if webhook:
        try:
            post_webhook(webhook, text)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
