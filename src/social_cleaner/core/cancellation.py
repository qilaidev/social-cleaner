"""Cooperative cancellation for long-running runs.

A run is a chain of browser actions and sleeps. Signal handlers cannot safely
tear those down mid-flight, so they only flip a flag here; every loop and
every sleep checks the flag and raises :class:`CancellationError` at the next
safe point. The runner then writes its summary and closes the browser.
"""

from __future__ import annotations

from .errors import CancellationError

_reason: str | None = None


def request_cancellation(reason: str | None = None) -> None:
    global _reason
    _reason = reason or "operation cancelled"


def is_cancellation_requested() -> bool:
    return _reason is not None


def raise_if_cancelled() -> None:
    if _reason is not None:
        raise CancellationError(_reason)


def reset_for_tests() -> None:
    global _reason
    _reason = None


def is_cancellation_error(error: BaseException) -> bool:
    return isinstance(error, CancellationError)
