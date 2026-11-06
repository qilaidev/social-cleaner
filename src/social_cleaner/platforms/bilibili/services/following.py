from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from social_cleaner.core import audit
from social_cleaner.core.cancellation import is_cancellation_error, is_cancellation_requested
from social_cleaner.platforms.bilibili import PLATFORM
from social_cleaner.platforms.bilibili.api import RelationApi, UserApi
from social_cleaner.platforms.bilibili.api.client import BiliApiClient, BiliApiError

from ._progress import ItemCallback
from ._utils import extract_following_mids

logger = logging.getLogger(__name__)

MAX_CLEAR_PAGES = 200


class FollowingService:
    def __init__(self, client: BiliApiClient) -> None:
        self._client = client
        self._relation_api = RelationApi(client)
        self._user_api = UserApi(client)

    async def list_page(
        self,
        mid: int,
        *,
        page: int = 1,
        page_size: int = 50,
        order: str = "desc",
        order_type: str = "attention",
    ) -> dict[str, Any]:
        return await self._relation_api.get_followings(
            mid, pn=page, ps=page_size, order=order, order_type=order_type
        )

    async def iter_all(
        self,
        mid: int,
        *,
        page_size: int = 50,
        order: str = "desc",
        order_type: str = "attention",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each following item across all pages."""
        page = 1
        safety = 0
        while True:
            if is_cancellation_requested():
                return
            data = await self._relation_api.get_followings(
                mid, pn=page, ps=page_size, order=order, order_type=order_type
            )
            items = data.get("list") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                return
            for item in items:
                if isinstance(item, dict):
                    yield item
            if len(items) < page_size:
                return
            page += 1
            safety += 1
            if safety > 500:
                logger.warning("iter_all reached safety limit at page=%s", page)
                return

    async def get_detail(self, target_mid: int) -> dict[str, Any]:
        """Combined profile + relation-stat + latest video for a single UP."""

        async def _info() -> dict[str, Any]:
            try:
                return await self._user_api.get_info(target_mid)
            except BiliApiError as exc:
                return {"_error": str(exc), "_code": exc.code}

        async def _stat() -> dict[str, Any]:
            try:
                return await self._user_api.get_stat(target_mid)
            except BiliApiError as exc:
                return {"_error": str(exc), "_code": exc.code}

        async def _videos() -> tuple[Any, int | None]:
            try:
                videos = await self._user_api.get_videos(target_mid, pn=1, ps=1)
                vlist = videos.get("list", {}).get("vlist") if isinstance(videos, dict) else None
                latest = vlist[0] if isinstance(vlist, list) and vlist else None
                count = videos.get("page", {}).get("count") if isinstance(videos, dict) else None
                return latest, count
            except BiliApiError as exc:
                return {"_error": str(exc), "_code": exc.code}, None

        info, stat, (latest, video_count) = await asyncio.gather(_info(), _stat(), _videos())
        return {
            "mid": target_mid,
            "info": info,
            "stat": stat,
            "latest_video": latest,
            "video_count": video_count,
        }

    async def enrich(
        self,
        mids: Sequence[int],
        *,
        concurrency: int = 3,
    ) -> list[dict[str, Any]]:
        """Fetch detail for many mids in parallel under ``concurrency`` limit."""
        sem = asyncio.Semaphore(concurrency)

        async def one(target: int) -> dict[str, Any]:
            async with sem:
                return await self.get_detail(target)

        return await asyncio.gather(*(one(m) for m in mids))

    async def unfollow_many(
        self,
        mids: Sequence[int],
        *,
        on_item: ItemCallback | None = None,
    ) -> dict[str, Any]:
        """Unfollow each mid sequentially. ``on_item`` is a callable
        ``(mid, ok, error)`` invoked after each attempt for progress tracking."""
        ok = 0
        errors: list[dict[str, Any]] = []
        for target in mids:
            if is_cancellation_requested():
                return {
                    "ok": ok,
                    "errors": errors,
                    "total": len(mids),
                    "stopped_reason": "cancelled",
                }
            try:
                await self._relation_api.unfollow(target)
                ok += 1
                audit.record(PLATFORM, "following.unfollow", target, ok=True)
                if on_item is not None:
                    on_item(target, True, None)
            except Exception as exc:
                if is_cancellation_error(exc):
                    return {
                        "ok": ok,
                        "errors": errors,
                        "total": len(mids),
                        "stopped_reason": "cancelled",
                    }
                err = {"mid": target, "type": type(exc).__name__, "message": str(exc)}
                errors.append(err)
                logger.warning("Failed to unfollow mid=%s: %s", target, exc)
                audit.record(PLATFORM, "following.unfollow", target, ok=False, error=str(exc))
                if on_item is not None:
                    on_item(target, False, err)
        return {"ok": ok, "errors": errors, "total": len(mids)}

    async def clear_all(
        self,
        mid: int,
        *,
        on_item: ItemCallback | None = None,
    ) -> dict[str, Any]:
        ok = 0
        errors: list[dict[str, Any]] = []
        safety = 0
        while True:
            if is_cancellation_requested():
                return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
            try:
                data = await self._relation_api.get_followings(mid, pn=1, ps=50)
            except Exception as exc:
                if is_cancellation_error(exc):
                    return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
                raise
            target_mids = extract_following_mids(data)
            if not target_mids:
                break
            page_ok = 0
            for target in target_mids:
                if is_cancellation_requested():
                    return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
                try:
                    await self._relation_api.unfollow(target)
                    ok += 1
                    page_ok += 1
                    audit.record(PLATFORM, "following.unfollow", target, ok=True)
                    if on_item is not None:
                        on_item(target, True, None)
                except Exception as exc:
                    if is_cancellation_error(exc):
                        return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
                    err = {"mid": target, "type": type(exc).__name__, "message": str(exc)}
                    errors.append(err)
                    logger.warning("Failed to unfollow mid=%s: %s", target, exc)
                    audit.record(PLATFORM, "following.unfollow", target, ok=False, error=str(exc))
                    if on_item is not None:
                        on_item(target, False, err)
            if page_ok == 0:
                logger.warning(
                    "Stopped clear_all for mid=%s after a page made no progress",
                    mid,
                )
                return {"ok": ok, "errors": errors, "stopped_reason": "no_progress"}
            safety += 1
            if safety > MAX_CLEAR_PAGES:
                # Bailing out here used to look identical to a finished clean,
                # so the caller reported success while followings remained.
                logger.warning(
                    "clear_all for mid=%s hit the %s-page safety limit with items left",
                    mid,
                    MAX_CLEAR_PAGES,
                )
                return {"ok": ok, "errors": errors, "stopped_reason": "page_limit"}
        return {"ok": ok, "errors": errors}
