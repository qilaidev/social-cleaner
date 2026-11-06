from __future__ import annotations

import json
from pathlib import Path

import pytest

from social_cleaner.core.errors import ConfigError
from social_cleaner.platforms.x.selectors import (
    DEFAULT_SELECTORS,
    REQUIRED_KEYS,
    load_selector_specs,
    load_selectors,
    selectors_metadata,
)


def test_defaults_cover_every_required_key() -> None:
    flat = load_selectors()
    assert REQUIRED_KEYS <= set(flat)
    assert all(v.strip() for v in flat.values())


def test_primary_and_fallback_are_joined() -> None:
    spec = DEFAULT_SELECTORS["tweet"]
    assert spec.combined() == f"{spec.primary}, {spec.fallback}"


def test_file_overrides_and_extends(tmp_path: Path) -> None:
    path = tmp_path / "selectors.json"
    path.write_text(
        json.dumps(
            {
                "version": "9.9",
                "lastUpdated": "2030-01-01",
                "selectors": {"tweet": {"primary": "article.new", "description": "changed"}},
                "customSelectors": {"extra": {"primary": ".x", "fallback": ".y"}},
            }
        ),
        encoding="utf-8",
    )
    specs = load_selector_specs(path)
    assert specs["tweet"].primary == "article.new"
    assert specs["tweet"].fallback is None
    assert specs["extra"].combined() == ".x, .y"
    assert specs["caret"] if "caret" in specs else True
    assert selectors_metadata(path) == {
        "source": "file",
        "path": str(path),
        "version": "9.9",
        "lastUpdated": "2030-01-01",
    }
    assert selectors_metadata(None) == {"source": "builtin"}


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"selectors": {"tweet": "article"}}, "must be an object"),
        ({"selectors": {"tweet": {"primary": ""}}}, "non-empty 'primary'"),
        ({"selectors": {"tweet": {"primary": "a", "fallback": 3}}}, "fallback"),
        ({"selectors": []}, "'selectors' must be an object"),
        ([], "JSON object"),
    ],
)
def test_invalid_selector_files(tmp_path: Path, payload: object, message: str) -> None:
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_selector_specs(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_selector_specs(tmp_path / "missing.json")
