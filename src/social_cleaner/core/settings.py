"""Process-wide settings read from ``SOCIAL_CLEANER_*`` environment variables.

Every value has a working default, so the tool runs with zero configuration.
Platform-specific knobs live next to the platform (``BILI_*``, ``X_*``); only
what every platform shares is here.

Values are read once at import time; restart the process to pick up changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "SOCIAL_CLEANER_"

DEFAULT_DATA_DIR = Path.home() / ".social-cleaner"


def env(name: str, *, prefix: str = ENV_PREFIX) -> str | None:
    value = os.environ.get(prefix + name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_float(
    name: str, default: float, *, minimum: float | None = None, prefix: str = ENV_PREFIX
) -> float:
    raw = env(name, prefix=prefix)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def env_int(
    name: str, default: int, *, minimum: int | None = None, prefix: str = ENV_PREFIX
) -> int:
    raw = env(name, prefix=prefix)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def env_bool(name: str, default: bool, *, prefix: str = ENV_PREFIX) -> bool:
    raw = env(name, prefix=prefix)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    """Shared tunables.

    ``data_dir`` is the root for everything the tool persists: credentials,
    browser profiles, run summaries and the audit trail.
    """

    data_dir: Path
    log_level: str
    audit_log_enabled: bool
    audit_log_path: Path


def load_settings() -> Settings:
    data_dir = Path(env("DATA_DIR") or DEFAULT_DATA_DIR).expanduser()
    audit_path = env("AUDIT_LOG_PATH")
    return Settings(
        data_dir=data_dir,
        log_level=(env("LOG_LEVEL") or "INFO").upper(),
        audit_log_enabled=env_bool("AUDIT_LOG_ENABLED", True),
        audit_log_path=Path(audit_path).expanduser() if audit_path else data_dir / "audit.jsonl",
    )


settings = load_settings()
