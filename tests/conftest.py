from __future__ import annotations

from dataclasses import replace

import pytest

from social_cleaner.core import audit, cancellation
from social_cleaner.platforms.bilibili.api import wbi
from social_cleaner.platforms.bilibili.api.client import BiliApiClient


@pytest.fixture(autouse=True)
def isolate_process_state(tmp_path, monkeypatch):
    """Reset process-wide state between tests.

    The WBI keys, the audit sink and the cancellation flag are deliberately
    global at runtime, so without this a test would inherit state from an
    earlier one (for example a mocked ``/nav`` route that is never called
    because the keys are already cached).
    """
    wbi.invalidate_cache()
    cancellation.reset_for_tests()
    # Settings is frozen, so swap the module-level binding instead of mutating.
    monkeypatch.setattr(
        audit,
        "settings",
        replace(
            audit.settings,
            audit_log_enabled=True,
            audit_log_path=tmp_path / "audit.jsonl",
        ),
    )
    audit.reset_for_tests()
    yield
    wbi.invalidate_cache()
    cancellation.reset_for_tests()
    audit.reset_for_tests()


@pytest.fixture
async def bili_client() -> BiliApiClient:
    client = BiliApiClient(sessdata="sessdata", bili_jct="csrf-token")
    yield client
    await client.close()
