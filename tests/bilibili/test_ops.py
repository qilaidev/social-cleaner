"""Tests for audit trail, honest completion reporting, WBI caching, and settings."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from social_cleaner.core import audit
from social_cleaner.core.settings import load_settings
from social_cleaner.platforms.bilibili import runtime
from social_cleaner.platforms.bilibili.api import wbi
from social_cleaner.platforms.bilibili.api.client import BiliApiClient
from social_cleaner.platforms.bilibili.api.relation import FOLLOWINGS_URL, MODIFY_URL
from social_cleaner.platforms.bilibili.api.wbi import NAV_URL
from social_cleaner.platforms.bilibili.services import CleanerService
from social_cleaner.platforms.bilibili.services.dynamic import DynamicService
from social_cleaner.platforms.bilibili.services.favorite import FavoriteService
from social_cleaner.platforms.bilibili.services.following import FollowingService
from social_cleaner.platforms.bilibili.settings import load_settings as load_bili_settings

pytestmark = pytest.mark.asyncio


NAV_PAYLOAD = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
        }
    },
}


# --- audit trail -----------------------------------------------------------


def _audit_entries() -> list[dict]:
    path = Path(audit.settings.audit_log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def test_unfollow_writes_audit_entries(bili_client: BiliApiClient) -> None:
    with respx.mock() as router:
        router.post(MODIFY_URL).mock(return_value=httpx.Response(200, json={"code": 0}))
        await FollowingService(bili_client).unfollow_many([11, 22])

    entries = _audit_entries()
    assert [e["target"] for e in entries] == [11, 22]
    assert all(e["action"] == "following.unfollow" and e["ok"] for e in entries)
    assert all(e["platform"] == "bilibili" for e in entries)


async def test_failed_unfollow_is_audited_with_the_error(bili_client: BiliApiClient) -> None:
    with respx.mock() as router:
        router.post(MODIFY_URL).mock(
            return_value=httpx.Response(200, json={"code": -101, "message": "not logged in"})
        )
        await FollowingService(bili_client).unfollow_many([11])

    entry = _audit_entries()[0]
    assert entry["ok"] is False
    assert "not logged in" in entry["error"]


async def test_audit_failure_never_breaks_the_clean(
    bili_client: BiliApiClient, monkeypatch
) -> None:
    """An unwritable audit sink must not abort work that is already underway."""
    monkeypatch.setattr(audit, "_initialised", True)
    monkeypatch.setattr(audit, "_sink", None)

    with respx.mock() as router:
        router.post(MODIFY_URL).mock(return_value=httpx.Response(200, json={"code": 0}))
        result = await FollowingService(bili_client).unfollow_many([11])

    assert result["ok"] == 1


# --- honest completion reporting -------------------------------------------


async def test_clear_all_flags_the_page_safety_limit(
    bili_client: BiliApiClient, monkeypatch
) -> None:
    """Hitting the page cap used to be indistinguishable from finishing."""
    monkeypatch.setattr("social_cleaner.platforms.bilibili.services.following.MAX_CLEAR_PAGES", 2)

    with respx.mock() as router:
        router.get(FOLLOWINGS_URL).mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"list": [{"mid": 7}], "total": 999}}
            )
        )
        router.post(MODIFY_URL).mock(return_value=httpx.Response(200, json={"code": 0}))
        result = await FollowingService(bili_client).clear_all(1)

    assert result["stopped_reason"] == "page_limit"


async def test_dynamic_clear_all_stops_when_a_page_makes_no_progress(
    bili_client: BiliApiClient,
) -> None:
    from social_cleaner.platforms.bilibili.api.dynamic import DELETE_DYNAMIC_URL, DYNAMICS_URL

    with respx.mock() as router:
        router.get(NAV_URL).mock(return_value=httpx.Response(200, json=NAV_PAYLOAD))
        router.get(DYNAMICS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"id_str": "100"}, {"id_str": "101"}],
                        "has_more": True,
                        "offset": "next",
                    },
                },
            )
        )
        router.post(DELETE_DYNAMIC_URL).mock(
            return_value=httpx.Response(200, json={"code": -352, "message": "risk"})
        )
        result = await DynamicService(bili_client).clear_all(1)

    assert result["ok"] == 0
    assert result["stopped_reason"] == "no_progress"


async def test_favorite_clear_all_stops_when_a_folder_makes_no_progress(
    bili_client: BiliApiClient,
) -> None:
    from social_cleaner.platforms.bilibili.api.favorite import (
        BATCH_DELETE_URL,
        FOLDERS_URL,
        RESOURCE_IDS_URL,
    )

    with respx.mock() as router:
        router.get(FOLDERS_URL).mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"list": [{"id": 1}, {"id": 2}]}}
            )
        )
        router.get(RESOURCE_IDS_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"ids": [10, 11]}})
        )
        delete_route = router.post(BATCH_DELETE_URL).mock(
            return_value=httpx.Response(200, json={"code": -101, "message": "expired"})
        )
        result = await FavoriteService(bili_client).clear_all(1)

    assert result["ok"] == 0
    assert result["stopped_reason"] == "no_progress"
    assert delete_route.call_count == 1


async def test_clean_all_service_reports_stopped_reason(bili_client: BiliApiClient) -> None:
    from social_cleaner.platforms.bilibili.api.dynamic import DYNAMICS_URL
    from social_cleaner.platforms.bilibili.api.favorite import FOLDERS_URL as FAV_FOLDERS_URL
    from social_cleaner.platforms.bilibili.api.history import CLEAR_HISTORY_URL

    with respx.mock(assert_all_called=False) as router:
        router.get(NAV_URL).mock(return_value=httpx.Response(200, json=NAV_PAYLOAD))
        router.get(FOLLOWINGS_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"list": [{"mid": 7}]}})
        )
        router.post(MODIFY_URL).mock(
            return_value=httpx.Response(200, json={"code": -101, "message": "expired"})
        )
        router.get(FAV_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"list": []}})
        )
        router.get(DYNAMICS_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"items": []}})
        )
        router.post(CLEAR_HISTORY_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {}})
        )
        result = await CleanerService(bili_client).clean_all(1)

    assert result["stopped_reason"] == {"followings": "no_progress"}


# --- WBI key caching & shared lock -----------------------------------------


async def test_wbi_keys_are_cached_across_clients() -> None:
    """Concurrent clients share the process-level cache and lock so /nav is
    called only once even when the cache starts cold."""
    with respx.mock() as router:
        route = router.get(NAV_URL).mock(return_value=httpx.Response(200, json=NAV_PAYLOAD))

        async with BiliApiClient() as first, BiliApiClient() as second:
            keys_a, keys_b = await asyncio.gather(first.get_wbi_keys(), second.get_wbi_keys())
            assert keys_a == keys_b

        assert route.call_count == 1


async def test_invalidate_clears_the_shared_cache() -> None:
    with respx.mock() as router:
        route = router.get(NAV_URL).mock(return_value=httpx.Response(200, json=NAV_PAYLOAD))

        async with BiliApiClient() as client:
            await client.get_wbi_keys()
            client.invalidate_wbi_keys()
            await client.get_wbi_keys()

        assert route.call_count == 2
        assert wbi.cached_keys() is not None


# --- settings & runtime ----------------------------------------------------


async def test_settings_fall_back_on_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("BILI_API_QPS", "not-a-number")
    monkeypatch.setenv("BILI_MAX_RETRIES", "-5")
    bili = load_bili_settings()

    assert bili.api_qps == 1.5
    assert bili.max_retries == 3


async def test_audit_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SOCIAL_CLEANER_AUDIT_LOG_ENABLED", "0")
    assert load_settings().audit_log_enabled is False


async def test_build_client_applies_configured_timeout_and_retries() -> None:
    async with runtime.build_client(("sess", "jct")) as client:
        assert client._client.timeout.read == runtime.settings.http_timeout
        assert client._max_retries == runtime.settings.max_retries
