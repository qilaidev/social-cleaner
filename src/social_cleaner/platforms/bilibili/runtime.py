"""Single place where an outbound Bilibili client is configured.

Every caller must go through :func:`build_client`. Constructing
``BiliApiClient`` inline silently skips the configured timeout, rate limit
and retry policy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .api import BiliApiClient
from .settings import settings


def build_client(auth: tuple[str, str] | None = None, *, qps: float | None = None) -> BiliApiClient:
    sessdata, bili_jct = auth if auth else (None, None)
    return BiliApiClient(
        sessdata=sessdata,
        bili_jct=bili_jct,
        qps=settings.api_qps if qps is None else qps,
        timeout=settings.http_timeout,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
    )


@asynccontextmanager
async def authed_client(
    auth: tuple[str, str], qps: float | None = None
) -> AsyncIterator[BiliApiClient]:
    async with build_client(auth, qps=qps) as client:
        yield client


@asynccontextmanager
async def anon_client(qps: float | None = None) -> AsyncIterator[BiliApiClient]:
    """For the QR-code login flow, which has no credentials yet."""
    async with build_client(None, qps=qps) as client:
        yield client
