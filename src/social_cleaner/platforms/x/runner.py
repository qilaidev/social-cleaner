"""Run lifecycle for X commands: lock, browser, login, summary, signals.

Every browser-driving command goes through :func:`run_with_browser`. It
takes the run lock for the profile, launches the browser in the right mode,
gets a logged-in page and the username, hands them to the command, and
finally writes ``run-summary-<id>.json`` under ``<data_dir>/x/runs`` no
matter how the run ended. Ctrl+C / SIGTERM request cooperative cancellation
so the summary and audit trail are still written.

Headless policy (``X_HEADLESS``):

- ``false``: one visible browser for everything.
- ``true``: headless; fails if a login would be needed.
- ``auto``: headless when a saved session exists; if it turns out to be
  invalid, relaunch visible for the login, then relaunch headless for the
  work.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from social_cleaner import __version__
from social_cleaner.core.cancellation import is_cancellation_error, request_cancellation
from social_cleaner.core.errors import (
    ConfigError,
    NonRetryableError,
    RateLimitError,
    SocialCleanerError,
)
from social_cleaner.core.retry import sleep_ms
from social_cleaner.core.settings import settings as core_settings

from .browser import BrowserSession
from .config import XConfig, XEnv
from .following import build_run_id, write_json_atomic
from .login import LoginManager, LoginResult, normalize_handle
from .run_lock import RunLock, RunLockError

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "completed", "failed", "cancelled"]

# Exit code for "X rate-limited this account". Outer schedulers must treat it
# as "wait a long time", never as "retry now".
RATE_LIMIT_EXIT_CODE = 75
CANCELLED_EXIT_CODE = 130


@dataclass
class RunContext:
    run_id: str = field(default_factory=build_run_id)
    command: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: RunStatus = "running"
    username: str | None = None
    env: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    selectors: dict[str, Any] | None = None
    lock_path: str | None = None
    stats: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    rate_limited: bool = False
    exit_code: int = 0

    def summary(self) -> dict[str, Any]:
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.now(timezone.utc)
        return {
            "run_id": self.run_id,
            "command": self.command,
            "started_at": self.started_at,
            "finished_at": finished.isoformat(),
            "duration_ms": int((finished - started).total_seconds() * 1000),
            "status": self.status,
            "exit_code": self.exit_code,
            "rate_limited": self.rate_limited,
            "username": self.username,
            "app_version": __version__,
            "runtime": {
                "python": platform.python_version(),
                "platform": sys.platform,
                "arch": platform.machine(),
            },
            "env": self.env,
            "lock_path": self.lock_path,
            "config": self.config,
            "selectors": self.selectors,
            "stats": self.stats,
            "result": self.result,
            "error": self.error,
        }


def runs_dir() -> Path:
    return core_settings.data_dir / "x" / "runs"


def write_summary(ctx: RunContext) -> Path | None:
    try:
        directory = runs_dir()
        path = directory / f"run-summary-{ctx.run_id}.json"
        write_json_atomic(path, ctx.summary())
        logger.info("Run summary written to %s", path)
        return path
    except OSError as exc:
        logger.warning("Could not write run summary: %s", exc)
        return None


@dataclass
class XSession:
    browser: BrowserSession
    username: str

    @property
    def page(self):  # noqa: ANN201 - Playwright Page
        return self.browser.get_page()

    @property
    def headless(self) -> bool:
        return self.browser.headless


def _raise_if_rate_limited(result: LoginResult) -> None:
    if result.rate_limited:
        raise RateLimitError(result.message)


async def _login(browser: BrowserSession, env: XEnv, config: XConfig) -> LoginResult:
    manager = LoginManager(browser.get_page(), env)
    result = await manager.login(
        headless=browser.headless, login_url=config.urls.login, home_url=config.urls.home
    )
    _raise_if_rate_limited(result)
    return result


async def _relaunch(browser: BrowserSession, env: XEnv, *, headless: bool) -> BrowserSession:
    await browser.close()
    return await BrowserSession(env).start(headless=headless)


async def open_session(
    env: XEnv, config: XConfig, *, headless: bool | None = None, work_headless: bool | None = None
) -> XSession:
    """Launch, log in, resolve the username. Applies the headless policy."""
    browser = await BrowserSession(env).start(headless=headless)
    try:
        result = await _login(browser, env, config)
        if not result.success and browser.headless:
            logger.warning(
                "Headless login failed (%s); relaunching visible to log in", result.message
            )
            browser = await _relaunch(browser, env, headless=False)
            result = await _login(browser, env, config)
        if not result.success:
            raise NonRetryableError(result.message or "login failed")

        await browser.save_state()
        manager = LoginManager(browser.get_page(), env)
        username = await manager.get_username()
        if not username:
            logger.warning("Could not read the username; open your profile in the browser (10s)")
            await sleep_ms(10_000)
            username = await manager.get_username()
        if not username:
            raise NonRetryableError("could not determine the logged-in username")

        want_headless = env.prefers_headless_work() if work_headless is None else work_headless
        if want_headless and not browser.headless:
            logger.info("Login done; continuing headless with the same profile")
            await browser.save_state()
            browser = await _relaunch(browser, env, headless=True)
            recheck = await _login(browser, env, config)
            if not recheck.success:
                logger.warning("Headless session invalid (%s); staying visible", recheck.message)
                browser = await _relaunch(browser, env, headless=False)
                again = await _login(browser, env, config)
                if not again.success:
                    raise NonRetryableError(again.message or "login lost after relaunch")
            headless_username = await LoginManager(browser.get_page(), env).get_username()
            if headless_username:
                username = headless_username

        if env.target_username and normalize_handle(username) != env.target_username:
            raise NonRetryableError(
                f"logged in as @{username} but X_TARGET_USERNAME is @{env.target_username}; "
                "stopping so another saved session is not cleaned by mistake"
            )
        logger.info("Logged in as @%s (headless=%s)", username, browser.headless)
        return XSession(browser=browser, username=username)
    except BaseException:
        await browser.close(quiet=True)
        raise


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _cancel(sig: str) -> None:
        logger.warning("Received %s; finishing the current step and shutting down", sig)
        request_cancellation(f"received {sig}")

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, _cancel, name)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            signal.signal(signum, lambda *_, _name=name: _cancel(_name))


Command = Callable[[XSession, RunContext], Awaitable[dict[str, Any] | None]]


async def _run(
    env: XEnv,
    config: XConfig,
    ctx: RunContext,
    command: Command,
    *,
    headless: bool | None,
    work_headless: bool | None,
    selectors_meta: dict[str, Any],
) -> int:
    ctx.env = env.summary()
    ctx.config = config.summary()
    ctx.selectors = selectors_meta
    _install_signal_handlers(asyncio.get_running_loop())

    lock = RunLock(ctx.run_id, env.user_data_dir)
    session: XSession | None = None
    try:
        lock.acquire()
        ctx.lock_path = str(lock.path)
        session = await open_session(env, config, headless=headless, work_headless=work_headless)
        ctx.username = session.username
        ctx.result = await command(session, ctx)
        ctx.status = "completed"
        ctx.exit_code = 0
    except RateLimitError as exc:
        logger.error("X rate-limited this account (HTTP 429); stopping. %s", exc)
        logger.error("Re-logging in or restarting will not help. Wait, then lower the action rate.")
        ctx.status, ctx.error, ctx.rate_limited, ctx.exit_code = (
            "failed",
            str(exc),
            True,
            RATE_LIMIT_EXIT_CODE,
        )
    except RunLockError as exc:
        logger.error("%s", exc)
        ctx.status, ctx.error, ctx.exit_code = "failed", str(exc), 2
    except (ConfigError, NonRetryableError, SocialCleanerError) as exc:
        if is_cancellation_error(exc):
            logger.warning("Run cancelled")
            ctx.status, ctx.error, ctx.exit_code = "cancelled", str(exc), CANCELLED_EXIT_CODE
        else:
            logger.error("%s", exc)
            ctx.status, ctx.error, ctx.exit_code = "failed", str(exc), 1
    except Exception as exc:
        logger.exception("Run failed")
        ctx.status, ctx.error, ctx.exit_code = "failed", f"{type(exc).__name__}: {exc}", 1
    finally:
        if ctx.exit_code == 0 and ctx.stats and ctx.stats.get("errors") and env.fail_on_errors:
            logger.error("X_FAIL_ON_ERRORS=1 and the run had errors; exiting non-zero")
            ctx.status, ctx.exit_code = "failed", 1
        write_summary(ctx)
        if session is not None:
            if ctx.exit_code == 0 and env.close_wait_seconds and not session.headless:
                logger.info("Closing the browser in %ss", env.close_wait_seconds)
                try:
                    await sleep_ms(env.close_wait_seconds * 1000)
                except Exception:
                    pass
            await session.browser.close()
        lock.release()
    return ctx.exit_code


def run_with_browser(
    env: XEnv,
    config: XConfig,
    command_name: str,
    command: Command,
    *,
    headless: bool | None = None,
    work_headless: bool | None = None,
    selectors_meta: dict[str, Any] | None = None,
) -> int:
    """Blocking entry point used by the CLI. Returns the exit code."""
    ctx = RunContext(command=command_name)
    logger.info(
        "Run %s (%s) starting; data dir %s", ctx.run_id, command_name, core_settings.data_dir
    )
    return asyncio.run(
        _run(
            env,
            config,
            ctx,
            command,
            headless=headless,
            work_headless=work_headless,
            selectors_meta=selectors_meta or {"source": "builtin"},
        )
    )


async def countdown(seconds: int, what: str) -> None:
    if seconds <= 0:
        return
    logger.warning("%s starts in %ss — press Ctrl+C to abort", what, seconds)
    for remaining in range(seconds, 0, -1):
        if remaining <= 5 or remaining % 5 == 0:
            logger.warning("%ss…", remaining)
        await sleep_ms(1000)
