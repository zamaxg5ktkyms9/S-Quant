"""Slack Webhook notifier."""

import httpx

from squant.domain.exceptions import SlackError
from squant.utils.logging import get_logger
from squant.utils.retry import with_retry

logger = get_logger(__name__)


class SlackNotifier:
    def __init__(self, webhook_url: str, dry_run: bool = False) -> None:
        self._url = webhook_url
        self._dry_run = dry_run

    @with_retry(max_attempts=3, min_wait=1.0, max_wait=8.0)
    def send(self, text: str, blocks: list[dict] | None = None) -> None:
        if not self._url:
            logger.warning("Slack webhook URL not configured — skipping notification")
            return
        if self._dry_run:
            text = f"[DRY RUN] {text}"
        payload: dict = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        resp = httpx.post(self._url, json=payload, timeout=10)
        if resp.status_code != 200:
            raise SlackError(f"Slack responded {resp.status_code}: {resp.text[:200]}")

    def send_error(self, title: str, detail: str) -> None:
        prefix = "[DRY RUN] " if self._dry_run else ""
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":red_circle: *{title}*\n```{detail}```"},
            }
        ]
        self.send(text=f"{prefix}[ERROR] {title}", blocks=blocks)
