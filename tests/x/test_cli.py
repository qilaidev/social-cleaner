from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from social_cleaner.cli import app
from social_cleaner.platforms.x.following import write_jsonl

runner = CliRunner()


def test_root_help_lists_platforms() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "bilibili" in result.output
    assert "x" in result.output
    assert "  serve " not in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_x_help_and_followings_help() -> None:
    assert runner.invoke(app, ["x", "--help"]).exit_code == 0
    result = runner.invoke(app, ["x", "followings", "--help"])
    assert result.exit_code == 0
    for name in ("export", "classify", "dry-run", "execute", "resume", "clear"):
        assert name in result.output


def test_classify_and_dry_run_need_no_browser(tmp_path: Path) -> None:
    src = tmp_path / "followings.jsonl"
    write_jsonl(
        src,
        [
            {"handle": "@keep", "display_name": "Keep", "bio": "coding daily"},
            {"handle": "@drop", "display_name": "Drop", "bio": "free airdrop"},
        ],
    )
    result = runner.invoke(app, ["x", "followings", "classify", "--input", str(src)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["candidates"] == 1
    approved = Path(payload["approved_file"])
    approved.write_text('{"handle": "@drop"}\n', encoding="utf-8")

    result = runner.invoke(app, ["x", "followings", "dry-run", "--input", str(approved)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "count": 1,
        "items": [{"handle": "@drop", "display_name": None}],
    }


def test_clean_rejects_empty_selection() -> None:
    result = runner.invoke(
        app,
        [
            "x",
            "clean",
            "--no-tweets",
            "--no-replies",
            "--no-retweets",
            "--no-likes",
            "--no-bookmarks",
        ],
    )
    assert result.exit_code == 2
    assert "nothing selected" in result.output


def test_bad_config_file_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "x.json"
    bad.write_text('{"execution": {"typo": 1}}', encoding="utf-8")
    result = runner.invoke(app, ["x", "whoami", "--config", str(bad)])
    assert result.exit_code == 2
    assert "unknown key" in result.output
