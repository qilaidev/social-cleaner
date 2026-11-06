from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from social_cleaner.core.errors import ConfigError, RateLimitError
from social_cleaner.platforms.x.config import (
    FollowingConfig,
    FollowingExecution,
    FollowingRules,
    XConfig,
    load_env,
)
from social_cleaner.platforms.x.following import (
    ExecutionSession,
    FollowingAccount,
    FollowingCollector,
    FollowingExecutor,
    SessionItem,
    assert_confirm_file_safe,
    build_run_id,
    build_session_items,
    classify_account,
    classify_accounts,
    classify_file,
    dry_run,
    read_jsonl,
    write_csv,
    write_jsonl,
)


def account(**kw) -> FollowingAccount:
    base = dict(
        handle="@Alice",
        display_name="Alice",
        bio="AI researcher",
        is_verified=False,
        follows_you=False,
    )
    base.update(kw)
    return FollowingAccount(**base)


def test_keep_handle_wins_over_everything() -> None:
    rules = FollowingRules(keep_handles=("alice",), drop_handles=("alice",), drop_keywords=("ai",))
    result = classify_account(account(), rules)
    assert result.decision == "keep"
    assert result.reasons == ["keep-handle"]


def test_keep_keyword_beats_drop_rules() -> None:
    rules = FollowingRules(keep_keywords=("researcher",), drop_keywords=("ai",))
    assert classify_account(account(), rules).reasons == ["keep-keyword:researcher"]


def test_drop_reasons_accumulate() -> None:
    rules = FollowingRules(drop_handles=("@ALICE",), drop_keywords=("airdrop", "AI"))
    result = classify_account(account(bio="free AIRDROP"), rules)
    assert result.decision == "candidate"
    assert result.reasons == ["drop-handle", "drop-keyword:airdrop"]


def test_low_info_accounts_are_candidates_unless_disabled() -> None:
    quiet = account(bio=None, is_verified=False, follows_you=False)
    assert classify_account(quiet, FollowingRules(drop_keywords=())).reasons == ["low-info"]
    assert (
        classify_account(quiet, FollowingRules(drop_keywords=(), low_info_candidate=False)).decision
        == "keep"
    )
    assert (
        classify_account(
            account(bio=None, follows_you=True), FollowingRules(drop_keywords=())
        ).decision
        == "keep"
    )


def test_classify_accounts_dedupes_and_canonicalises() -> None:
    rows = [
        account(handle="@Alice"),
        account(handle="alice"),
        account(handle=""),
        account(handle="@bob", bio=None),
    ]
    result = classify_accounts(rows, FollowingRules(drop_keywords=()))
    assert [r.handle for r in result] == ["@alice", "@bob"]
    assert result[1].decision == "candidate"


def test_classify_file_writes_review_artifacts(tmp_path: Path) -> None:
    src = tmp_path / "followings.jsonl"
    write_jsonl(src, [account().__dict__, account(handle="@spam", bio="casino bonus").__dict__])
    result = classify_file(src, FollowingConfig())
    assert result.total == 2
    assert result.candidates == 1
    assert result.keep == 1
    assert (tmp_path / "candidates.jsonl").exists()
    assert (tmp_path / "keep-list.jsonl").exists()
    assert (tmp_path / "review.csv").read_text(encoding="utf-8").startswith('"decision","handle"')
    approved = tmp_path / "approved-unfollow.jsonl"
    assert approved.exists() and approved.read_text(encoding="utf-8") == ""
    # A second run must not clobber an edited approval list.
    approved.write_text('{"handle": "@spam"}\n', encoding="utf-8")
    classify_file(src, FollowingConfig())
    assert approved.read_text(encoding="utf-8") == '{"handle": "@spam"}\n'


def test_read_jsonl_reports_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text('{"handle": "@a"}\n\nnot json\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="x.jsonl:3"):
        read_jsonl(path)


def test_write_csv_flattens_lists_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    write_csv(path, ("a", "b"), [{"a": ["x", "y"], "b": 'say "hi"'}])
    assert path.read_text(encoding="utf-8").splitlines()[1] == '"x|y","say ""hi"""'


def test_write_csv_neutralizes_formula_injection_except_handle(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(
        path,
        ("handle", "display_name", "bio"),
        [{"handle": "@alice", "display_name": "=CMD()", "bio": "+SUM(A1:A2)"}],
    )
    line = path.read_text(encoding="utf-8").splitlines()[1]
    assert line == '"@alice","\'=CMD()","\'+SUM(A1:A2)"'


def test_build_session_items_dedupes(tmp_path: Path) -> None:
    path = tmp_path / "approved-unfollow.jsonl"
    write_jsonl(
        path,
        [{"handle": "@A"}, {"handle": "a", "display_name": "dup"}, {"handle": ""}, {"nope": 1}],
    )
    items = build_session_items(path)
    assert [i.handle for i in items] == ["@a"]
    assert dry_run(path) == items


def test_confirm_file_safety() -> None:
    cfg = FollowingConfig()
    with pytest.raises(ConfigError, match="auto-generated"):
        assert_confirm_file_safe(Path("/tmp/candidates.jsonl"), cfg)
    with pytest.raises(ConfigError, match="approved"):
        assert_confirm_file_safe(Path("/tmp/list.jsonl"), cfg)
    assert_confirm_file_safe(Path("/tmp/approved-unfollow.jsonl"), cfg)
    assert_confirm_file_safe(Path("/tmp/my-confirm.jsonl"), cfg)
    relaxed = FollowingConfig(execution=FollowingExecution(require_confirm_file=False))
    assert_confirm_file_safe(Path("/tmp/candidates.jsonl"), relaxed)


def test_session_round_trip_and_recount() -> None:
    session = ExecutionSession(
        run_id="r",
        input_file="f",
        username="@me",
        started_at="t",
        updated_at="t",
        status="running",
        max_unfollow_per_session=10,
        items=[
            SessionItem("@a", None, "success"),
            SessionItem("@b", None),
            SessionItem("@c", None, "failed"),
        ],
    )
    session.recount()
    assert (session.processed, session.success, session.failed, session.pending) == (2, 1, 1, 1)
    restored = ExecutionSession.from_dict(json.loads(json.dumps(session.to_dict())))
    assert restored == session


def test_run_id_is_sortable_and_unique() -> None:
    a, b = build_run_id(), build_run_id()
    assert a != b
    assert a[:8].isdigit() and "T" in a


class _FakeCell:
    def __init__(self, handle: str) -> None:
        self.handle = handle

    async def evaluate(self, _js: str) -> str:
        return self.handle


class _FakeFollowingPage:
    def __init__(self, cells: list[_FakeCell] | None = None, *, blank: bool = False) -> None:
        self.url = "https://x.com/me/following"
        self.cells = cells or []
        self.blank = blank
        self.handlers: dict[str, Any] = {}
        self.scrolled_top = 0

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def goto(self, url: str, **_kw: Any) -> None:
        self.url = url

    async def evaluate(self, script: str) -> None:
        if "0, 0" in script:
            self.scrolled_top += 1

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def query_selector_all(self, _selector: str) -> list[_FakeCell]:
        return self.cells

    async def eval_on_selector_all(self, _selector: str, js: str, arg: Any = None) -> Any:
        if arg is None:
            return []
        for idx, cell in enumerate(self.cells):
            if cell.handle.lstrip("@").lower() == str(arg).lower():
                return idx
        return -1

    async def content(self) -> str:
        return "x" * (10_000 if self.blank else 100)


def test_export_raises_rate_limit_error_on_blank_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "social_cleaner.platforms.x.following.sleep_ms", lambda _ms: asyncio.sleep(0)
    )
    page = _FakeFollowingPage(cells=[], blank=True)
    collector = FollowingCollector(page, XConfig(), "me", output_dir=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(RateLimitError, match="followings export"):
        asyncio.run(collector.export("run-429"))


def test_find_cell_uses_viewport_batch_lookup_before_scrolling_top(tmp_path: Path) -> None:
    cell_a = _FakeCell("@alice")
    cell_b = _FakeCell("@bob")
    page = _FakeFollowingPage(cells=[cell_a, cell_b])
    executor = FollowingExecutor(
        page,
        XConfig(),
        load_env(),
        "me",
        headless=False,
        output_dir=tmp_path,  # type: ignore[arg-type]
    )
    found = asyncio.run(executor._find_cell("bob"))
    assert found is cell_b
    assert page.scrolled_top == 0


def test_execute_aborts_on_429_and_keeps_items_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "social_cleaner.platforms.x.following.sleep_ms", lambda _ms: asyncio.sleep(0)
    )
    approved = tmp_path / "approved-unfollow.jsonl"
    write_jsonl(approved, [{"handle": "@alice"}, {"handle": "@bob"}])
    page = _FakeFollowingPage(cells=[], blank=True)
    executor = FollowingExecutor(
        page,
        XConfig(),
        load_env(),
        "me",
        headless=False,
        output_dir=tmp_path,  # type: ignore[arg-type]
    )
    with pytest.raises(RateLimitError):
        asyncio.run(executor.execute(approved, "run-exec-429"))
    session = json.loads((tmp_path / "run-exec-429" / "session.json").read_text(encoding="utf-8"))
    assert session["status"] == "failed"
    assert session["stop_reason"].startswith("rate-limit:http-429")
    assert [item["status"] for item in session["items"]] == ["pending", "pending"]
