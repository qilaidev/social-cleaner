"""Append-only audit trail of destructive operations.

Every unfollow / delete / clear is written as one JSON object per line. None
of the supported platforms offer an undo, so this file is the only record of
what was removed. Keep it if you may need to reconstruct a follow list or
explain what a run actually did.

Failures here never propagate: an unwritable audit file must not abort a
clean that is already half-done. If the sink cannot be opened it is disabled
and the reason is logged once.
"""

from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .settings import settings

logger = logging.getLogger(__name__)

AUDIT_LOGGER_NAME = "social_cleaner.audit"

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_sink: logging.Logger | None = None
_initialised = False


def _ensure_private_file(path: Path) -> None:
    """Ensure the audit file exists with owner-only (0600) permissions."""
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
    else:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _build_sink() -> logging.Logger | None:
    if not settings.audit_log_enabled:
        return None
    path = Path(settings.audit_log_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_file(path)
        handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        logger.error(
            "Audit log disabled: cannot open %s (%s). Set SOCIAL_CLEANER_AUDIT_LOG_PATH to a "
            "writable location or SOCIAL_CLEANER_AUDIT_LOG_ENABLED=0 to silence this.",
            path,
            exc,
        )
        return None

    handler.setFormatter(logging.Formatter("%(message)s"))
    sink = logging.getLogger(AUDIT_LOGGER_NAME)
    sink.handlers = [handler]
    sink.setLevel(logging.INFO)
    # Audit records are machine-readable JSON; keep them out of the human log.
    sink.propagate = False
    logger.info("Audit log enabled at %s", path)
    return sink


def _get_sink() -> logging.Logger | None:
    global _sink, _initialised
    if not _initialised:
        _sink = _build_sink()
        _initialised = True
    return _sink


def reset_for_tests() -> None:
    """Drop the cached sink so a test can point the audit log elsewhere."""
    global _sink, _initialised
    if _sink is not None:
        for handler in _sink.handlers:
            handler.close()
        _sink.handlers = []
    _sink = None
    _initialised = False


def record(
    platform: str,
    action: str,
    target: Any,
    *,
    ok: bool,
    error: str | None = None,
    **extra: Any,
) -> None:
    """Append one audit entry and flush immediately.

    ``platform`` is the platform id (``bilibili``, ``x``); ``action`` is a
    dotted verb such as ``following.unfollow``; ``target`` identifies what was
    removed.
    """
    sink = _get_sink()
    if sink is None:
        return
    entry: dict[str, Any] = {
        "ts": time.time(),
        "platform": platform,
        "action": action,
        "target": target,
        "ok": ok,
    }
    if error:
        entry["error"] = error
    if extra:
        entry.update(extra)
    try:
        sink.info(json.dumps(entry, ensure_ascii=False, default=str))
        for handler in sink.handlers:
            handler.flush()
    except Exception:  # pragma: no cover - defensive, must never break a clean
        logger.exception("Failed to write audit entry for %s.%s", platform, action)
