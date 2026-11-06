"""Logging setup.

Without this the application loggers propagate to a root logger nobody
configures, so ``logger.info`` is silently dropped and warnings fall back to
``logging.lastResort`` (no timestamp, no logger name). Anything logged for
troubleshooting would be invisible.

Call :func:`configure_logging` once at startup. It is idempotent.
"""

from __future__ import annotations

import logging
import sys

from .settings import settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def configure_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return

    # stderr, not stdout: the CLI writes JSON to stdout and users pipe it into jq.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level or settings.log_level)

    # httpx logs every outbound request at INFO with the full URL; that is
    # noise at our request volume and can leak query params into logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
