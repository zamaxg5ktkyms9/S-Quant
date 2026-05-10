"""Retry decorators for external I/O calls."""

import logging
from collections.abc import Callable
from typing import Any

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


def with_retry(
    max_attempts: int = 3,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    min_wait: float = 1.0,
    max_wait: float = 10.0,
) -> Callable[..., Any]:
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
