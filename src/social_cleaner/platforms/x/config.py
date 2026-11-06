"""Typed configuration for the X platform.

Two sources, merged in this order:

1. Built-in conservative defaults (small batches, long delays, headful).
2. An optional JSON file (``--config`` / ``X_CONFIG_FILE``) that overrides any
   subset of keys — see ``docs/x-config.example.json``.

CLI flags then override individual fields on top. Environment variables
(``X_*``) cover the machine-level knobs: browser profile, viewport, headless
mode, credentials for automatic login.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal

from social_cleaner.core.errors import ConfigError
from social_cleaner.core.settings import env, env_bool, env_int
from social_cleaner.core.settings import settings as core_settings

from .selectors import REQUIRED_KEYS, load_selectors

ENV_PREFIX = "X_"

HeadlessMode = Literal["auto", "true", "false"]
BrowserType = Literal["chromium", "firefox", "webkit"]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# File / CLI configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeleteOptions:
    """Which content types ``x clean`` touches. Direct unfollow is not here on
    purpose: it lives behind ``x followings clear`` with its own confirmation."""

    tweets: bool = True
    replies: bool = True
    retweets: bool = True
    likes: bool = False
    bookmarks: bool = False

    def enabled(self) -> list[str]:
        return [f.name for f in fields(self) if getattr(self, f.name)]


@dataclass(frozen=True)
class ExecutionConfig:
    max_per_session: int = 5
    per_batch: int = 3
    delay_between_actions_ms: int = 1500
    delay_jitter_ms: int = 500
    delay_between_batches_ms: int = 2500
    page_ready_timeout_ms: int = 4000
    refresh_every_batches: int = 2


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    delay_ms: int = 5000
    exponential_backoff: bool = True


@dataclass(frozen=True)
class FollowingRules:
    keep_handles: tuple[str, ...] = ()
    drop_handles: tuple[str, ...] = ()
    keep_keywords: tuple[str, ...] = ()
    drop_keywords: tuple[str, ...] = (
        "airdrop",
        "giveaway",
        "casino",
        "bet",
        "博彩",
        "空投",
        "抽奖",
        "接推广",
        "商务合作",
    )
    low_info_candidate: bool = True


@dataclass(frozen=True)
class FollowingExecution:
    min_delay_ms: int = 6000
    max_delay_ms: int = 14000
    max_unfollow_per_session: int = 10
    require_confirm_file: bool = True
    max_consecutive_failures: int = 3
    cooldown_every_actions: int = 20
    cooldown_ms: int = 300_000


@dataclass(frozen=True)
class FollowingSafety:
    require_headful_for_execute: bool = True
    stop_on_risk_signals: bool = True
    risk_text_patterns: tuple[str, ...] = (
        "unusual activity",
        "temporarily restricted",
        "temporarily limited",
        "rate limit",
        "account locked",
        "account suspended",
        "verify your account",
        "账号已锁定",
        "账号已暂停",
        "异常活动",
        "请验证",
        "访问受限",
    )


@dataclass(frozen=True)
class FollowingConfig:
    rules: FollowingRules = field(default_factory=FollowingRules)
    execution: FollowingExecution = field(default_factory=FollowingExecution)
    safety: FollowingSafety = field(default_factory=FollowingSafety)


@dataclass(frozen=True)
class Urls:
    """``{username}`` is substituted with the logged-in handle.

    x.com is the canonical host; twitter.com still 301s to it, and going
    straight to x.com avoids losing login state across the redirect.
    """

    login: str = "https://x.com/i/flow/login"
    home: str = "https://x.com/home"
    tweets: str = "https://x.com/{username}"
    tweets_with_replies: str = "https://x.com/{username}/with_replies"
    likes: str = "https://x.com/{username}/likes"
    bookmarks: str = "https://x.com/i/bookmarks"
    following: str = "https://x.com/{username}/following"
    reposts: str = "https://x.com/{username}/reposts"

    def for_user(self, key: str, username: str) -> str:
        return getattr(self, key).replace("{username}", username)


@dataclass(frozen=True)
class XConfig:
    delete: DeleteOptions = field(default_factory=DeleteOptions)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    following: FollowingConfig = field(default_factory=FollowingConfig)
    urls: Urls = field(default_factory=Urls)
    selectors: dict[str, str] = field(default_factory=load_selectors)

    def summary(self) -> dict[str, Any]:
        """What the run summary records (selectors are recorded by source)."""
        return {
            "delete": _to_dict(self.delete),
            "execution": _to_dict(self.execution),
            "retry": _to_dict(self.retry),
            "following": _to_dict(self.following),
            "urls": _to_dict(self.urls),
        }


def _to_dict(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_dict(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_to_dict(v) for v in value]
    return value


def _merge_dataclass(instance: Any, raw: Any, *, where: str) -> Any:
    """Return ``instance`` with the keys present in ``raw`` overridden.

    Unknown keys are an error: a typo in a safety limit must not silently
    leave the default in place.
    """
    if raw is None:
        return instance
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object")
    known = {f.name: f for f in fields(instance)}
    updates: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"{where}: unknown key '{key}' (known: {', '.join(sorted(known))})")
        current = getattr(instance, key)
        if is_dataclass(current) and not isinstance(current, type):
            updates[key] = _merge_dataclass(current, value, where=f"{where}.{key}")
        elif isinstance(current, tuple):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{where}.{key} must be a list of strings")
            updates[key] = tuple(v.strip() for v in value if v.strip())
        elif isinstance(current, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{where}.{key} must be true or false")
            updates[key] = value
        elif isinstance(current, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{where}.{key} must be an integer")
            updates[key] = value
        elif isinstance(current, str):
            if not isinstance(value, str):
                raise ConfigError(f"{where}.{key} must be a string")
            updates[key] = value
        else:  # pragma: no cover - no other field types exist
            updates[key] = value
    return replace(instance, **updates)


def load_config(path: Path | None = None, *, selectors_file: Path | None = None) -> XConfig:
    """Defaults overridden by ``path`` (JSON). Always validated."""
    config = XConfig(selectors=load_selectors(selectors_file))
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config file {path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"config file {path} must contain a JSON object")
        if "selectors" in raw:
            raise ConfigError(
                f"config file {path}: put selectors in their own file "
                "(--selectors / X_SELECTORS_FILE)"
            )
        config = _merge_dataclass(config, raw, where=str(path))
    validate_config(config)
    return config


def validate_config(config: XConfig) -> None:
    ex = config.execution
    if ex.max_per_session <= 0:
        raise ConfigError("execution.max_per_session must be > 0")
    if ex.per_batch <= 0:
        raise ConfigError("execution.per_batch must be > 0")
    if ex.max_per_session < ex.per_batch:
        raise ConfigError("execution.max_per_session must be >= execution.per_batch")
    for name in (
        "delay_between_actions_ms",
        "delay_jitter_ms",
        "delay_between_batches_ms",
        "page_ready_timeout_ms",
    ):
        if getattr(ex, name) < 0:
            raise ConfigError(f"execution.{name} must be >= 0")
    if ex.refresh_every_batches < 1:
        raise ConfigError("execution.refresh_every_batches must be >= 1")

    rt = config.retry
    if rt.max_attempts < 1:
        raise ConfigError("retry.max_attempts must be >= 1")
    if rt.delay_ms < 0:
        raise ConfigError("retry.delay_ms must be >= 0")

    fe = config.following.execution
    if fe.min_delay_ms < 0 or fe.max_delay_ms < 0:
        raise ConfigError("following.execution delays must be >= 0")
    if fe.max_delay_ms < fe.min_delay_ms:
        raise ConfigError("following.execution.max_delay_ms must be >= min_delay_ms")
    if fe.max_unfollow_per_session <= 0:
        raise ConfigError("following.execution.max_unfollow_per_session must be > 0")
    if fe.max_consecutive_failures <= 0:
        raise ConfigError("following.execution.max_consecutive_failures must be > 0")
    if fe.cooldown_every_actions < 0:
        raise ConfigError("following.execution.cooldown_every_actions must be >= 0")
    if fe.cooldown_ms < 0:
        raise ConfigError("following.execution.cooldown_ms must be >= 0")

    missing = sorted(k for k in REQUIRED_KEYS if not config.selectors.get(k, "").strip())
    if missing:
        raise ConfigError(f"missing selectors: {', '.join(missing)}")
    for key, value in config.selectors.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"selector '{key}' must be a non-empty string")

    for key in ("tweets", "tweets_with_replies", "likes", "following", "reposts"):
        if "{username}" not in getattr(config.urls, key):
            raise ConfigError(f"urls.{key} must contain the {{username}} placeholder")


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class XEnv:
    """Machine-level settings from ``X_*`` environment variables.

    ``headless_mode``: ``false`` (default, always show the browser), ``true``
    (never show it — needs a saved session or credentials), or ``auto`` (show
    it only when a login is needed, then continue headless).
    """

    username: str
    password: str
    headless_mode: HeadlessMode
    browser_type: BrowserType
    browser_channel: str | None
    user_agent: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: float
    locale: str
    timezone_id: str
    user_data_dir: Path
    target_username: str | None
    manual_login_timeout_minutes: int
    start_countdown_seconds: int
    close_wait_seconds: int
    fail_on_errors: bool

    @property
    def profile_dir(self) -> Path:
        return self.user_data_dir / "profile"

    @property
    def state_path(self) -> Path:
        return self.user_data_dir / "state.json"

    def resolve_headless(self) -> bool:
        """What the *first* browser launch should be.

        ``auto`` launches headless only when a saved session exists; if that
        session turns out to be invalid the runner relaunches headful.
        """
        if self.headless_mode == "auto":
            return has_saved_session(self.user_data_dir)
        return self.headless_mode == "true"

    def prefers_headless_work(self) -> bool:
        return self.headless_mode in {"auto", "true"}

    def summary(self) -> dict[str, Any]:
        return {
            "headless_mode": self.headless_mode,
            "browser_type": self.browser_type,
            "browser_channel": self.browser_channel,
            "user_agent": self.user_agent,
            "viewport": f"{self.viewport_width}x{self.viewport_height}",
            "device_scale_factor": self.device_scale_factor,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "user_data_dir": str(self.user_data_dir),
            "target_username": self.target_username,
            "fail_on_errors": self.fail_on_errors,
        }


def has_saved_session(user_data_dir: Path) -> bool:
    """A reusable local login: a non-trivial state.json or a non-empty profile."""
    state = user_data_dir / "state.json"
    try:
        if state.is_file() and state.stat().st_size > 32:
            return True
    except OSError:
        pass
    profile = user_data_dir / "profile"
    try:
        return profile.is_dir() and any(p.name != ".DS_Store" for p in profile.iterdir())
    except OSError:
        return False


def parse_headless_mode(value: str | None) -> HeadlessMode:
    if value is None:
        return "false"
    lowered = value.strip().lower()
    if lowered == "auto":
        return "auto"
    if lowered in {"true", "1", "yes", "on"}:
        return "true"
    if lowered in {"false", "0", "no", "off"}:
        return "false"
    raise ConfigError(f"X_HEADLESS must be auto, true or false (got {value!r})")


def load_env() -> XEnv:
    browser_type = (env("BROWSER_TYPE", prefix=ENV_PREFIX) or "chromium").lower()
    if browser_type not in {"chromium", "firefox", "webkit"}:
        raise ConfigError(
            f"X_BROWSER_TYPE must be chromium, firefox or webkit (got {browser_type!r})"
        )

    width = env_int("VIEWPORT_WIDTH", 1512, prefix=ENV_PREFIX)
    height = env_int("VIEWPORT_HEIGHT", 982, prefix=ENV_PREFIX)
    if width < 320 or height < 320:
        raise ConfigError("X_VIEWPORT_WIDTH / X_VIEWPORT_HEIGHT must be >= 320")

    dsf_raw = env("DEVICE_SCALE_FACTOR", prefix=ENV_PREFIX)
    try:
        dsf = float(dsf_raw) if dsf_raw else 2.0
    except ValueError as exc:
        raise ConfigError("X_DEVICE_SCALE_FACTOR must be a number") from exc
    if dsf <= 0:
        raise ConfigError("X_DEVICE_SCALE_FACTOR must be > 0")

    user_data_dir = env("USER_DATA_DIR", prefix=ENV_PREFIX)
    resolved_dir = (
        Path(user_data_dir).expanduser()
        if user_data_dir
        else core_settings.data_dir / "x" / "browser-data"
    ).resolve()
    if resolved_dir == Path(resolved_dir.anchor) or resolved_dir == Path.cwd().resolve():
        raise ConfigError(
            "X_USER_DATA_DIR must not be the filesystem root or the working directory"
        )

    target = env("TARGET_USERNAME", prefix=ENV_PREFIX)
    manual_timeout = min(60, max(5, env_int("MANUAL_LOGIN_TIMEOUT_MINUTES", 15, prefix=ENV_PREFIX)))
    countdown = min(60, max(0, env_int("START_COUNTDOWN_SECONDS", 10, prefix=ENV_PREFIX)))
    close_wait = min(30, max(0, env_int("CLOSE_WAIT_SECONDS", 3, prefix=ENV_PREFIX)))

    return XEnv(
        username=os.environ.get("X_USERNAME", "").strip(),
        password=os.environ.get("X_PASSWORD", ""),
        headless_mode=parse_headless_mode(env("HEADLESS", prefix=ENV_PREFIX)),
        browser_type=browser_type,  # type: ignore[arg-type]
        browser_channel=env("BROWSER_CHANNEL", prefix=ENV_PREFIX),
        user_agent=env("USER_AGENT", prefix=ENV_PREFIX) or DEFAULT_USER_AGENT,
        viewport_width=width,
        viewport_height=height,
        device_scale_factor=dsf,
        locale=env("LOCALE", prefix=ENV_PREFIX) or "zh-CN",
        timezone_id=env("TIMEZONE_ID", prefix=ENV_PREFIX) or "Asia/Shanghai",
        user_data_dir=resolved_dir,
        target_username=target.lstrip("@").lower() if target else None,
        manual_login_timeout_minutes=manual_timeout,
        start_countdown_seconds=countdown,
        close_wait_seconds=close_wait,
        fail_on_errors=env_bool("FAIL_ON_ERRORS", False, prefix=ENV_PREFIX),
    )


def default_config_path() -> Path | None:
    raw = env("CONFIG_FILE", prefix=ENV_PREFIX)
    return Path(raw).expanduser() if raw else None


def default_selectors_path() -> Path | None:
    raw = env("SELECTORS_FILE", prefix=ENV_PREFIX)
    return Path(raw).expanduser() if raw else None
