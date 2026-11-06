from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from social_cleaner.platforms.x.run_lock import (
    LOCK_FILENAME,
    RunLock,
    RunLockError,
    _read,
    _write_atomic,
    is_stale,
)


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    with RunLock("run-1", tmp_path) as lock:
        assert lock.path.exists()
        info = _read(lock.path)
        assert info is not None and info.pid == os.getpid() and info.run_id == "run-1"
        with pytest.raises(RunLockError, match="another run is active"):
            RunLock("run-2", tmp_path).acquire()
    assert not (tmp_path / LOCK_FILENAME).exists()


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / LOCK_FILENAME
    path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "run_id": "old",
                "started_at": "2020-01-01T00:00:00+00:00",
                "last_heartbeat": "2020-01-01T00:00:00+00:00",
                "cwd": "/",
                "user_data_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    assert is_stale(_read(path))  # type: ignore[arg-type]
    with RunLock("new", tmp_path) as lock:
        assert _read(lock.path).run_id == "new"  # type: ignore[union-attr]


def test_corrupt_lock_is_reported(tmp_path: Path) -> None:
    (tmp_path / LOCK_FILENAME).write_text("garbage", encoding="utf-8")
    with pytest.raises(RunLockError, match="corrupt"):
        RunLock("x", tmp_path).acquire()


def test_release_does_not_remove_someone_elses_lock(tmp_path: Path) -> None:
    lock = RunLock("mine", tmp_path).acquire()
    info = _read(lock.path)
    assert info is not None
    info.last_heartbeat = "2030-01-01T00:00:00+00:00"
    _write_atomic(lock.path, info)
    assert _read(lock.path).last_heartbeat == "2030-01-01T00:00:00+00:00"  # type: ignore[union-attr]
    lock.path.write_text(
        json.dumps({**json.loads(lock.path.read_text()), "run_id": "theirs"}), encoding="utf-8"
    )
    lock.release()
    assert lock.path.exists()
    lock.path.unlink()
