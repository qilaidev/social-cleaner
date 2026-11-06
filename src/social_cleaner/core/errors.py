"""Error taxonomy shared by the platforms.

The retry helper in :mod:`social_cleaner.core.retry` and the platform
runners key their decisions on these classes, so a platform that raises them
gets the right behaviour for free:

- :class:`RetryableError` — try again after a delay.
- :class:`RateLimitError` — retryable, but the platform told us to slow down;
  carries an optional ``retry_after_ms``. Runners abort the run on it because
  continuing only deepens the limit.
- :class:`NonRetryableError` — stop immediately (account restricted, etc.).
- :class:`CancellationError` — the user asked to stop; never retried.
"""

from __future__ import annotations


class SocialCleanerError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(SocialCleanerError):
    """Invalid or missing configuration."""


class RetryableError(SocialCleanerError):
    """A transient failure worth retrying."""


class NonRetryableError(SocialCleanerError):
    """A failure that retrying cannot fix."""


class RateLimitError(RetryableError):
    """The platform is rate-limiting this account."""

    def __init__(self, message: str, retry_after_ms: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class CancellationError(SocialCleanerError):
    """The current run was cancelled (Ctrl+C, SIGTERM, or programmatically)."""

    def __init__(self, message: str = "operation cancelled") -> None:
        super().__init__(message)
