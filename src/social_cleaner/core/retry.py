"""Retry with bounded, jittered exponential backoff, plus cancellable sleeps."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from .cancellation import raise_if_cancelled
from .errors import CancellationError, RateLimitError

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SLEEP_TICK_S = 0.2


async def sleep_ms(ms: float) -> None:
    """Sleep ``ms`` milliseconds, waking every 200ms to honour cancellation."""
    raise_if_cancelled()
    deadline = time.monotonic() + max(0.0, ms) / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _SLEEP_TICK_S))
        raise_if_cancelled()


async def random_sleep_ms(minimum: float, maximum: float) -> None:
    """Sleep a uniformly random duration between ``minimum`` and ``maximum`` ms."""
    low = max(0.0, min(minimum, maximum))
    high = max(low, maximum)
    await sleep_ms(random.uniform(low, high))


@dataclass(frozen=True)
class RetryPolicy:
    """How :func:`with_retry` behaves.

    ``max_attempts`` counts the first try: ``1`` means no retries.
    ``jitter_ratio`` widens each delay by ``±ratio/2`` to avoid retry storms.
    ``max_elapsed_ms`` caps the whole loop, including waits.
    """

    max_attempts: int = 3
    delay_ms: float = 1000.0
    exponential_backoff: bool = True
    max_delay_ms: float | None = None
    jitter_ratio: float | None = None
    max_elapsed_ms: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.delay_ms < 0:
            raise ValueError("delay_ms must be >= 0")

    def delay_for(self, attempt: int, error: BaseException | None = None) -> float:
        """Delay in ms before attempt ``attempt + 1`` (``attempt`` is 1-based)."""
        if isinstance(error, RateLimitError) and error.retry_after_ms is not None:
            delay = float(error.retry_after_ms)
            jitter = False
        else:
            delay = (
                self.delay_ms * (2 ** (attempt - 1)) if self.exponential_backoff else self.delay_ms
            )
            jitter = True
        if self.max_delay_ms is not None:
            delay = min(delay, max(0.0, self.max_delay_ms))
        if jitter and self.jitter_ratio:
            delay *= 1 + (random.random() - 0.5) * max(0.0, self.jitter_ratio)
        return max(0.0, delay)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    name: str = "operation",
    retry_on: Callable[[BaseException], bool] | None = None,
) -> T:
    """Run ``fn`` until it succeeds or the policy is exhausted.

    ``retry_on`` decides per error whether another attempt makes sense; errors
    it rejects (and cancellations) propagate immediately.
    """
    started = time.monotonic()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except CancellationError:
            raise
        except Exception as error:
            if retry_on is not None and not retry_on(error):
                raise
            elapsed_ms = (time.monotonic() - started) * 1000
            if policy.max_elapsed_ms is not None and elapsed_ms > policy.max_elapsed_ms:
                raise TimeoutError(
                    f"{name} failed: exceeded max elapsed time ({policy.max_elapsed_ms:.0f}ms)"
                ) from error
            if attempt >= policy.max_attempts:
                logger.error("%s failed after %s attempt(s)", name, attempt)
                raise
            delay = policy.delay_for(attempt, error)
            if policy.max_elapsed_ms is not None and elapsed_ms + delay > policy.max_elapsed_ms:
                raise TimeoutError(
                    f"{name} failed: exceeded max elapsed time ({policy.max_elapsed_ms:.0f}ms)"
                ) from error
            logger.warning(
                "%s failed (attempt %s/%s), retrying in %.0fms: %s",
                name,
                attempt,
                policy.max_attempts,
                delay,
                error,
            )
            await sleep_ms(delay)
    raise AssertionError("unreachable")  # pragma: no cover
