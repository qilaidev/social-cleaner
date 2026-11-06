from __future__ import annotations

from dataclasses import dataclass

from social_cleaner.platforms.bilibili.api.client import BiliApiClient

from .dynamic import DynamicService
from .favorite import FavoriteService
from .following import FollowingService
from .history import HistoryService


@dataclass(frozen=True)
class CleanResult:
    count: int
    errors: int = 0
    stopped_reason: str | None = None

    @property
    def complete(self) -> bool:
        """False when the clean gave up early or hit per-item failures.

        A clean that bailed out after a page of failures must not look
        identical to one that actually emptied the account.
        """
        return self.errors == 0 and self.stopped_reason is None


class CleanerService:
    """Whole-account clean built from the per-resource services.

    Used by ``clean-all`` in the CLI. Selective work should depend on the
    underlying ``FollowingService`` / ``FavoriteService`` / etc.
    """

    def __init__(self, client: BiliApiClient) -> None:
        self._following = FollowingService(client)
        self._favorite = FavoriteService(client)
        self._dynamic = DynamicService(client)
        self._history = HistoryService(client)

    async def clear_all_followings(self, mid: int) -> CleanResult:
        return _to_result(await self._following.clear_all(mid))

    async def clear_all_favorites(self, mid: int) -> CleanResult:
        return _to_result(await self._favorite.clear_all(mid))

    async def clear_all_dynamics(self, mid: int) -> CleanResult:
        return _to_result(await self._dynamic.clear_all(mid))

    async def clear_history(self) -> CleanResult:
        await self._history.clear()
        return CleanResult(1)

    async def clean_all(self, mid: int) -> dict:
        followings = await self.clear_all_followings(mid)
        favorites = await self.clear_all_favorites(mid)
        dynamics = await self.clear_all_dynamics(mid)
        history = await self.clear_history()
        parts = (
            ("followings", followings),
            ("favorites", favorites),
            ("dynamics", dynamics),
            ("history", history),
        )
        payload: dict = {name: part.count for name, part in parts}
        stopped = {name: part.stopped_reason for name, part in parts if part.stopped_reason}
        if stopped:
            payload["stopped_reason"] = stopped
        return payload


def _to_result(raw: dict) -> CleanResult:
    errors = raw.get("errors")
    return CleanResult(
        count=int(raw.get("ok", 0)),
        errors=len(errors) if isinstance(errors, list) else 0,
        stopped_reason=raw.get("stopped_reason"),
    )
