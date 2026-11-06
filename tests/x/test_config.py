from __future__ import annotations

import json
from pathlib import Path

import pytest

from social_cleaner.core.errors import ConfigError
from social_cleaner.platforms.x.config import (
    DeleteOptions,
    XConfig,
    has_saved_session,
    load_config,
    load_env,
    parse_headless_mode,
    validate_config,
)


def test_defaults_are_conservative() -> None:
    config = XConfig()
    validate_config(config)
    assert config.execution.max_per_session == 5
    assert config.execution.per_batch == 3
    assert config.delete.likes is False
    assert config.following.execution.require_confirm_file is True
    assert config.following.safety.require_headful_for_execute is True


def test_delete_options_enabled_lists_true_fields() -> None:
    assert DeleteOptions(tweets=False, replies=False, retweets=False, likes=True).enabled() == [
        "likes"
    ]


def test_config_file_overrides_subset(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text(
        json.dumps(
            {
                "execution": {"max_per_session": 50, "per_batch": 5},
                "delete": {"likes": True},
                "following": {"rules": {"keep_handles": ["@Keep_Me", " other "]}},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.execution.max_per_session == 50
    assert config.execution.per_batch == 5
    assert config.execution.delay_between_actions_ms == 1500  # untouched default
    assert config.delete.likes is True
    assert config.following.rules.keep_handles == ("@Keep_Me", "other")


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"execution": {"max_per_sesion": 1}}, "unknown key"),
        ({"execution": {"per_batch": 10, "max_per_session": 5}}, ">= execution.per_batch"),
        ({"execution": {"refresh_every_batches": 0}}, "refresh_every_batches"),
        ({"retry": {"max_attempts": 0}}, "retry.max_attempts"),
        ({"following": {"execution": {"min_delay_ms": 9, "max_delay_ms": 1}}}, "max_delay_ms"),
        ({"delete": {"tweets": "yes"}}, "true or false"),
        ({"urls": {"tweets": "https://x.com/me"}}, "{username}"),
        ({"selectors": {}}, "own file"),
        ([], "JSON object"),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, raw: object, message: str) -> None:
    path = tmp_path / "x.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_missing_or_broken_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(broken)


def test_summary_is_json_serialisable() -> None:
    json.dumps(XConfig().summary())


@pytest.mark.parametrize(
    "value, expected",
    [(None, "false"), ("auto", "auto"), ("TRUE", "true"), ("0", "false"), ("yes", "true")],
)
def test_parse_headless_mode(value: str | None, expected: str) -> None:
    assert parse_headless_mode(value) == expected


def test_parse_headless_mode_rejects_garbage() -> None:
    with pytest.raises(ConfigError):
        parse_headless_mode("sometimes")


def test_has_saved_session(tmp_path: Path) -> None:
    assert has_saved_session(tmp_path) is False
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    assert has_saved_session(tmp_path) is False  # too small to be real
    (tmp_path / "state.json").write_text('{"cookies": [' + '"x",' * 20 + '"y"]}', encoding="utf-8")
    assert has_saved_session(tmp_path) is True
    (tmp_path / "state.json").unlink()
    (tmp_path / "profile").mkdir()
    assert has_saved_session(tmp_path) is False
    (tmp_path / "profile" / "Default").mkdir()
    assert has_saved_session(tmp_path) is True


def test_env_defaults_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(__import__("os").environ):
        if name.startswith("X_"):
            monkeypatch.delenv(name)
    env = load_env()
    assert env.headless_mode == "false"
    assert env.browser_type == "chromium"
    assert env.user_data_dir.name == "browser-data"
    assert env.target_username is None

    monkeypatch.setenv("X_HEADLESS", "auto")
    monkeypatch.setenv("X_USER_DATA_DIR", str(tmp_path / "bd"))
    monkeypatch.setenv("X_TARGET_USERNAME", "@Someone")
    monkeypatch.setenv("X_START_COUNTDOWN_SECONDS", "999")
    env = load_env()
    assert env.headless_mode == "auto"
    assert env.resolve_headless() is False  # no saved session yet
    assert env.prefers_headless_work() is True
    assert env.user_data_dir == (tmp_path / "bd").resolve()
    assert env.target_username == "someone"
    assert env.start_countdown_seconds == 60  # clamped


@pytest.mark.parametrize(
    "name, value",
    [("X_BROWSER_TYPE", "safari"), ("X_VIEWPORT_WIDTH", "10"), ("X_DEVICE_SCALE_FACTOR", "0")],
)
def test_env_rejects_bad_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_env()


def test_env_rejects_dangerous_user_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_USER_DATA_DIR", "/")
    with pytest.raises(ConfigError):
        load_env()
