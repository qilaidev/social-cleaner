from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from social_cleaner.platforms.bilibili.api import AuthApi
from social_cleaner.platforms.bilibili.api.auth import GENERATE_QRCODE_URL, POLL_QRCODE_URL
from social_cleaner.platforms.bilibili.api.dynamic import DELETE_DYNAMIC_URL, DYNAMICS_URL
from social_cleaner.platforms.bilibili.api.favorite import (
    BATCH_DELETE_URL,
    FOLDERS_URL,
    RESOURCE_IDS_URL,
)
from social_cleaner.platforms.bilibili.api.history import CLEAR_HISTORY_URL
from social_cleaner.platforms.bilibili.api.relation import FOLLOWINGS_URL, MODIFY_URL
from social_cleaner.platforms.bilibili.api.wbi import NAV_URL

NAV_PAYLOAD = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/a.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/b.png",
        }
    },
}

pytestmark = pytest.mark.asyncio


def parse_form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


async def test_generate_and_poll_qrcode(bili_client) -> None:
    with respx.mock(assert_all_called=True, assert_all_mocked=False) as router:
        router.get(GENERATE_QRCODE_URL).mock(
            return_value=httpx.Response(
                200,
                json={"code": 0, "data": {"url": "https://qrcode", "qrcode_key": "key"}},
            )
        )
        router.get(POLL_QRCODE_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"status": 1}})
        )
        api = AuthApi(bili_client)
        url, key = await api.generate_qrcode()
        polled = await api.poll_qrcode(key)

    assert url == "https://qrcode"
    assert key == "key"
    assert polled["status"] == 1


async def test_cleaner_service_clears_followings(bili_client) -> None:
    from social_cleaner.platforms.bilibili.services import CleanerService

    followings_responses = [
        httpx.Response(200, json={"code": 0, "data": {"list": [{"mid": 1}, {"mid": 2}]}}),
        httpx.Response(200, json={"code": 0, "data": {"list": []}}),
    ]
    with respx.mock(assert_all_called=True, assert_all_mocked=False) as router:
        router.get(FOLLOWINGS_URL).mock(side_effect=followings_responses)
        modify_route = router.post(MODIFY_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"ok": True}})
        )
        result = await CleanerService(bili_client).clear_all_followings(123)

    assert result.complete is True
    assert result.count == 2
    assert len(modify_route.calls) == 2
    assert parse_form(modify_route.calls[0].request)["fid"] == ["1"]


async def test_cleaner_service_clears_favorites(bili_client) -> None:
    from social_cleaner.platforms.bilibili.services import CleanerService

    with respx.mock(assert_all_called=True, assert_all_mocked=False) as router:
        router.get(FOLDERS_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"list": [{"id": 10}]}})
        )
        router.get(RESOURCE_IDS_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"ids": [1, 2]}})
        )
        delete_route = router.post(BATCH_DELETE_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"success": True}})
        )
        result = await CleanerService(bili_client).clear_all_favorites(456)

    assert result.complete is True
    assert result.count == 2
    assert parse_form(delete_route.calls[0].request)["media_id"] == ["10"]


async def test_cleaner_service_clears_dynamics(bili_client) -> None:
    from social_cleaner.platforms.bilibili.services import CleanerService

    with respx.mock(assert_all_called=True, assert_all_mocked=False) as router:
        router.get(NAV_URL).mock(return_value=httpx.Response(200, json=NAV_PAYLOAD))
        router.get(DYNAMICS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{"id": 111}, {"id": 222}], "has_more": False}},
            )
        )
        delete_route = router.post(DELETE_DYNAMIC_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"ok": True}})
        )
        result = await CleanerService(bili_client).clear_all_dynamics(999)

    assert result.count == 2
    assert len(delete_route.calls) == 2


async def test_cleaner_service_clears_history(bili_client) -> None:
    from social_cleaner.platforms.bilibili.services import CleanerService

    with respx.mock(assert_all_called=True, assert_all_mocked=False) as router:
        router.post(CLEAR_HISTORY_URL).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"ok": True}})
        )
        result = await CleanerService(bili_client).clear_history()

    assert result.count == 1


async def test_cleaner_service_reports_partial_clean(bili_client) -> None:
    from social_cleaner.platforms.bilibili.services import CleanerService

    with respx.mock() as router:
        router.get(FOLLOWINGS_URL).mock(
            side_effect=[
                httpx.Response(200, json={"code": 0, "data": {"list": [{"mid": 7}]}}),
                httpx.Response(200, json={"code": 0, "data": {"list": []}}),
            ]
        )
        router.post(MODIFY_URL).mock(
            return_value=httpx.Response(200, json={"code": -101, "message": "expired"})
        )
        result = await CleanerService(bili_client).clear_all_followings(1)

    assert result.complete is False
    assert result.errors == 1
    assert result.stopped_reason == "no_progress"
