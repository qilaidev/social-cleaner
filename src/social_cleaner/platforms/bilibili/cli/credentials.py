"""Where the CLI keeps the Bilibili session.

Priority when loading:

1. ``BILI_SESSDATA`` + ``BILI_JCT`` environment variables (always win when
   both are present; ``BILI_MID`` / ``BILI_UNAME`` are optional extras).
2. The JSON file at ``BILI_CREDENTIALS_PATH`` or
   ``<data_dir>/bilibili/credentials.json`` written by ``bilibili auth login``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings import load_settings


@dataclass
class Credentials:
    sessdata: str
    bili_jct: str
    mid: int | None = None
    uname: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessdata": self.sessdata,
            "bili_jct": self.bili_jct,
            "mid": self.mid,
            "uname": self.uname,
        }


def credentials_path() -> Path:
    # Re-read so tests (and users) can redirect the path per process.
    return load_settings().credentials_path


def load() -> Credentials | None:
    env_sess = os.environ.get("BILI_SESSDATA")
    env_jct = os.environ.get("BILI_JCT")
    if env_sess and env_jct:
        return Credentials(
            sessdata=env_sess,
            bili_jct=env_jct,
            mid=_try_int(os.environ.get("BILI_MID")),
            uname=os.environ.get("BILI_UNAME"),
        )
    path = credentials_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    sess = raw.get("sessdata")
    jct = raw.get("bili_jct")
    if not sess or not jct:
        return None
    return Credentials(
        sessdata=str(sess),
        bili_jct=str(jct),
        mid=_try_int(raw.get("mid")),
        uname=raw.get("uname"),
    )


def save(creds: Credentials) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(creds.to_dict(), ensure_ascii=False, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def clear() -> bool:
    path = credentials_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
