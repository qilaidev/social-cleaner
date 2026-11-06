from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from social_cleaner.core import audit, cancellation
from social_cleaner.core.errors import (
    CancellationError,
    NonRetryableError,
    RateLimitError,
    RetryableError,
)
from social_cleaner.core.retry import RetryPolicy, sleep_ms, with_retry
from social_cleaner.core.settings import load_settings


def test_settings_defaults_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SOCIAL_CLEANER_DATA_DIR", raising=False)
    monkeypatch.delenv("SOCIAL_CLEANER_AUDIT_LOG_PATH", raising=False)
    s = load_settings()
    assert s.data_dir == Path.home() / ".social-cleaner"
    assert s.audit_log_path == s.data_dir / "audit.jsonl"

    monkeypatch.setenv("SOCIAL_CLEANER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOCIAL_CLEANER_LOG_LEVEL", "debug")
    monkeypatch.setenv("SOCIAL_CLEANER_AUDIT_LOG_ENABLED", "false")
    s = load_settings()
    assert s.data_dir == tmp_path
    assert s.audit_log_path == tmp_path / "audit.jsonl"
    assert s.log_level == "DEBUG"
    assert s.audit_log_enabled is False


def test_audit_entries_carry_platform() -> None:
    audit.record("x", "tweets.delete", "123", ok=True, preview="hi")
    audit.record("x", "tweets.delete", "124", ok=False, error="boom")
    lines = Path(audit.settings.audit_log_path).read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)
    assert (
        first["platform"] == "x" and first["action"] == "tweets.delete" and first["preview"] == "hi"
    )
    assert second["ok"] is False and second["error"] == "boom"


def test_retry_policy_delays() -> None:
    policy = RetryPolicy(max_attempts=3, delay_ms=100, exponential_backoff=True, max_delay_ms=250)
    assert policy.delay_for(1) == 100
    assert policy.delay_for(2) == 200
    assert policy.delay_for(3) == 250  # capped
    assert policy.delay_for(1, RateLimitError("slow", retry_after_ms=5000)) == 250
    assert RetryPolicy(delay_ms=100, exponential_backoff=False).delay_for(5) == 100
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


def test_with_retry_retries_then_succeeds() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RetryableError("again")
        return "ok"

    result = asyncio.run(with_retry(flaky, RetryPolicy(max_attempts=3, delay_ms=0), name="flaky"))
    assert result == "ok" and calls == 3


def test_with_retry_honours_retry_on_and_gives_up() -> None:
    async def fatal() -> None:
        raise NonRetryableError("nope")

    with pytest.raises(NonRetryableError):
        asyncio.run(
            with_retry(
                fatal,
                RetryPolicy(max_attempts=5, delay_ms=0),
                retry_on=lambda e: isinstance(e, RetryableError),
            )
        )

    async def always() -> None:
        raise RetryableError("still")

    with pytest.raises(RetryableError):
        asyncio.run(with_retry(always, RetryPolicy(max_attempts=2, delay_ms=0)))


def test_with_retry_respects_max_elapsed() -> None:
    async def slow_fail() -> None:
        raise RetryableError("x")

    with pytest.raises(TimeoutError):
        asyncio.run(
            with_retry(slow_fail, RetryPolicy(max_attempts=10, delay_ms=50, max_elapsed_ms=10))
        )


def test_cancellation_interrupts_sleep_and_retry() -> None:
    cancellation.request_cancellation("stop")
    with pytest.raises(CancellationError, match="stop"):
        asyncio.run(sleep_ms(10_000))

    async def never() -> None:
        raise CancellationError("stop")

    with pytest.raises(CancellationError):
        asyncio.run(with_retry(never, RetryPolicy(max_attempts=3, delay_ms=0)))
