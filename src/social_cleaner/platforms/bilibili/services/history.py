from __future__ import annotations

from typing import Any

from social_cleaner.core import audit
from social_cleaner.platforms.bilibili import PLATFORM
from social_cleaner.platforms.bilibili.api import HistoryApi
from social_cleaner.platforms.bilibili.api.client import BiliApiClient


class HistoryService:
    def __init__(self, client: BiliApiClient) -> None:
        self._api = HistoryApi(client)

    async def list_page(
        self,
        *,
        max_id: int = 0,
        business: str = "",
        view_at: int = 0,
        page_size: int = 20,
        type_: str = "all",
    ) -> dict[str, Any]:
        return await self._api.list_history(
            max_id=max_id,
            business=business,
            view_at=view_at,
            ps=page_size,
            type_=type_,
        )

    async def delete(self, kid: str) -> dict[str, Any]:
        try:
            result = await self._api.delete_history(kid)
        except Exception as exc:
            audit.record(PLATFORM, "history.delete", kid, ok=False, error=str(exc))
            raise
        audit.record(PLATFORM, "history.delete", kid, ok=True)
        return result

    async def clear(self) -> dict[str, Any]:
        try:
            result = await self._api.clear_history()
        except Exception as exc:
            audit.record(PLATFORM, "history.clear", None, ok=False, error=str(exc))
            raise
        audit.record(PLATFORM, "history.clear", None, ok=True)
        return result
