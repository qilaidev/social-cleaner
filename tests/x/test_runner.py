from __future__ import annotations

import json
from pathlib import Path

from social_cleaner.platforms.x import runner


def test_run_summary_is_written(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "runs_dir", lambda: tmp_path / "runs")
    ctx = runner.RunContext(command="clean")
    ctx.username = "alice"
    ctx.stats = {"tweets": 3, "errors": 0}
    ctx.status = "completed"
    path = runner.write_summary(ctx)
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["command"] == "clean"
    assert data["username"] == "alice"
    assert data["stats"]["tweets"] == 3
    assert data["duration_ms"] >= 0
    assert data["app_version"]


def test_rate_limit_exit_code_is_distinct() -> None:
    assert runner.RATE_LIMIT_EXIT_CODE == 75
    assert runner.CANCELLED_EXIT_CODE == 130
