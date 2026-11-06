"""Bilibili-specific tunables, read from ``BILI_*`` environment variables."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from social_cleaner.core.settings import env, env_float, env_int
from social_cleaner.core.settings import settings as core_settings

ENV_PREFIX = "BILI_"


@dataclass(frozen=True)
class BiliSettings:
    """``api_qps`` is the single most important knob: it caps the request
    rate against Bilibili for the whole process. Raising it increases the
    chance of triggering risk control (``-352`` / HTTP 412)."""

    api_qps: float
    http_timeout: float
    max_retries: int
    retry_base_delay: float
    credentials_path: Path


def load_settings() -> BiliSettings:
    override = env("CREDENTIALS_PATH", prefix=ENV_PREFIX)
    return BiliSettings(
        api_qps=env_float("API_QPS", 1.5, minimum=0.01, prefix=ENV_PREFIX),
        http_timeout=env_float("HTTP_TIMEOUT", 10.0, minimum=0.1, prefix=ENV_PREFIX),
        max_retries=env_int("MAX_RETRIES", 3, minimum=0, prefix=ENV_PREFIX),
        retry_base_delay=env_float("RETRY_BASE_DELAY", 1.0, minimum=0.0, prefix=ENV_PREFIX),
        credentials_path=(
            Path(override).expanduser()
            if override
            else core_settings.data_dir / "bilibili" / "credentials.json"
        ),
    )


settings = load_settings()
