"""One run per browser profile.

Two processes driving the same persistent Chromium profile corrupt it and
double every action against the account. The lock is a JSON file inside the
profile's parent directory with the owner's pid and a heartbeat; a stale lock
(dead pid, or no heartbeat for 15s) is reclaimed automatically.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from social_cleaner.core.errors import SocialCleanerError

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_TTL_S = 15.0
LOCK_FILENAME = "run.lock"


class RunLockError(SocialCleanerError):
    """Another run owns this profile."""


@dataclass
class LockInfo:
    pid: int
    run_id: str
    started_at: str
    last_heartbeat: str
    cwd: str
    user_data_dir: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read(path: Path) -> LockInfo | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(**{k: raw[k] for k in LockInfo.__dataclass_fields__})
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_exclusive(path: Path, info: LockInfo) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(info), fh, indent=2)


def _write_atomic(path: Path, info: LockInfo) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def is_stale(info: LockInfo, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    beat = _parse_iso(info.last_heartbeat) or _parse_iso(info.started_at)
    if beat is None or now - beat > HEARTBEAT_TTL_S:
        return True
    return not _pid_alive(info.pid)


class RunLock:
    def __init__(self, run_id: str, user_data_dir: Path) -> None:
        self.run_id = run_id
        self.user_data_dir = Path(user_data_dir)
        self.path = self.user_data_dir / LOCK_FILENAME
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._held = False

    def acquire(self) -> RunLock:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        info = LockInfo(
            pid=os.getpid(),
            run_id=self.run_id,
            started_at=_now_iso(),
            last_heartbeat=_now_iso(),
            cwd=os.getcwd(),
            user_data_dir=str(self.user_data_dir),
        )
        try:
            _write_exclusive(self.path, info)
        except FileExistsError:
            existing = _read(self.path)
            if existing is None:
                raise RunLockError(
                    f"corrupt run lock at {self.path}; make sure no run is active, then delete it"
                ) from None
            if not is_stale(existing):
                raise RunLockError(
                    f"another run is active (pid {existing.pid}, run {existing.run_id}); "
                    "stop it first or use a different X_USER_DATA_DIR"
                ) from None
            logger.warning("Reclaiming stale run lock at %s (pid %s)", self.path, existing.pid)
            self.path.unlink(missing_ok=True)
            _write_exclusive(self.path, info)
        self._held = True
        self._thread = threading.Thread(
            target=self._heartbeat, name="run-lock-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def _heartbeat(self) -> None:
        warned = False
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            existing = _read(self.path)
            if existing is None or existing.pid != os.getpid() or existing.run_id != self.run_id:
                continue
            existing.last_heartbeat = _now_iso()
            try:
                _write_atomic(self.path, existing)
                warned = False
            except OSError as exc:
                if not warned:
                    warned = True
                    logger.warning("Failed to refresh run lock heartbeat at %s: %s", self.path, exc)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        existing = _read(self.path)
        if existing and existing.pid == os.getpid() and existing.run_id == self.run_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Failed to remove run lock at %s: %s", self.path, exc)

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
