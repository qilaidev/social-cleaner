"""Persistent browser profile management.

The real browser profile (cookies, localStorage, IndexedDB, service workers)
lives in ``<user_data_dir>/profile`` and is the source of truth for the
session. ``<user_data_dir>/state.json`` is a secondary cookie snapshot: it is
re-imported on every launch so a channel switch or a crash that clobbered the
profile's cookies does not turn into a "logged out / blank page" run, and it
makes the session easy to back up or move.

Device fingerprint is deliberately fixed rather than randomised per run: an
account whose "device" changes every day looks more suspicious, not less.
"""

from __future__ import annotations

import json
import logging
import shutil
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import XEnv

logger = logging.getLogger(__name__)

_INIT_SCRIPT = """
(() => {{
  const profile = {profile};
  const define = (name, value) =>
    Object.defineProperty(navigator, name, {{ get: () => value, configurable: true }});
  define('webdriver', false);
  define('plugins', [1, 2, 3, 4, 5]);
  define('languages', profile.languages);
  define('platform', profile.platform);
  define('hardwareConcurrency', profile.hardwareConcurrency);
  define('deviceMemory', profile.deviceMemory);
}})();
"""


class BrowserSession:
    """One launched browser context. ``headless`` reflects the real launch."""

    def __init__(self, env: XEnv) -> None:
        self.env = env
        self.headless = False
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self, headless: bool | None = None) -> BrowserSession:
        env = self.env
        self.headless = env.resolve_headless() if headless is None else headless
        logger.info(
            "Launching %s (headless=%s, mode=%s)",
            env.browser_type,
            self.headless,
            env.headless_mode,
        )

        profile_dir = env.profile_dir
        profile_existed = profile_dir.exists()
        profile_was_empty = profile_existed and not any(profile_dir.iterdir())
        env.user_data_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, env.browser_type)

        launch: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                f"--window-size={env.viewport_width},{env.viewport_height}",
            ],
            "viewport": {"width": env.viewport_width, "height": env.viewport_height},
            "screen": {"width": env.viewport_width, "height": env.viewport_height},
            "device_scale_factor": env.device_scale_factor,
            "user_agent": env.user_agent,
            "locale": env.locale,
            "timezone_id": env.timezone_id,
            "has_touch": False,
            "is_mobile": False,
            "color_scheme": "light",
            "reduced_motion": "reduce",
            "extra_http_headers": {
                "Accept-Language": f"{env.locale},zh;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }
        if env.browser_channel:
            launch["channel"] = env.browser_channel

        try:
            try:
                self.context = await browser_type.launch_persistent_context(
                    str(profile_dir), **launch
                )
            except Exception as exc:
                message = str(exc)
                missing_shell = "headless_shell" in message or "Executable doesn't exist" in message
                if (
                    self.headless
                    and not env.browser_channel
                    and missing_shell
                    and env.browser_type == "chromium"
                ):
                    logger.warning(
                        "Playwright headless shell missing; falling back to channel=chrome"
                    )
                    self.context = await browser_type.launch_persistent_context(
                        str(profile_dir), **{**launch, "channel": "chrome"}
                    )
                else:
                    raise

            profile = {
                "languages": [env.locale, "zh", "en-US", "en"],
                "platform": "MacIntel",
                "hardwareConcurrency": 8,
                "deviceMemory": 8,
            }
            await self.context.add_init_script(_INIT_SCRIPT.format(profile=json.dumps(profile)))
            await self._restore_cookie_snapshot(profile_existed, profile_was_empty)

            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            self.page.set_default_timeout(30_000)
            self.page.set_default_navigation_timeout(30_000)
            logger.info(
                "Browser ready: viewport=%sx%s dpr=%s locale=%s tz=%s profile=%s",
                env.viewport_width,
                env.viewport_height,
                env.device_scale_factor,
                env.locale,
                env.timezone_id,
                profile_dir,
            )
            return self
        except Exception:
            await self._rollback(profile_existed, profile_was_empty)
            raise

    async def _restore_cookie_snapshot(
        self, profile_existed: bool, profile_was_empty: bool
    ) -> None:
        assert self.context is not None
        state_path = self.env.state_path
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies") if isinstance(state, dict) else None
            if isinstance(cookies, list) and cookies:
                await self.context.add_cookies(cookies)
                reason = (
                    "new profile"
                    if (not profile_existed or profile_was_empty)
                    else "existing profile"
                )
                logger.info("Imported %s cookie(s) from state.json (%s)", len(cookies), reason)
        except Exception as exc:
            logger.warning("Could not import state.json snapshot, continuing with profile: %s", exc)

    async def save_state(self) -> None:
        """Snapshot cookies + storage to ``state.json`` (0600) atomically."""
        if self.context is None:
            raise RuntimeError("browser context not started")
        state_path = self.env.state_path
        tmp = state_path.with_suffix(".json.tmp")
        try:
            await self.context.storage_state(path=str(tmp))
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            tmp.replace(state_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        logger.info("Session snapshot saved to %s", state_path)

    async def _rollback(self, profile_existed: bool, profile_was_empty: bool) -> None:
        await self.close(quiet=True)
        if not profile_existed or profile_was_empty:
            shutil.rmtree(self.env.profile_dir, ignore_errors=True)
            logger.warning("Launch failed; removed the empty profile created for this run")

    async def close(self, *, quiet: bool = False) -> None:
        page, context, pw = self.page, self.context, self._playwright
        self.page = self.context = self._playwright = None
        for label, closer in (("page", page), ("context", context)):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception as exc:
                if not quiet:
                    logger.warning("Failed to close %s: %s", label, exc)
        if pw is not None:
            try:
                await pw.stop()
            except Exception as exc:  # pragma: no cover - best effort
                if not quiet:
                    logger.warning("Failed to stop Playwright: %s", exc)
        if not quiet:
            logger.info("Browser closed")

    def get_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("browser page not started")
        return self.page
