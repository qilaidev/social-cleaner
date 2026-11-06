from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import typer

from social_cleaner.core.cancellation import request_cancellation

from ..api import BiliApiClient
from ..runtime import build_client
from . import credentials

logger = logging.getLogger(__name__)
T = TypeVar("T")


def require_credentials() -> credentials.Credentials:
    creds = credentials.load()
    if creds is None:
        typer.echo(
            "No Bilibili credentials found. Run `social-cleaner bilibili login` "
            "or set BILI_SESSDATA / BILI_JCT.",
            err=True,
        )
        raise typer.Exit(code=1)
    return creds


def resolve_mid(explicit: int | None) -> int:
    """The owning account's mid: ``--mid`` if given, else the saved login."""
    if explicit is not None:
        return explicit
    creds = credentials.load()
    if creds and creds.mid:
        return creds.mid
    typer.echo(
        "No mid given. Pass --mid or run `social-cleaner bilibili login` first.",
        err=True,
    )
    raise typer.Exit(code=1)


@asynccontextmanager
async def make_client() -> AsyncIterator[BiliApiClient]:
    creds = require_credentials()
    async with build_client((creds.sessdata, creds.bili_jct)) as client:
        yield client


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _cancel(sig: str) -> None:
        logger.warning("Received %s; finishing current step and stopping", sig)
        request_cancellation(f"received {sig}")

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, _cancel, name)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows / non-main thread
            pass


def run_async(coro: Awaitable[T]) -> T:
    async def _wrapped() -> T:
        _install_signal_handlers(asyncio.get_running_loop())
        return await coro

    return asyncio.run(_wrapped())


def emit(obj: Any, *, json_output: bool = True) -> None:
    """Print ``obj`` as JSON (default) for machine consumers, or as a pretty
    repr if ``json_output=False``. AI agents should leave the default on."""
    if json_output:
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        typer.echo(repr(obj))
