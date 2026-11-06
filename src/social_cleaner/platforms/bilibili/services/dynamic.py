from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from social_cleaner.core import audit
from social_cleaner.core.cancellation import is_cancellation_error, is_cancellation_requested
from social_cleaner.platforms.bilibili import PLATFORM
from social_cleaner.platforms.bilibili.api import DynamicApi
from social_cleaner.platforms.bilibili.api.client import BiliApiClient

from ._progress import ItemCallback
from ._utils import extract_dynamic_id, safe_int

logger = logging.getLogger(__name__)

MAX_CLEAR_PAGES = 200


class DynamicService:
    def __init__(self, client: BiliApiClient) -> None:
        self._client = client
        self._api = DynamicApi(client)

    async def list_page(self, mid: int, offset: str | None = None) -> dict[str, Any]:
        return await self._api.get_dynamics(mid, offset=offset)

    async def iter_all(self, mid: int) -> AsyncIterator[dict[str, Any]]:
        offset: str | None = None
        safety = 0
        while True:
            if is_cancellation_requested():
                return
            data = await self._api.get_dynamics(mid, offset=offset)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                return
            for item in items:
                if isinstance(item, dict):
                    yield item
            has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
            next_offset = data.get("offset") if isinstance(data, dict) else None
            if not has_more or not next_offset or next_offset == offset:
                return
            offset = str(next_offset)
            safety += 1
            if safety > 500:
                logger.warning("iter_all reached safety limit on mid %s", mid)
                return

    async def delete_many(
        self,
        ids: Sequence[int | str],
        *,
        on_item: ItemCallback | None = None,
    ) -> dict[str, Any]:
        ok = 0
        errors: list[dict[str, Any]] = []
        for raw in ids:
            if is_cancellation_requested():
                return {
                    "ok": ok,
                    "errors": errors,
                    "total": len(ids),
                    "stopped_reason": "cancelled",
                }
            dynamic_id = safe_int(raw)
            if dynamic_id is None:
                errors.append({"id": str(raw), "type": "ValueError", "message": "invalid id"})
                continue
            try:
                await self._api.delete_dynamic(dynamic_id)
                ok += 1
                audit.record(PLATFORM, "dynamic.delete", dynamic_id, ok=True)
                if on_item is not None:
                    on_item(dynamic_id, True, None)
            except Exception as exc:
                if is_cancellation_error(exc):
                    return {
                        "ok": ok,
                        "errors": errors,
                        "total": len(ids),
                        "stopped_reason": "cancelled",
                    }
                err = {"id": dynamic_id, "type": type(exc).__name__, "message": str(exc)}
                errors.append(err)
                logger.warning("Failed to delete dynamic id=%s: %s", dynamic_id, exc)
                audit.record(PLATFORM, "dynamic.delete", dynamic_id, ok=False, error=str(exc))
                if on_item is not None:
                    on_item(dynamic_id, False, err)
        return {"ok": ok, "errors": errors, "total": len(ids)}

    async def clear_all(self, mid: int, *, on_item: ItemCallback | None = None) -> dict[str, Any]:
        ok = 0
        errors: list[dict[str, Any]] = []
        offset: str | None = None
        safety = 0
        while True:
            if is_cancellation_requested():
                return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
            try:
                data = await self._api.get_dynamics(mid, offset=offset)
            except Exception as exc:
                if is_cancellation_error(exc):
                    return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
                raise
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                break
            page_ok = 0
            page_attempted = 0
            for item in items:
                if is_cancellation_requested():
                    return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
                dynamic_id = extract_dynamic_id(item) if isinstance(item, dict) else None
                if dynamic_id is None:
                    continue
                page_attempted += 1
                try:
                    await self._api.delete_dynamic(dynamic_id)
                    ok += 1
                    page_ok += 1
                    audit.record(PLATFORM, "dynamic.delete", dynamic_id, ok=True)
                    if on_item is not None:
                        on_item(dynamic_id, True, None)
                except Exception as exc:
                    if is_cancellation_error(exc):
                        return {"ok": ok, "errors": errors, "stopped_reason": "cancelled"}
                    err = {"id": dynamic_id, "type": type(exc).__name__, "message": str(exc)}
                    errors.append(err)
                    logger.warning("Failed to delete dynamic id=%s: %s", dynamic_id, exc)
                    audit.record(PLATFORM, "dynamic.delete", dynamic_id, ok=False, error=str(exc))
                    if on_item is not None:
                        on_item(dynamic_id, False, err)
            if page_attempted and page_ok == 0:
                logger.warning(
                    "Stopped dynamic clear_all for mid=%s after a page made no progress",
                    mid,
                )
                return {"ok": ok, "errors": errors, "stopped_reason": "no_progress"}
            has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
            next_offset = data.get("offset") if isinstance(data, dict) else None
            if not has_more or not next_offset or next_offset == offset:
                break
            offset = str(next_offset)
            safety += 1
            if safety > MAX_CLEAR_PAGES:
                logger.warning(
                    "dynamic clear_all for mid=%s hit the %s-page safety limit",
                    mid,
                    MAX_CLEAR_PAGES,
                )
                return {"ok": ok, "errors": errors, "stopped_reason": "page_limit"}
        return {"ok": ok, "errors": errors}
