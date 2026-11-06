from __future__ import annotations

import asyncio
from typing import Any

import pytest

from social_cleaner.platforms.x.login import extract_username_from_url, normalize_handle
from social_cleaner.platforms.x.signals import (
    RateLimitDetector,
    blocking_state,
    detect_risk_signal,
    is_blank_render,
    pathname_of,
)


class FakeLocator:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    async def all_text_contents(self) -> list[str]:
        return self._texts

    async def inner_text(self, timeout: int | None = None) -> str:
        return " ".join(self._texts)


class FakePage:
    def __init__(
        self, url: str = "https://x.com/home", body: str = "", html_len: int = 10_000
    ) -> None:
        self.url = url
        self._body = body
        self._html_len = html_len
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator([self._body] if selector == "body" else [])

    async def content(self) -> str:
        return "x" * self._html_len


class FakeResponse:
    def __init__(self, status: int, url: str) -> None:
        self.status = status
        self.url = url


def test_rate_limit_detector_counts_only_api_429s() -> None:
    page = FakePage()
    detector = RateLimitDetector(page)
    assert detector.hit_recently() is False
    page.handlers["response"](FakeResponse(429, "https://x.com/i/api/graphql/Viewer"))
    page.handlers["response"](FakeResponse(429, "https://abs.twimg.com/static.js"))
    page.handlers["response"](FakeResponse(200, "https://x.com/i/api/1.1/home.json"))
    assert detector.count == 1
    assert detector.hit_recently() is True
    detector.reset()
    assert detector.hit_recently() is False


def test_blank_render_needs_real_html_and_empty_body() -> None:
    assert asyncio.run(is_blank_render(FakePage(body="", html_len=100))) is False
    assert asyncio.run(is_blank_render(FakePage(body="", html_len=10_000))) is True
    assert asyncio.run(is_blank_render(FakePage(body="hello", html_len=10_000))) is False


def test_pathname_of() -> None:
    assert pathname_of("https://x.com/i/flow/login?x=1") == "/i/flow/login"
    assert pathname_of("nonsense") == "nonsense"


def test_risk_signal_from_url_text_and_custom_patterns() -> None:
    blocked = asyncio.run(detect_risk_signal(FakePage(url="https://x.com/account/access"), "t"))
    assert blocked is not None and blocked.label == "session-blocked"

    text = asyncio.run(detect_risk_signal(FakePage(body="We detected unusual activity"), "t"))
    assert text is not None and text.label == "account-restricted"

    custom = asyncio.run(detect_risk_signal(FakePage(body="Please slow DOWN"), "t", ["slow down"]))
    assert custom is not None and custom.label == "configured-risk-text"

    assert asyncio.run(detect_risk_signal(FakePage(body="all good"), "t")) is None


def test_blocking_state_prefers_network_429_over_text() -> None:
    page = FakePage(body="")
    detector = RateLimitDetector(page)
    assert asyncio.run(blocking_state(page, detector, rate_limit_backoff_ms=1)) is None
    page.handlers["response"](FakeResponse(429, "https://x.com/i/api/x"))
    state = asyncio.run(blocking_state(page, detector, rate_limit_backoff_ms=123))
    assert state is not None and state.type == "rate_limit" and state.retry_after_ms == 123

    restricted = asyncio.run(
        blocking_state(
            FakePage(body="Your account is temporarily locked: account locked"),
            RateLimitDetector(FakePage()),
            rate_limit_backoff_ms=1,
        )
    )
    assert restricted is not None and restricted.severity == "non_retryable"

    transient = asyncio.run(
        blocking_state(
            FakePage(body="Something went wrong. Try reloading."),
            RateLimitDetector(FakePage()),
            rate_limit_backoff_ms=1,
        )
    )
    assert transient is not None and transient.type == "transient_error"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://x.com/alice", "alice"),
        ("https://twitter.com/alice/status/1", "alice"),
        ("https://x.com/home", None),
        ("https://x.com/i/flow/login", None),
        ("https://x.com/settings/account", None),
        ("https://example.com/alice", None),
        ("", None),
    ],
)
def test_extract_username_from_url(url: str, expected: str | None) -> None:
    assert extract_username_from_url(url) == expected


def test_normalize_handle() -> None:
    assert normalize_handle(" @@Alice ") == "alice"
    assert normalize_handle(None) == ""
