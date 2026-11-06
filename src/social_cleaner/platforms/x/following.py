"""Reviewed unfollowing: export → classify → dry-run → execute → resume.

Unfollowing is the action most likely to trip X's risk control and the one
users most regret automating blindly, so it is split into steps with a human
in the middle:

1. ``export`` scrolls the following list and writes ``followings.jsonl`` +
   ``followings.csv`` (read-only).
2. ``classify`` applies local rules (keep/drop handles, keywords, low-info
   accounts) and writes ``candidates.jsonl``, ``keep-list.jsonl``,
   ``review.csv`` and an *empty* ``approved-unfollow.jsonl``.
3. The user copies the accounts they really want gone into
   ``approved-unfollow.jsonl``.
4. ``dry-run`` prints what ``execute`` would do without a browser.
5. ``execute`` unfollows the approved handles one by one, slowly, in a
   visible browser, persisting ``session.json`` after every item so
   ``resume`` can pick up after an interruption.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import ElementHandle, Page

from social_cleaner.core import audit
from social_cleaner.core.cancellation import is_cancellation_error, raise_if_cancelled
from social_cleaner.core.errors import ConfigError, NonRetryableError, RateLimitError
from social_cleaner.core.retry import random_sleep_ms, sleep_ms
from social_cleaner.core.settings import settings as core_settings

from . import PLATFORM
from .config import FollowingConfig, FollowingRules, XConfig, XEnv
from .deleter import DEFAULT_USER_CELL, PageHelper
from .login import normalize_handle
from .signals import RateLimitDetector, detect_risk_signal, is_blank_render

logger = logging.getLogger(__name__)

Decision = Literal["candidate", "keep"]
ItemStatus = Literal["pending", "success", "failed", "skipped"]
SessionStatus = Literal["running", "completed", "cancelled", "failed"]

AUTO_GENERATED_FILES = frozenset({"candidates.jsonl", "followings.jsonl", "keep-list.jsonl"})
EXPORT_COLUMNS = (
    "handle",
    "display_name",
    "bio",
    "is_verified",
    "follows_you",
    "profile_url",
    "avatar_url",
    "collected_at",
    "source_url",
)
REVIEW_COLUMNS = (
    "decision",
    "handle",
    "display_name",
    "bio",
    "reasons",
    "is_verified",
    "follows_you",
    "profile_url",
)


def default_output_dir() -> Path:
    return core_settings.data_dir / "x" / "followings"


def build_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{stamp}-{suffix}"


def canonical_handle(handle: str | None) -> str:
    normalized = normalize_handle(handle)
    return f"@{normalized}" if normalized else ""


# ---------------------------------------------------------------- records


@dataclass
class FollowingAccount:
    handle: str
    display_name: str | None = None
    bio: str | None = None
    is_verified: bool = False
    follows_you: bool = False
    avatar_url: str | None = None
    profile_url: str | None = None
    collected_at: str = ""
    source_url: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FollowingAccount:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class ClassifiedAccount(FollowingAccount):
    decision: Decision = "keep"
    reasons: list[str] = field(default_factory=list)


@dataclass
class SessionItem:
    handle: str
    display_name: str | None
    status: ItemStatus = "pending"
    reason: str | None = None
    processed_at: str | None = None


@dataclass
class ExecutionSession:
    run_id: str
    input_file: str
    username: str
    started_at: str
    updated_at: str
    status: SessionStatus
    max_unfollow_per_session: int
    items: list[SessionItem]
    stop_reason: str | None = None
    processed: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0

    def recount(self) -> None:
        self.processed = sum(1 for i in self.items if i.status != "pending")
        self.success = sum(1 for i in self.items if i.status == "success")
        self.failed = sum(1 for i in self.items if i.status == "failed")
        self.skipped = sum(1 for i in self.items if i.status == "skipped")

    @property
    def pending(self) -> int:
        return sum(1 for i in self.items if i.status == "pending")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExecutionSession:
        items = [SessionItem(**i) for i in raw.get("items", [])]
        data = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and k != "items"}
        return cls(items=items, **data)


# ----------------------------------------------------------------- rules


def _keyword_hit(text: str, keywords: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for keyword in keywords:
        k = keyword.strip().lower()
        if k and k in lowered:
            return keyword
    return None


def _is_low_info(account: FollowingAccount) -> bool:
    return not account.bio and not account.is_verified and not account.follows_you


def classify_account(account: FollowingAccount, rules: FollowingRules) -> ClassifiedAccount:
    handle = normalize_handle(account.handle)
    text = " ".join(filter(None, (account.handle, account.display_name, account.bio)))
    base = asdict(account)

    if handle in {normalize_handle(h) for h in rules.keep_handles}:
        return ClassifiedAccount(**base, decision="keep", reasons=["keep-handle"])
    keep_kw = _keyword_hit(text, rules.keep_keywords)
    if keep_kw:
        return ClassifiedAccount(**base, decision="keep", reasons=[f"keep-keyword:{keep_kw}"])

    reasons: list[str] = []
    if handle in {normalize_handle(h) for h in rules.drop_handles}:
        reasons.append("drop-handle")
    drop_kw = _keyword_hit(text, rules.drop_keywords)
    if drop_kw:
        reasons.append(f"drop-keyword:{drop_kw}")
    if rules.low_info_candidate and _is_low_info(account):
        reasons.append("low-info")
    if not reasons:
        return ClassifiedAccount(**base, decision="keep", reasons=["no-drop-rule"])
    return ClassifiedAccount(**base, decision="candidate", reasons=reasons)


def dedupe_accounts(accounts: list[FollowingAccount]) -> list[FollowingAccount]:
    seen: set[str] = set()
    result: list[FollowingAccount] = []
    for account in accounts:
        handle = normalize_handle(account.handle)
        if not handle or handle in seen:
            continue
        seen.add(handle)
        account.handle = canonical_handle(handle)
        result.append(account)
    return result


def classify_accounts(
    accounts: list[FollowingAccount], rules: FollowingRules
) -> list[ClassifiedAccount]:
    return [classify_account(a, rules) for a in dedupe_accounts(accounts)]


# ------------------------------------------------------------------ files


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}:{line_no}: invalid JSON ({exc})") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SAFE_CSV_COLUMNS = frozenset({"handle", "profile_url", "avatar_url", "source_url", "collected_at"})


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(columns), extrasaction="ignore", quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _csv_value(row.get(c), column=c) for c in columns})


def _csv_value(value: Any, *, column: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = "|".join(str(v) for v in value)
    else:
        text = str(value)
    if column not in _SAFE_CSV_COLUMNS and text.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_if_absent(path: Path, content: str) -> bool:
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(content)
        return True
    except FileExistsError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- export


_EXTRACT_ACCOUNTS_JS = """
(cells) => {
  const now = new Date().toISOString();
  return cells.map((el) => {
    const links = Array.from(el.querySelectorAll('a[href^="/"][role="link"]'));
    const hrefOf = (l) => l.getAttribute('href') || '';
    const profile = links.find(
      (l) => /^\\/[^/?#]+$/.test(hrefOf(l)) && !hrefOf(l).startsWith('/i/')
    );
    const href = profile ? profile.getAttribute('href') || '' : '';
    const m = href.match(/^\\/([^/?#]+)$/);
    const handle = m ? '@' + m[1].replace(/^@+/, '') : '';
    if (!handle) return null;
    const img = el.querySelector('img[src]');
    const nameNode =
      el.querySelector('[data-testid="UserName"] span') || el.querySelector('[dir="ltr"] span');
    const name = nameNode ? nameNode.textContent : null;
    const blocks = Array.from(el.querySelectorAll('[dir="auto"], [dir="ltr"]'))
      .map((n) => (n.textContent || '').trim())
      .filter(Boolean);
    const bio = blocks.find((t) => !t.includes(handle.slice(1)) && t !== name) || null;
    const body = el.textContent || '';
    return {
      handle,
      display_name: name,
      bio,
      is_verified: Boolean(el.querySelector('[data-testid="icon-verified"]')),
      follows_you: /关注了你|Follows you/i.test(body),
      avatar_url: img ? img.getAttribute('src') : null,
      profile_url: href ? 'https://x.com' + href : null,
      collected_at: now,
      source_url: window.location.href,
    };
  }).filter(Boolean);
}
"""

_HANDLE_JS = """
(el) => {
  for (const link of el.querySelectorAll('a[href^="/"][role="link"]')) {
    const href = link.getAttribute('href') || '';
    const m = href.match(/^\\/([^/?#]+)$/);
    if (m && !href.startsWith('/i/')) return '@' + m[1].replace(/^@+/, '');
  }
  return null;
}
"""

_FIND_HANDLE_INDEX_JS = """
(cells, wanted) => {
  for (let i = 0; i < cells.length; i++) {
    for (const link of cells[i].querySelectorAll('a[href^="/"][role="link"]')) {
      const href = link.getAttribute('href') || '';
      const m = href.match(/^\\/([^/?#]+)$/);
      if (m && !href.startsWith('/i/')) {
        if (m[1].trim().replace(/^@+/, '').toLowerCase() === wanted) return i;
        break;
      }
    }
  }
  return -1;
}
"""


@dataclass(frozen=True)
class ExportResult:
    run_id: str
    output_dir: Path
    jsonl_path: Path
    csv_path: Path
    count: int


class FollowingCollector:
    def __init__(
        self, page: Page, config: XConfig, username: str, output_dir: Path | None = None
    ) -> None:
        self.page = page
        self.config = config
        self.username = username
        self.helper = PageHelper(page, config.selectors)
        self.rate_limit = RateLimitDetector(page)
        self.output_dir = output_dir or default_output_dir()

    async def export(self, run_id: str | None = None) -> ExportResult:
        run_id = run_id or build_run_id()
        run_dir = self.output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        await self.page.goto(
            self.config.urls.for_user("following", self.username), wait_until="domcontentloaded"
        )
        await sleep_ms(2000)
        accounts = await self._collect()
        if not accounts and (self.rate_limit.hit_recently() or await is_blank_render(self.page)):
            raise RateLimitError(
                f"followings export: X returned 429 and the page cannot render "
                f"({self.rate_limit.count} hit(s)). Stop and wait before retrying."
            )

        jsonl_path = run_dir / "followings.jsonl"
        csv_path = run_dir / "followings.csv"
        rows = [asdict(a) for a in accounts]
        write_jsonl(jsonl_path, rows)
        write_csv(csv_path, EXPORT_COLUMNS, rows)
        logger.info("Exported %s following(s) to %s", len(accounts), jsonl_path)
        return ExportResult(run_id, run_dir, jsonl_path, csv_path, len(accounts))

    async def _collect(self) -> list[FollowingAccount]:
        seen: dict[str, FollowingAccount] = {}
        stale = 0
        previous = 0
        await self.helper.scroll_to_top()
        for _ in range(120):
            if stale >= 6:
                break
            raise_if_cancelled()
            for raw in await self._visible_accounts():
                account = FollowingAccount.from_dict(raw)
                handle = normalize_handle(account.handle)
                if handle and handle not in seen:
                    seen[handle] = account
                    logger.info("[EXPORT] %s %r", account.handle, account.display_name or "")
            if len(seen) == previous:
                stale += 1
            else:
                previous = len(seen)
                stale = 0
            await self.helper.scroll_to_bottom()
            await sleep_ms(1200)
        return list(seen.values())

    async def _visible_accounts(self) -> list[dict[str, Any]]:
        selector = self.helper.selector("userCell", DEFAULT_USER_CELL)
        try:
            return await self.page.eval_on_selector_all(selector, _EXTRACT_ACCOUNTS_JS)
        except Exception as exc:
            logger.debug("Extracting visible accounts failed: %s", exc)
            return []


# -------------------------------------------------------------- classify


@dataclass(frozen=True)
class ClassifyResult:
    output_dir: Path
    candidates_path: Path
    keep_list_path: Path
    review_path: Path
    approved_template_path: Path
    total: int
    candidates: int
    keep: int


def classify_file(input_file: Path, following: FollowingConfig) -> ClassifyResult:
    input_file = input_file.resolve()
    accounts = [FollowingAccount.from_dict(r) for r in read_jsonl(input_file)]
    classified = classify_accounts(accounts, following.rules)
    output_dir = input_file.parent
    candidates = [asdict(c) for c in classified if c.decision == "candidate"]
    keep = [asdict(c) for c in classified if c.decision == "keep"]

    candidates_path = output_dir / "candidates.jsonl"
    keep_list_path = output_dir / "keep-list.jsonl"
    review_path = output_dir / "review.csv"
    approved_path = output_dir / "approved-unfollow.jsonl"

    write_jsonl(candidates_path, candidates)
    write_jsonl(keep_list_path, keep)
    write_csv(review_path, REVIEW_COLUMNS, [asdict(c) for c in classified])
    created = write_if_absent(approved_path, "")

    logger.info("Candidates: %s → %s", len(candidates), candidates_path)
    logger.info("Keep list: %s → %s", len(keep), keep_list_path)
    logger.info("Review sheet: %s", review_path)
    if created:
        logger.warning(
            "Created an EMPTY %s. Copy the accounts you really want to unfollow from "
            "candidates.jsonl into it before running execute.",
            approved_path.name,
        )
    else:
        logger.warning("Kept the existing %s untouched", approved_path.name)

    return ClassifyResult(
        output_dir,
        candidates_path,
        keep_list_path,
        review_path,
        approved_path,
        len(classified),
        len(candidates),
        len(keep),
    )


# --------------------------------------------------------------- execute


def build_session_items(input_file: Path) -> list[SessionItem]:
    seen: set[str] = set()
    items: list[SessionItem] = []
    for row in read_jsonl(input_file):
        handle = normalize_handle(row.get("handle"))
        if not handle or handle in seen:
            continue
        seen.add(handle)
        items.append(
            SessionItem(handle=canonical_handle(handle), display_name=row.get("display_name"))
        )
    return items


def resolve_confirm_file(path: Path, following: FollowingConfig) -> Path:
    """Absolute path of the confirm file, after the safety checks."""
    absolute = Path(os.path.abspath(path))
    assert_confirm_file_safe(absolute, following)
    return absolute


def assert_confirm_file_safe(path: Path, following: FollowingConfig) -> None:
    if not following.execution.require_confirm_file:
        return
    name = path.name.lower()
    if name in AUTO_GENERATED_FILES:
        raise ConfigError(
            f"refusing to execute the auto-generated file {path.name}; review it and write the "
            "approved handles to approved-unfollow.jsonl"
        )
    if "approved" not in name and "confirm" not in name:
        raise ConfigError(
            f"the confirm file name must contain 'approved' or 'confirm' (got {path.name}); "
            "set following.execution.require_confirm_file=false to use another name"
        )


@dataclass(frozen=True)
class ExecuteResult:
    session_path: Path
    session: ExecutionSession


def dry_run(input_file: Path) -> list[SessionItem]:
    """What ``execute`` would process. Needs no browser."""
    return build_session_items(input_file.resolve())


class FollowingExecutor:
    def __init__(
        self,
        page: Page,
        config: XConfig,
        env: XEnv,
        username: str,
        *,
        headless: bool,
        output_dir: Path | None = None,
    ) -> None:
        self.page = page
        self.config = config
        self.env = env
        self.username = username
        self.headless = headless
        self.helper = PageHelper(page, config.selectors)
        self.rate_limit = RateLimitDetector(page)
        self.output_dir = output_dir or default_output_dir()

    @property
    def following(self) -> FollowingConfig:
        return self.config.following

    def _run_dir(self, run_id: str) -> Path:
        return self.output_dir / run_id

    async def execute(self, confirm_file: Path, run_id: str | None = None) -> ExecuteResult:
        if self.following.safety.require_headful_for_execute and self.headless:
            raise ConfigError("following execute requires a visible browser (X_HEADLESS=false)")
        confirm_file = resolve_confirm_file(confirm_file, self.following)

        run_dir = self._run_dir(run_id) if run_id else confirm_file.parent
        run_dir.mkdir(parents=True, exist_ok=True)
        session_path = run_dir / "session.json"
        if session_path.exists():
            session = ExecutionSession.from_dict(
                json.loads(session_path.read_text(encoding="utf-8"))
            )
        else:
            items = build_session_items(confirm_file)
            if not items:
                raise ConfigError(
                    f"{confirm_file.name} is empty; nothing to unfollow. Edit it first."
                )
            now = _now()
            session = ExecutionSession(
                run_id=run_id or run_dir.name,
                input_file=str(confirm_file),
                username=canonical_handle(self.username),
                started_at=now,
                updated_at=now,
                status="running",
                max_unfollow_per_session=self.following.execution.max_unfollow_per_session,
                items=items,
            )
        if not session.items:
            raise ConfigError("the session has no items; edit approved-unfollow.jsonl first")
        self._assert_session_user(session)

        session.status = "running"
        self._save(session_path, session)
        try:
            await self._run(session, session_path)
            return ExecuteResult(session_path, session)
        except Exception as exc:
            if is_cancellation_error(exc):
                session.status = "cancelled"
                session.stop_reason = "cancelled"
                self._save(session_path, session)
            raise

    async def resume(self, run_id: str) -> ExecuteResult:
        session_path = self._run_dir(run_id) / "session.json"
        if not session_path.exists():
            raise ConfigError(f"no session found for run {run_id} ({session_path})")
        session = ExecutionSession.from_dict(json.loads(session_path.read_text(encoding="utf-8")))
        self._assert_session_user(session)
        return await self.execute(Path(session.input_file), run_id)

    def _assert_session_user(self, session: ExecutionSession) -> None:
        if not session.username:
            raise ConfigError("session.json has no username; start a fresh export/classify/execute")
        if normalize_handle(session.username) != normalize_handle(self.username):
            raise ConfigError(
                f"session belongs to @{normalize_handle(session.username)} but the browser is "
                f"logged in as @{normalize_handle(self.username)}; refusing to continue"
            )

    def _save(self, path: Path, session: ExecutionSession) -> None:
        session.recount()
        session.updated_at = _now()
        write_json_atomic(path, session.to_dict())

    async def _run(self, session: ExecutionSession, session_path: Path) -> None:
        execution = self.following.execution
        await self.page.goto(
            self.config.urls.for_user("following", self.username), wait_until="domcontentloaded"
        )
        await sleep_ms(2000)

        unfollowed = 0
        consecutive_failures = 0
        for item in session.items:
            raise_if_cancelled()
            if item.status != "pending":
                continue
            if unfollowed >= execution.max_unfollow_per_session:
                session.stop_reason = (
                    f"max-unfollow-per-session:{execution.max_unfollow_per_session}"
                )
                break

            if self.following.safety.stop_on_risk_signals:
                if self.rate_limit.hit_recently():
                    session.status = "failed"
                    session.stop_reason = f"rate-limit:http-429({self.rate_limit.count})"
                    self._save(session_path, session)
                    raise RateLimitError(
                        f"following execute: X returned 429 ({self.rate_limit.count} hit(s)); "
                        "stopping so pending handles can be resumed later"
                    )
                signal = await detect_risk_signal(
                    self.page, "following execute", self.following.safety.risk_text_patterns
                )
                if signal:
                    session.status = "failed"
                    session.stop_reason = f"{signal.label}:{signal.reason}"
                    self._save(session_path, session)
                    raise NonRetryableError(
                        f"risk signal detected, stopping: {session.stop_reason}"
                    )

            logger.info("[UNFOLLOW] %s %r", item.handle, item.display_name or "")
            ok, reason = await self._unfollow_handle(item.handle)
            if (
                not ok
                and reason == "handle-not-found"
                and self.following.safety.stop_on_risk_signals
            ):
                if self.rate_limit.hit_recently() or await is_blank_render(self.page):
                    session.status = "failed"
                    session.stop_reason = f"rate-limit:http-429({self.rate_limit.count})"
                    self._save(session_path, session)
                    raise RateLimitError(
                        "following execute: page rendered blank / HTTP 429 while looking up "
                        f"{item.handle}; stopping so pending handles remain resumable"
                    )
            item.status = "success" if ok else "failed"
            item.reason = reason
            item.processed_at = _now()
            audit.record(
                PLATFORM,
                "following.unfollow",
                item.handle,
                ok=ok,
                error=None if ok else reason,
                reviewed=True,
            )
            self._save(session_path, session)

            if ok:
                unfollowed += 1
                consecutive_failures = 0
                if (
                    execution.cooldown_every_actions
                    and unfollowed % execution.cooldown_every_actions == 0
                ):
                    logger.warning(
                        "%s unfollows done; cooling down for %sms",
                        unfollowed,
                        execution.cooldown_ms,
                    )
                    await sleep_ms(execution.cooldown_ms)
                await random_sleep_ms(execution.min_delay_ms, execution.max_delay_ms)
            else:
                consecutive_failures += 1
                if consecutive_failures >= execution.max_consecutive_failures:
                    session.status = "failed"
                    session.stop_reason = f"max-consecutive-failures:{consecutive_failures}"
                    self._save(session_path, session)
                    logger.warning(
                        "%s consecutive failures; stopping to protect the account",
                        consecutive_failures,
                    )
                    return

        if session.pending:
            session.status = "running"
        elif session.failed:
            session.status = "failed"
        else:
            session.status = "completed"
        self._save(session_path, session)

    async def _unfollow_handle(self, handle: str) -> tuple[bool, str]:
        wanted = normalize_handle(handle)
        cell = await self._find_cell(wanted)
        if cell is None:
            return False, "handle-not-found"
        actual = normalize_handle(await self._handle_of(cell))
        if actual != wanted:
            return False, f"handle-mismatch:{actual or 'unknown'}"
        ok = await self.helper.unfollow(cell)
        return (True, "unfollowed") if ok else (False, "click-failed")

    async def _find_cell_in_view(self, handle: str) -> ElementHandle | None:
        cells = await self.helper.user_cells()
        if not cells:
            return None
        eval_all = getattr(self.page, "eval_on_selector_all", None)
        if callable(eval_all):
            try:
                index = await eval_all(
                    self.helper.selector("userCell", DEFAULT_USER_CELL),
                    _FIND_HANDLE_INDEX_JS,
                    handle,
                )
                if isinstance(index, int):
                    return cells[index] if 0 <= index < len(cells) else None
            except Exception as exc:
                logger.debug("Batch cell lookup failed, falling back to per-cell scan: %s", exc)
        for cell in cells:
            if normalize_handle(await self._handle_of(cell)) == handle:
                return cell
        return None

    async def _find_cell(self, handle: str) -> ElementHandle | None:
        # Fast path: check currently visible cells before scrolling back to top,
        # since approved-unfollow.jsonl is normally ordered top-to-bottom.
        cell = await self._find_cell_in_view(handle)
        if cell is not None:
            return cell

        await self.helper.scroll_to_top()
        for _ in range(40):
            raise_if_cancelled()
            cell = await self._find_cell_in_view(handle)
            if cell is not None:
                return cell
            await self.helper.scroll_to_bottom()
            await sleep_ms(900)
        return None

    async def _handle_of(self, cell: ElementHandle) -> str | None:
        try:
            return await cell.evaluate(_HANDLE_JS)
        except Exception as exc:
            logger.debug("Reading handle failed: %s", exc)
            return None
