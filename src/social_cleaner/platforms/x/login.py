"""Getting a logged-in page.

Order of preference:

1. The persistent profile already carries a session → nothing to do.
2. ``X_USERNAME`` / ``X_PASSWORD`` are set → fill the login form; any
   verification step (2FA, captcha, "confirm your identity") hands over to
   the human in a visible browser.
3. Otherwise wait for the user to log in manually in the visible browser.

Login state is read from the DOM (profile link, account switcher, composer)
rather than from URLs, because x.com keeps ``/home`` in the address bar while
it shows a login wall.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from playwright.async_api import Page

from social_cleaner.core.retry import RetryPolicy, sleep_ms, with_retry

from .config import XEnv, has_saved_session
from .signals import RateLimitDetector, is_blank_render

logger = logging.getLogger(__name__)

LoginState = Literal["logged_in", "verification", "login_flow", "pending"]

AUTHENTICATED_DOM_SELECTORS = (
    '[data-testid="AppTabBar_Profile_Link"]',
    '[data-testid="SideNav_AccountSwitcher_Button"]',
    '[data-testid="AppTabBar_Home_Link"]',
    '[data-testid="primaryColumn"] [data-testid="tweetTextarea_0"]',
)

LOGIN_FLOW_SELECTORS = (
    'input[autocomplete="username"]',
    'input[type="password"]',
    'input[name="text"]',
    '[data-testid="LoginForm_Login_Button"]',
)

VERIFICATION_DOM_SELECTORS = (
    'input[data-testid="ocfEnterTextTextInput"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    '[data-testid="ocfEnterTextNextButton"]',
    '[data-testid="ocfEnterTextSkipButton"]',
    '[data-testid="arkoseFrame"]',
    'iframe[src*="arkoselabs"]',
    'iframe[title*="challenge" i]',
    'iframe[title*="captcha" i]',
)

# (source, flags) so the same patterns run in Python and inside the page.
VERIFICATION_TEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"verify your account", "i"),
    (r"confirm your identity", "i"),
    (r"enter your phone number", "i"),
    (r"enter your verification code", "i"),
    (r"suspicious login", "i"),
    (r"unusual activity", "i"),
    (r"account access", "i"),
    (r"temporarily limited", "i"),
    (r"temporarily restricted", "i"),
    (r"账号.*验证", ""),
    (r"请验证", ""),
    (r"验证码", ""),
    (r"异常活动", ""),
    (r"访问受限", ""),
)
_VERIFICATION_RE = tuple(
    re.compile(src, re.I if "i" in flags else 0) for src, flags in VERIFICATION_TEXT_PATTERNS
)

RESERVED_PATH_SEGMENTS = frozenset(
    {
        "account",
        "compose",
        "explore",
        "home",
        "i",
        "jobs",
        "login",
        "logout",
        "messages",
        "notifications",
        "search",
        "settings",
        "tos",
    }
)
X_HOSTS = frozenset({"twitter.com", "www.twitter.com", "x.com", "www.x.com"})

_STATE_PROBE_JS = """
({ states, authSelectors, loginSelectors, verificationSelectors, verificationPatterns }) => {
  const hasSelector = (selectors) => selectors.some((s) => document.querySelector(s));
  const bodyText = (document.body && document.body.innerText) || '';
  const hasVerificationText = verificationPatterns.some(
    ([source, flags]) => new RegExp(source, flags).test(bodyText)
  );
  const current = hasSelector(authSelectors)
    ? 'logged_in'
    : hasSelector(verificationSelectors) || hasVerificationText
      ? 'verification'
      : hasSelector(loginSelectors)
        ? 'login_flow'
        : 'pending';
  return states.includes(current) ? current : false;
}
"""


@dataclass(frozen=True)
class LoginResult:
    success: bool
    message: str
    # The session may be fine but X returns 429 for the account and renders
    # nothing. Retrying, switching browser mode or re-logging only deepens it.
    rate_limited: bool = False


def extract_username_from_url(url: str) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.hostname not in X_HOSTS:
        return None
    path = parsed.path.strip("/")
    if not path or path == "home":
        return None
    first = path.split("/", 1)[0]
    if not first or first.lower() in RESERVED_PATH_SEGMENTS:
        return None
    return first


class LoginManager:
    def __init__(self, page: Page, env: XEnv) -> None:
        self.page = page
        self.env = env
        self.rate_limit = RateLimitDetector(page)

    # ------------------------------------------------------------------ probes

    async def _has_any(self, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            try:
                if await self.page.query_selector(selector):
                    return True
            except Exception:
                continue
        return False

    async def _has_verification_signal(self) -> bool:
        if await self._has_any(VERIFICATION_DOM_SELECTORS):
            return True
        try:
            body = await self.page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return any(p.search(body) for p in _VERIFICATION_RE)

    async def _logged_in_without_navigation(self) -> bool:
        return await self._has_any(AUTHENTICATED_DOM_SELECTORS)

    async def _current_state(self) -> LoginState:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception:
            pass
        if await self._logged_in_without_navigation():
            return "logged_in"
        if await self._has_verification_signal():
            return "verification"
        if await self._has_any(LOGIN_FLOW_SELECTORS):
            return "login_flow"
        return "pending"

    async def _wait_for_state(
        self, expected: tuple[LoginState, ...], timeout_ms: int
    ) -> LoginState:
        handle = await self.page.wait_for_function(
            _STATE_PROBE_JS,
            arg={
                "states": list(expected),
                "authSelectors": list(AUTHENTICATED_DOM_SELECTORS),
                "loginSelectors": list(LOGIN_FLOW_SELECTORS),
                "verificationSelectors": list(VERIFICATION_DOM_SELECTORS),
                "verificationPatterns": [list(p) for p in VERIFICATION_TEXT_PATTERNS],
            },
            timeout=timeout_ms,
        )
        return await handle.json_value()

    async def _wait_for_authenticated_dom(self, timeout_ms: int) -> bool:
        import time

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=1000)
            except Exception:
                pass
            if await self._logged_in_without_navigation():
                return True
            if await self._has_verification_signal():
                return False
            await sleep_ms(1000)
        return False

    async def is_logged_in(self, home_url: str) -> bool:
        try:
            await self.page.goto(home_url, wait_until="domcontentloaded")
            if await self._wait_for_authenticated_dom(10_000):
                logger.info("Existing session is valid")
                return True
            return False
        except Exception as exc:
            logger.debug("Login probe failed: %s", exc)
            return False

    # ------------------------------------------------------------------- flows

    async def login(self, *, headless: bool, login_url: str, home_url: str) -> LoginResult:
        env = self.env
        has_credentials = bool(env.username and env.password)
        if headless and not has_credentials and not has_saved_session(env.user_data_dir):
            return LoginResult(
                False,
                "headless mode needs either X_USERNAME/X_PASSWORD or a saved browser session; "
                "run `social-cleaner x login` once with X_HEADLESS=false",
            )

        if await self.is_logged_in(home_url):
            return LoginResult(True, "already logged in")

        # Not logged in *or* rate-limited into a blank page? The latter must
        # stop here: every further attempt makes it worse.
        if self.rate_limit.hit_recently() or await is_blank_render(self.page):
            return LoginResult(
                False,
                "X returned 429 for this account and the page cannot render. This is not a "
                "login problem: stop all automation and wait (30-60 minutes at least), then "
                "lower the action rate.",
                rate_limited=True,
            )

        if not has_credentials:
            logger.info("No X_USERNAME/X_PASSWORD; waiting for manual login")
            return await self.manual_login(navigate=True, headless=headless, login_url=login_url)

        logger.info("Credentials present; attempting automatic login")
        try:
            return await self._automatic_login(headless=headless, login_url=login_url)
        except Exception as exc:
            logger.debug("Automatic login raised: %s", exc)
            if await self._has_verification_signal():
                return await self._hand_over(
                    "automatic login hit a verification step", headless, login_url
                )
            logger.warning("Automatic login failed (%s); falling back to manual login", exc)
            return await self.manual_login(navigate=False, headless=headless, login_url=login_url)

    async def _hand_over(self, why: str, headless: bool, login_url: str) -> LoginResult:
        if headless:
            return LoginResult(False, f"{why}; rerun with X_HEADLESS=false to complete it by hand")
        logger.warning("%s; waiting for you to finish it in the browser", why)
        return await self.manual_login(navigate=False, headless=False, login_url=login_url)

    async def _automatic_login(self, *, headless: bool, login_url: str) -> LoginResult:
        stage = RetryPolicy(max_attempts=3, delay_ms=1500, exponential_backoff=False)

        async def navigate() -> LoginState:
            await self.page.goto(login_url, wait_until="domcontentloaded")
            return await self._wait_for_state(("logged_in", "login_flow", "verification"), 15_000)

        state = await with_retry(navigate, stage, name="login: navigate")
        if state == "logged_in":
            return LoginResult(True, "already logged in")
        if state == "verification":
            return await self._hand_over(
                "login page opened on a verification step", headless, login_url
            )

        async def fill_username() -> None:
            field = await self.page.wait_for_selector(
                'input[autocomplete="username"]', timeout=10_000, state="visible"
            )
            assert field is not None
            await field.fill(self.env.username)
            await self.page.keyboard.press("Enter")

        await with_retry(fill_username, stage, name="login: username")
        await sleep_ms(1500)
        if await self._current_state() == "verification":
            return await self._hand_over(
                "X asked for extra verification after the username", headless, login_url
            )

        async def fill_password() -> None:
            field = await self.page.wait_for_selector(
                'input[type="password"]', timeout=15_000, state="visible"
            )
            assert field is not None
            await field.fill(self.env.password)
            await self.page.keyboard.press("Enter")

        await with_retry(fill_password, stage, name="login: password")
        if await self._wait_for_authenticated_dom(20_000):
            logger.info("Automatic login succeeded")
            return LoginResult(True, "logged in")
        if await self._has_verification_signal():
            return await self._hand_over(
                "X asked for verification after the password", headless, login_url
            )
        logger.warning("No logged-in DOM after submitting; falling back to manual login")
        return await self.manual_login(navigate=False, headless=headless, login_url=login_url)

    async def manual_login(self, *, navigate: bool, headless: bool, login_url: str) -> LoginResult:
        if headless:
            return LoginResult(
                False, "manual login is impossible in headless mode; set X_HEADLESS=false"
            )

        minutes = self.env.manual_login_timeout_minutes
        logger.info("=" * 60)
        logger.info("Please log in to X in the browser window.")
        logger.info("Complete any verification steps; the run continues automatically.")
        logger.info("Waiting up to %s minute(s) (+3 on a verification step).", minutes)
        logger.info("=" * 60)

        if navigate:
            try:
                await self.page.goto(login_url, wait_until="domcontentloaded")
                await sleep_ms(2000)
            except Exception as exc:
                logger.debug("Navigation to login page failed (maybe already there): %s", exc)

        import time

        started = time.monotonic()
        base = minutes * 60
        grace = 3 * 60
        deadline = started + base
        grace_applied = False
        last_notice = started

        while time.monotonic() < deadline:
            await sleep_ms(1000)
            state = await self._current_state()
            if state == "logged_in":
                logger.info("Login detected")
                return LoginResult(True, "manual login succeeded")
            if state == "verification" and not grace_applied:
                deadline = min(started + base + grace, deadline + grace)
                grace_applied = True
            if time.monotonic() - last_notice >= 30:
                last_notice = time.monotonic()
                remaining = max(1, int((deadline - time.monotonic()) // 60) + 1)
                logger.info(
                    "Still waiting for login (%s), about %s minute(s) left", state, remaining
                )

        return LoginResult(False, "timed out waiting for login (captcha, 2FA or network page?)")

    # ---------------------------------------------------------------- identity

    async def get_username(self) -> str | None:
        try:
            await sleep_ms(2000)
            link = await self.page.query_selector('[data-testid="AppTabBar_Profile_Link"]')
            if link:
                href = await link.get_attribute("href")
                if href:
                    username = extract_username_from_url(f"https://x.com{href}")
                    if username:
                        return username
            return extract_username_from_url(self.page.url)
        except Exception as exc:
            logger.debug("Could not read username: %s", exc)
            return None


def normalize_handle(handle: str | None) -> str:
    """``@Foo `` → ``foo``."""
    return (handle or "").strip().lstrip("@").lower()
