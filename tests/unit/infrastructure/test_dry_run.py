"""Tests for DRY RUN mode in SlackNotifier."""

from unittest.mock import MagicMock, patch

import pytest

from squant.infrastructure.slack_notifier import SlackNotifier

_WEBHOOK = "https://hooks.slack.com/services/TEST"


class TestSlackNotifierDryRun:
    def test_dry_run_prefixes_text(self):
        notifier = SlackNotifier(_WEBHOOK, dry_run=True)
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier.send("テストメッセージ")
            payload = mock_post.call_args.kwargs["json"]
            assert payload["text"].startswith("[DRY RUN]")
            assert "テストメッセージ" in payload["text"]

    def test_no_dry_run_no_prefix(self):
        notifier = SlackNotifier(_WEBHOOK, dry_run=False)
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier.send("テストメッセージ")
            payload = mock_post.call_args.kwargs["json"]
            assert not payload["text"].startswith("[DRY RUN]")
            assert payload["text"] == "テストメッセージ"

    def test_dry_run_send_error_prefixes(self):
        notifier = SlackNotifier(_WEBHOOK, dry_run=True)
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier.send_error("エラータイトル", "詳細")
            payload = mock_post.call_args.kwargs["json"]
            assert "[DRY RUN]" in payload["text"]
            assert "[ERROR]" in payload["text"]

    def test_no_url_skips_send(self):
        """No webhook URL → no HTTP call (even in dry_run=False)."""
        notifier = SlackNotifier("", dry_run=False)
        with patch("httpx.post") as mock_post:
            notifier.send("msg")
            mock_post.assert_not_called()

    def test_dry_run_with_blocks_still_prefixes_text(self):
        notifier = SlackNotifier(_WEBHOOK, dry_run=True)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "body"}}]
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier.send("ブロック付き通知", blocks=blocks)
            payload = mock_post.call_args.kwargs["json"]
            assert payload["text"].startswith("[DRY RUN]")
            assert "blocks" in payload
