"""Detecting that X is pushing back.

Three independent signals, from most to least reliable:

1. **HTTP 429 on X's internal APIs** (:class:`RateLimitDetector`). When an
   account is rate-limited, ``Viewer`` / ``GetUserClaims`` and friends return
   429 and the page renders *nothing*: empty body, zero user cells. Text-based
   matching ("rate limit", "请求过多") cannot fire because there is no text.
   The network layer is the only trustworthy source.
2. **A blank render** (:func:`is_blank_render`): HTML was delivered but the
   body has no text. Alone it is only a hint; combined with (1) it is decisive.
3. **Risk text** (:func:`detect_risk_signal`, :func:`blocking_state`): toasts,
   alerts and interstitials such as "unusual activity" or "account locked",
   plus login/verification redirects.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

BlockingType = Literal["rate_limit", "transient_error", "account_restricted"]
Severity = Literal["retryable", "non_retryable"]


class RateLimitDetector:
    """Counts 429 responses from ``/i/api/`` and ``/graphql/`` on a page."""

    def __init__(self, page: Any) -> None:
        self._last_hit_at = 0.0
        self._hit_count = 0
        page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        try:
            if response.status != 429:
                return
            url = response.url
        except Exception:  # pragma: no cover - defensive against closed pages
            return
        if "/i/api/" in url or "/graphql/" in url:
            self._last_hit_at = time.monotonic()
            self._hit_count += 1

    def hit_recently(self, within_ms: float = 90_000) -> bool:
        return self._last_hit_at > 0 and (time.monotonic() - self._last_hit_at) * 1000 < within_ms

    @property
    def count(self) -> int:
        return self._hit_count

    def reset(self) -> None:
        self._last_hit_at = 0.0
        self._hit_count = 0


async def is_blank_render(page: Any) -> bool:
    """HTML arrived but the page shows no text — the typical rate-limited look."""
    try:
        html = await page.content()
        if len(html) < 5000:
            return False
        try:
            text = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            text = ""
        return text.strip() == ""
    except Exception:
        return False


def pathname_of(url: str) -> str:
    try:
        return urlparse(url).path
    except ValueError:
        return url


SESSION_BLOCKED_PATH = re.compile(r"^/(i/flow|login|account/access|account/suspended)", re.I)


@dataclass(frozen=True)
class RiskSignal:
    label: str
    reason: str


_DEFAULT_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rate-limit", re.compile(r"rate limit|too many requests|请求过多|频率限制|请稍后再试", re.I)),
    (
        "account-restricted",
        re.compile(
            r"account locked|account suspended|temporarily restricted|temporarily limited|"
            r"unusual activity|verify your account|"
            r"账号已锁定|账号已暂停|异常活动|请验证|访问受限",
            re.I,
        ),
    ),
    ("page-error", re.compile(r"something went wrong|出了点问题|出错了|发生错误", re.I)),
)


async def _alert_or_body_text(page: Any, *, timeout_ms: int = 2000) -> str:
    text = ""
    try:
        alerts = await page.locator('[role="alert"], [data-testid="toast"]').all_text_contents()
        text = " ".join(alerts).strip()
    except Exception:
        text = ""
    if not text:
        try:
            text = await page.locator("body").inner_text(timeout=timeout_ms)
        except Exception:
            text = ""
    return text


async def detect_risk_signal(
    page: Any, context: str, extra_patterns: tuple[str, ...] | list[str] = ()
) -> RiskSignal | None:
    """Used by the reviewed-unfollow executor before every action."""
    url = page.url
    if SESSION_BLOCKED_PATH.search(pathname_of(url)):
        return RiskSignal(
            "session-blocked", f"login/verification/account page detected ({context}): {url}"
        )

    text = await _alert_or_body_text(page)
    if not text:
        return None
    lowered = text.lower()
    for pattern in extra_patterns:
        if pattern and pattern.lower() in lowered:
            return RiskSignal("configured-risk-text", f"risk text matched: {pattern!r} ({context})")
    for label, pattern in _DEFAULT_RISK_PATTERNS:
        if pattern.search(text):
            return RiskSignal(
                label, f"blocking notice ({label}); retry later or intervene ({context})"
            )
    return None


@dataclass(frozen=True)
class BlockingState:
    type: BlockingType
    label: str
    severity: Severity
    message: str
    retry_after_ms: int | None = None


_BLOCKING_PATTERNS: tuple[tuple[BlockingType, str, Severity, re.Pattern[str]], ...] = (
    (
        "rate_limit",
        "rate limited",
        "retryable",
        re.compile(
            r"rate limit|too many requests|requests limit|"
            r"请求过多|频率限制|操作过于频繁|请稍后再试|try again later",
            re.I,
        ),
    ),
    (
        "account_restricted",
        "account access restricted",
        "non_retryable",
        re.compile(
            r"account locked|account suspended|temporarily restricted|temporarily limited|"
            r"unusual activity|账号已锁定|账号已暂停|访问受限|异常活动|请验证",
            re.I,
        ),
    ),
    (
        "transient_error",
        "transient page error",
        "retryable",
        re.compile(r"something went wrong|出了点问题|出错了|发生错误", re.I),
    ),
)

_BLOCKING_TEXT_SELECTORS = ", ".join(
    (
        '[role="alert"]',
        '[data-testid="toast"]',
        '[data-testid="error-detail"]',
        '[data-testid="emptyState"]',
        '[aria-live="assertive"]',
    )
)


async def blocking_text_snapshot(page: Any) -> str:
    try:
        visible = await page.locator(_BLOCKING_TEXT_SELECTORS).all_text_contents()
        try:
            body = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            body = ""
        joined = " ".join([*visible, body])
        return re.sub(r"\s+", " ", joined).strip()[:4000]
    except Exception:
        return ""


async def blocking_state(
    page: Any, rate_limit: RateLimitDetector, *, rate_limit_backoff_ms: int
) -> BlockingState | None:
    """What, if anything, currently stops us from acting on this page."""
    pathname = pathname_of(page.url)
    if pathname.startswith("/account/access"):
        return BlockingState(
            "account_restricted",
            "account access restricted",
            "non_retryable",
            f"page is {pathname}",
        )
    # Network-level 429 is the only reliable rate-limit signal: the page is
    # blank, so none of the text patterns below can fire.
    if rate_limit.hit_recently():
        return BlockingState(
            "rate_limit",
            "rate limited",
            "retryable",
            f"X API returned 429 ({rate_limit.count} hit(s) recently); page cannot render",
            retry_after_ms=rate_limit_backoff_ms,
        )
    text = await blocking_text_snapshot(page)
    if not text:
        return None
    for type_, label, severity, pattern in _BLOCKING_PATTERNS:
        if pattern.search(text):
            return BlockingState(
                type_,
                label,
                severity,
                text[:240],
                retry_after_ms=rate_limit_backoff_ms if type_ == "rate_limit" else None,
            )
    return None
