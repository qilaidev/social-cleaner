"""Bulk actions on the logged-in x.com UI.

Every loop here has the same shape: open a listing page, act on the first
few items the DOM shows, refresh, repeat until the per-session cap is reached
or several consecutive rounds find nothing. Before concluding "nothing left"
the loop asks :mod:`.signals` whether the emptiness is really a 429.

Items are identified by tweet id / handle so a card that the page did not
remove after a successful action is not clicked twice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, fields
from typing import Any, Literal

from playwright.async_api import ElementHandle, Page

from social_cleaner.core import audit
from social_cleaner.core.cancellation import is_cancellation_error, raise_if_cancelled
from social_cleaner.core.errors import NonRetryableError, RateLimitError, RetryableError
from social_cleaner.core.retry import RetryPolicy, random_sleep_ms, sleep_ms, with_retry

from . import PLATFORM
from .config import XConfig, XEnv
from .signals import BlockingState, RateLimitDetector, blocking_state, is_blank_render, pathname_of

logger = logging.getLogger(__name__)

ContentType = Literal["tweets", "replies", "retweets", "likes", "bookmarks", "following"]
ActionOutcome = Literal["deleted", "skipped", "failed"]

DELETE_MENU_ITEM = re.compile(r"^(删除|Delete)(帖子| post)?$", re.I)
DEFAULT_USER_CELL = '[data-testid="UserCell"]'


@dataclass
class DeleteStats:
    tweets: int = 0
    replies: int = 0
    retweets: int = 0
    likes: int = 0
    bookmarks: int = 0
    following: int = 0
    errors: int = 0

    def total(self) -> int:
        return sum(getattr(self, f.name) for f in fields(self) if f.name != "errors")

    def to_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class TweetInfo:
    tweet_id: str | None
    author_handle: str | None
    text_preview: str | None


@dataclass(frozen=True)
class UserInfo:
    handle: str | None
    display_name: str | None


@dataclass
class _Candidate:
    element: ElementHandle
    key: str
    tweet: TweetInfo | None = None
    user: UserInfo | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_TWEET_INFO_JS = """
(el) => {
  const link = el.querySelector('a[href*="/status/"]');
  let tweetId = null;
  if (link) {
    const m = (link.getAttribute('href') || '').match(/\\/status\\/(\\d+)/);
    if (m) tweetId = m[1];
  }
  const author = el.querySelector('a[href^="/"][role="link"]');
  let authorHandle = null;
  if (author) {
    const m = (author.getAttribute('href') || '').match(/^\\/([^/]+)$/);
    if (m && !['home', 'explore', 'notifications', 'messages'].includes(m[1])) authorHandle = m[1];
  }
  const text = el.querySelector('[data-testid="tweetText"]');
  let textPreview = null;
  if (text) {
    const t = text.textContent || '';
    textPreview = t.length > 50 ? t.substring(0, 50) + '…' : t;
  }
  return { tweetId, authorHandle, textPreview };
}
"""

_USER_INFO_JS = """
(el, followingSelector) => {
  const links = Array.from(el.querySelectorAll('a[href^="/"][role="link"]'));
  let handle = null;
  for (const link of links) {
    const href = link.getAttribute('href') || '';
    const m = href.match(/^\\/([^/?#]+)$/);
    if (m && !href.startsWith('/i/')) { handle = m[1]; break; }
  }
  const nameSpan =
    el.querySelector('[data-testid="UserName"] span') || el.querySelector('[dir="ltr"] span');
  let hasUnfollow = false;
  if (followingSelector) {
    hasUnfollow = Boolean(
      el.querySelector(followingSelector) ||
      el.querySelector('[data-testid$="-unfollow"], [data-testid*="unfollow"]')
    );
  }
  return {
    handle,
    displayName: nameSpan ? nameSpan.textContent : null,
    hasUnfollow,
  };
}
"""


class PageHelper:
    """Small DOM helpers shared by the deleter and the following workflow."""

    def __init__(self, page: Page, selectors: dict[str, str]) -> None:
        self.page = page
        self.selectors = selectors

    def selector(self, key: str, fallback: str | None = None) -> str:
        value = self.selectors.get(key)
        if value:
            return value
        if fallback is None:
            raise KeyError(f"selector '{key}' is not configured")
        return fallback

    async def tweets(self) -> list[ElementHandle]:
        return await self.page.query_selector_all(self.selector("tweet"))

    async def user_cells(self) -> list[ElementHandle]:
        try:
            return await self.page.query_selector_all(self.selector("userCell", DEFAULT_USER_CELL))
        except Exception as exc:
            logger.debug("Reading user cells failed: %s", exc)
            return []

    async def close_menu(self) -> None:
        try:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(150)
        except Exception:
            pass

    async def scroll_to_bottom(self) -> None:
        await self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await self.page.wait_for_timeout(1000)

    async def scroll_to_top(self) -> None:
        await self.page.evaluate("() => window.scrollTo(0, 0)")
        await self.page.wait_for_timeout(500)

    async def tweet_info(self, element: ElementHandle) -> TweetInfo:
        try:
            raw = await element.evaluate(_TWEET_INFO_JS)
            return TweetInfo(raw.get("tweetId"), raw.get("authorHandle"), raw.get("textPreview"))
        except Exception as exc:
            logger.debug("Reading tweet info failed: %s", exc)
            return TweetInfo(None, None, None)

    async def user_info(self, element: ElementHandle) -> UserInfo:
        info, _ = await self.user_info_with_unfollow(element)
        return info

    async def user_info_with_unfollow(self, element: ElementHandle) -> tuple[UserInfo, bool]:
        try:
            raw = await element.evaluate(_USER_INFO_JS, self.selector("followingButton"))
            return (
                UserInfo(raw.get("handle"), raw.get("displayName")),
                bool(raw.get("hasUnfollow")),
            )
        except Exception as exc:
            logger.debug("Reading user info failed: %s", exc)
            return UserInfo(None, None), False

    async def has_unfollow_button(self, cell: ElementHandle) -> bool:
        try:
            if await cell.query_selector(self.selector("followingButton")):
                return True
            return bool(
                await cell.query_selector('[data-testid$="-unfollow"], [data-testid*="unfollow"]')
            )
        except Exception:
            return False

    async def unlike(self, tweet: ElementHandle) -> bool:
        try:
            button = await tweet.query_selector(self.selector("unlikeButton"))
            if button is None:
                return False
            await button.wait_for_element_state("visible", timeout=3000)
            await button.click()
            await self.page.wait_for_timeout(500)
            return True
        except Exception as exc:
            logger.debug("Unlike failed: %s", exc)
            return False

    async def remove_bookmark(self, tweet: ElementHandle) -> bool:
        try:
            # The bookmarks page usually has the remove button inline in the
            # action bar; prefer that over opening the menu.
            inline = await tweet.query_selector(self.selector("removeBookmarkButton"))
            if inline is not None:
                await inline.wait_for_element_state("visible", timeout=3000)
                await inline.click()
                await self.page.wait_for_timeout(500)
                return True
            more = await tweet.query_selector(self.selector("tweetMoreButton"))
            if more is None:
                return False
            await more.wait_for_element_state("visible", timeout=3000)
            await more.click()
            await self.page.wait_for_timeout(500)
            item = await self.page.wait_for_selector(
                self.selector("removeBookmarkButton"), timeout=3000, state="visible"
            )
            if item is None:
                return False
            await item.click()
            await self.page.wait_for_timeout(500)
            return True
        except Exception as exc:
            logger.debug("Remove bookmark failed: %s", exc)
            return False

    async def unfollow(self, cell: ElementHandle) -> bool:
        """True when unfollowed *or* already not following; False = retry later."""
        try:
            button = await cell.query_selector(self.selector("followingButton"))
            if button is None:
                button = await cell.query_selector(
                    '[data-testid$="-unfollow"], [data-testid*="unfollow"]'
                )
            if button is None:
                logger.debug("User cell has no unfollow button; treating as done")
                return True
            await button.wait_for_element_state("visible", timeout=1500)
            await button.click()
            await self.page.wait_for_timeout(120)
            try:
                confirm = await self.page.wait_for_selector(
                    self.selector("unfollowConfirm"), timeout=1500, state="visible"
                )
            except Exception:
                confirm = None
            if confirm is not None:
                await confirm.click()
                await self.page.wait_for_timeout(180)
                return True
            # Some layouts unfollow on the first click; the button vanishing counts.
            return not await self.has_unfollow_button(cell)
        except Exception as exc:
            logger.debug("Unfollow failed: %s", exc)
            return False


class ContentDeleter:
    def __init__(self, page: Page, config: XConfig, env: XEnv) -> None:
        self.page = page
        self.config = config
        self.env = env
        self.helper = PageHelper(page, config.selectors)
        self.rate_limit = RateLimitDetector(page)
        self.stats = DeleteStats()
        self.username = ""
        self._session_skipped: set[str] = set()

    # ---------------------------------------------------------------- timing

    def _jitter_ms(self) -> int:
        return max(0, self.config.execution.delay_jitter_ms)

    async def _pause_between_actions(self) -> None:
        low = max(0, self.config.execution.delay_between_actions_ms)
        high = low + self._jitter_ms()
        if high > 0:
            await random_sleep_ms(low, high)

    def _rate_limit_backoff_ms(self) -> int:
        ex = self.config.execution
        return max(
            60_000,
            self.config.retry.delay_ms,
            ex.delay_between_batches_ms,
            ex.delay_between_actions_ms + self._jitter_ms(),
        )

    def _action_policy(self) -> RetryPolicy:
        rt = self.config.retry
        delay = max(0, rt.delay_ms)
        return RetryPolicy(
            max_attempts=rt.max_attempts,
            delay_ms=delay,
            exponential_backoff=rt.exponential_backoff,
            max_delay_ms=max(self._rate_limit_backoff_ms(), delay * 4),
            jitter_ratio=0.2,
        )

    def _should_refresh(self, batch_count: int) -> bool:
        every = self.config.execution.refresh_every_batches
        return every <= 1 or batch_count % every == 0

    async def _reload_or_goto(self, url: str, content: ContentType) -> None:
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            logger.warning("Reload timed out (%s); reopening the page", exc)
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self._wait_for_content(content)
        await self._ensure_session_active(f"{content} after refresh")

    async def _wait_for_content(self, content: ContentType) -> None:
        timeout = self.config.execution.page_ready_timeout_ms
        if timeout <= 0:
            return
        selector = (
            self.helper.selector("userCell", DEFAULT_USER_CELL)
            if content == "following"
            else self.helper.selector("tweet")
        )
        try:
            await self.page.wait_for_selector(selector, timeout=timeout, state="attached")
        except Exception:
            pass  # a slow page is not an error; the loop copes with an empty list

    # ---------------------------------------------------------------- guards

    async def _assert_not_rate_limited(self, context: str) -> None:
        """An empty list is only "done" if we are not being rate-limited."""
        if not self.rate_limit.hit_recently() and not await is_blank_render(self.page):
            return
        raise RateLimitError(
            f"{context}: X returned 429 and the page cannot render "
            f"({self.rate_limit.count} hit(s)). "
            "This is not an empty list; stop and wait before continuing.",
            retry_after_ms=self._rate_limit_backoff_ms(),
        )

    async def _session_blocker(self, context: str) -> str | None:
        pathname = pathname_of(self.page.url)
        if pathname.startswith(("/i/flow", "/login")):
            return f"session expired or verification required ({context})"
        if await self.page.query_selector('input[autocomplete="username"]'):
            return f"login page detected ({context})"
        if await self.page.query_selector('input[data-testid="ocfEnterTextTextInput"]'):
            return f"account verification page detected ({context})"
        return None

    async def _wait_for_session_recovery(self, context: str, reason: str) -> None:
        if self.env.resolve_headless() and self.env.headless_mode != "auto":
            raise NonRetryableError(f"{reason}; cannot recover in headless mode, log in again")
        import time

        deadline = time.monotonic() + 120
        last_notice = 0.0
        logger.warning("%s; waiting up to 2 minutes for you to restore the session", reason)
        while time.monotonic() < deadline:
            await sleep_ms(2000)
            if await self._session_blocker(context) is None:
                logger.info("Session restored, continuing (%s)", context)
                return
            if time.monotonic() - last_notice >= 30:
                last_notice = time.monotonic()
                logger.info("Still waiting for the session (%s)…", context)
        raise NonRetryableError(f"{reason}; timed out waiting for manual recovery ({context})")

    async def _ensure_session_active(self, context: str) -> None:
        raise_if_cancelled()
        blocker = await self._session_blocker(context)
        if blocker:
            await self._wait_for_session_recovery(context, blocker)
        await with_retry(
            lambda: self._raise_if_blocked(context),
            self._action_policy(),
            name=f"blocking-state check ({context})",
            retry_on=lambda e: isinstance(e, RetryableError),
        )

    def _blocking_error(self, context: str, state: BlockingState) -> Exception:
        message = f"blocked ({state.label}): {state.message} ({context})"
        if state.type == "rate_limit":
            return RateLimitError(message, state.retry_after_ms)
        if state.severity == "retryable":
            return RetryableError(message)
        return NonRetryableError(message)

    async def _raise_if_blocked(self, context: str) -> None:
        state = await blocking_state(
            self.page, self.rate_limit, rate_limit_backoff_ms=self._rate_limit_backoff_ms()
        )
        if state is not None:
            raise self._blocking_error(context, state)

    async def _run_action(self, name: str, action: Any) -> bool:
        async def attempt() -> bool:
            raise_if_cancelled()
            await self._raise_if_blocked(f"{name} pre-check")
            ok = await action()
            await self._raise_if_blocked(f"{name} post-check")
            if not ok:
                raise RetryableError(f"{name} did not complete; retrying")
            return True

        return await with_retry(
            attempt,
            self._action_policy(),
            name=name,
            retry_on=lambda e: isinstance(e, RetryableError),
        )

    @staticmethod
    def _should_abort(error: BaseException) -> bool:
        return (
            is_cancellation_error(error)
            or isinstance(error, (NonRetryableError, RateLimitError))
            or (isinstance(error, RetryableError) and "blocked (" in str(error))
        )

    # ------------------------------------------------------------ candidates

    @staticmethod
    def _tweet_key(info: TweetInfo, index: int) -> str:
        if info.tweet_id:
            return f"tweet:{info.tweet_id}"
        if info.author_handle or info.text_preview:
            return f"tweet:{info.author_handle or 'unknown'}:{info.text_preview or 'empty'}"
        return f"tweet:index:{index}"

    @staticmethod
    def _user_key(info: UserInfo, index: int) -> str:
        if info.handle:
            return f"user:{info.handle.lower()}"
        if info.display_name:
            return f"user:name:{info.display_name}"
        return f"user:index:{index}"

    async def _next_tweet(self, skipped: set[str]) -> _Candidate | None:
        for index, element in enumerate(await self.helper.tweets()):
            info = await self.helper.tweet_info(element)
            key = self._tweet_key(info, index)
            if key not in skipped:
                return _Candidate(element, key, tweet=info)
        return None

    async def _next_user(self, skipped: set[str]) -> _Candidate | None:
        for index, element in enumerate(await self.helper.user_cells()):
            info, has_unfollow = await self.helper.user_info_with_unfollow(element)
            key = self._user_key(info, index)
            if key in skipped:
                continue
            # Only cards that still say "Following"; skip the rest for good.
            if not has_unfollow:
                skipped.add(key)
                continue
            return _Candidate(element, key, user=info)
        return None

    def _log_target(self, content: ContentType, candidate: _Candidate) -> None:
        if candidate.tweet is not None:
            t = candidate.tweet
            logger.info(
                "[TARGET] %s id=%s author=%s preview=%r",
                content,
                t.tweet_id or "-",
                f"@{t.author_handle}" if t.author_handle else "-",
                t.text_preview or "",
            )
        elif candidate.user is not None:
            u = candidate.user
            logger.info(
                "[TARGET] %s handle=%s name=%r",
                content,
                f"@{u.handle}" if u.handle else "-",
                u.display_name or "",
            )

    def _audit(
        self, action: str, target: Any, *, ok: bool, error: str | None = None, **extra: Any
    ) -> None:
        audit.record(PLATFORM, action, target, ok=ok, error=error, **extra)

    # ------------------------------------------------------------- entrypoint

    async def run(self, username: str) -> DeleteStats:
        self.username = username.lstrip("@").strip().lower()
        opts = self.config.delete
        urls = self.config.urls
        logger.info(
            "Starting cleanup for @%s: %s", self.username, ", ".join(opts.enabled()) or "nothing"
        )

        if opts.tweets:
            self.stats.tweets = await self._delete_posts(
                urls.for_user("tweets", username), "tweets"
            )
        if opts.replies:
            self.stats.replies = await self._delete_posts(
                urls.for_user("tweets_with_replies", username), "replies"
            )
        if opts.retweets:
            # Undo reposts from the reposts tab: the profile timeline has no undo button.
            self.stats.retweets = await self._undo_reposts(urls.for_user("reposts", username))
        if opts.likes:
            self.stats.likes = await self._unlike(urls.for_user("likes", username))
        if opts.bookmarks:
            self.stats.bookmarks = await self._remove_bookmarks(urls.bookmarks)

        logger.info(
            "Cleanup finished: %s item(s), %s error(s)", self.stats.total(), self.stats.errors
        )
        return self.stats

    async def unfollow_all(self, username: str) -> DeleteStats:
        """Direct unfollow loop — reviewed unfollowing lives in :mod:`.following`."""
        self.username = username.lstrip("@").strip().lower()
        self.stats.following = await self._unfollow_users(
            self.config.urls.for_user("following", username)
        )
        return self.stats

    # ------------------------------------------------------ tweets / replies

    async def _delete_posts(self, url: str, content: ContentType) -> int:
        ex = self.config.execution
        deleted_total = 0
        consecutive_empty = 0
        batch_count = 0
        self._session_skipped.clear()

        await self.page.goto(url, wait_until="domcontentloaded")
        await self._wait_for_content(content)
        await self._ensure_session_active(f"{content} init")

        while deleted_total < ex.max_per_session:
            raise_if_cancelled()
            await self._ensure_session_active(f"{content} batch")
            deleted, skipped, failed = await self._delete_post_batch(content)

            if deleted == 0:
                consecutive_empty += 1
                give_up_after = 8 if skipped > 0 else 3
                logger.warning(
                    "%s: batch deleted nothing (%s/%s empty, skipped=%s failed=%s)",
                    content,
                    consecutive_empty,
                    give_up_after,
                    skipped,
                    failed,
                )
                if consecutive_empty >= give_up_after:
                    await self._assert_not_rate_limited(f"{content} listing")
                    logger.info("%s: nothing left to delete", content)
                    break
                await self.helper.scroll_to_bottom()
                continue

            consecutive_empty = 0
            deleted_total += deleted
            logger.info("%s: %s/%s deleted", content, deleted_total, ex.max_per_session)
            if deleted_total >= ex.max_per_session:
                logger.info("%s: per-session cap reached (%s)", content, ex.max_per_session)
                break

            batch_count += 1
            if self._should_refresh(batch_count):
                await self._reload_or_goto(url, content)

        return deleted_total

    async def _delete_post_batch(self, content: ContentType) -> tuple[int, int, int]:
        ex = self.config.execution
        deleted = skipped = failed = 0
        try:
            await self.helper.scroll_to_top()
            first = await self._next_tweet(self._session_skipped)
            if first is None:
                return 0, 0, 0
            self._session_skipped.add(first.key)

            for i in range(ex.per_batch):
                try:
                    raise_if_cancelled()
                    candidate = first if i == 0 else await self._next_tweet(self._session_skipped)
                    if candidate is None:
                        break
                    self._session_skipped.add(candidate.key)
                    self._log_target(content, candidate)
                    outcome = await self._delete_single_post(candidate, content)
                    if outcome == "deleted":
                        deleted += 1
                        await self._pause_between_actions()
                    elif outcome == "skipped":
                        skipped += 1
                        await self.helper.close_menu()
                    else:
                        failed += 1
                        self.stats.errors += 1
                        await self.helper.close_menu()
                except Exception as exc:
                    if self._should_abort(exc):
                        raise
                    failed += 1
                    self.stats.errors += 1
                    logger.warning("%s: delete failed, skipping: %s", content, exc)
                    await self.helper.close_menu()

            await sleep_ms(ex.delay_between_batches_ms)
        except Exception as exc:
            if self._should_abort(exc):
                raise
            logger.error("%s: batch failed: %s", content, exc)
            self.stats.errors += 1
        return deleted, skipped, failed

    def _is_own(self, info: TweetInfo) -> bool:
        if not self.username or not info.author_handle:
            return True
        return info.author_handle.lstrip("@").strip().lower() == self.username

    async def _delete_single_post(
        self, candidate: _Candidate, content: ContentType
    ) -> ActionOutcome:
        assert candidate.tweet is not None
        info = candidate.tweet
        if not self._is_own(info):
            logger.info("%s: skipping post by @%s", content, info.author_handle)
            return "skipped"

        await self._raise_if_blocked(f"delete {content} pre-check")

        tweet_id = info.tweet_id
        tweet_sel = self.helper.selector("tweet")
        card = (
            self.page.locator(tweet_sel)
            .filter(has=self.page.locator(f'a[href*="/status/{tweet_id}"]'))
            .first
            if tweet_id
            else self.page.locator(tweet_sel).first
        )
        try:
            await card.scroll_into_view_if_needed()
            await card.locator(self.helper.selector("tweetMoreButton")).first.click(timeout=4000)
        except Exception as exc:
            logger.warning("%s: no menu button on %s (%s)", content, tweet_id or "post", exc)
            self._audit(f"{content}.delete", tweet_id, ok=False, error="menu button not found")
            return "failed"

        item = self.page.get_by_role("menuitem", name=DELETE_MENU_ITEM)
        if await item.count() == 0:
            logger.info("%s: no delete item in menu for %s; skipping", content, tweet_id or "post")
            await self.helper.close_menu()
            return "skipped"

        await item.first.click()
        try:
            await self.page.locator(self.helper.selector("confirmDeleteButton")).first.click(
                timeout=4000
            )
        except Exception as exc:
            logger.warning(
                "%s: confirmation did not appear for %s (%s)", content, tweet_id or "post", exc
            )
            await self.helper.close_menu()
            self._audit(f"{content}.delete", tweet_id, ok=False, error="confirmation not shown")
            return "failed"

        if tweet_id:
            try:
                await self.page.wait_for_function(
                    'id => !document.querySelector(`a[href*="/status/${id}"]`)',
                    arg=tweet_id,
                    timeout=5000,
                )
            except Exception:
                logger.warning("%s: post %s still in the DOM after confirming", content, tweet_id)
                self._audit(
                    f"{content}.delete", tweet_id, ok=False, error="still present after confirm"
                )
                return "failed"

        self._audit(f"{content}.delete", tweet_id, ok=True, preview=info.text_preview)
        logger.info("%s: deleted %s", content, tweet_id or info.text_preview or "")
        return "deleted"

    # --------------------------------------------------------------- reposts

    async def _undo_reposts(self, url: str) -> int:
        ex = self.config.execution
        total = 0
        consecutive_empty = 0
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._wait_for_content("retweets")
        await self._ensure_session_active("reposts init")

        while total < ex.max_per_session:
            raise_if_cancelled()
            await self._ensure_session_active("reposts batch")
            card = (
                self.page.locator(self.helper.selector("tweet"))
                .filter(has=self.page.locator(self.helper.selector("unretweet")))
                .first
            )
            if await card.count() == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    await self._assert_not_rate_limited("reposts listing")
                    logger.info("reposts: nothing left")
                    break
                await self.helper.scroll_to_bottom()
                continue
            href = None
            try:
                await card.scroll_into_view_if_needed()
                href = await card.locator('a[href*="/status/"]').first.get_attribute("href")
                logger.info("[TARGET] retweets href=%s", href or "-")
                await card.locator(self.helper.selector("unretweet")).first.click(timeout=4000)
                await self.page.locator(self.helper.selector("unretweetConfirm")).first.click(
                    timeout=4000
                )
                await self.page.wait_for_timeout(800)
                consecutive_empty = 0
                total += 1
                self._audit("retweets.undo", href, ok=True)
                logger.info("reposts: %s/%s undone", total, ex.max_per_session)
                await self._pause_between_actions()
            except Exception as exc:
                if self._should_abort(exc):
                    raise
                self.stats.errors += 1
                consecutive_empty += 1
                self._audit("retweets.undo", href, ok=False, error=str(exc))
                logger.warning("reposts: undo failed, skipping: %s", exc)
                await self.helper.close_menu()
                if consecutive_empty >= 3:
                    break
        return total

    # ------------------------------------------------- likes and bookmarks

    async def _per_tweet_loop(
        self, url: str, content: ContentType, action_name: str, action: Any, audit_action: str
    ) -> int:
        """Shared loop for likes and bookmarks: act on visible tweets, refresh, repeat."""
        ex = self.config.execution
        total = 0
        consecutive_empty = 0
        batch_count = 0
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._wait_for_content(content)
        await self._ensure_session_active(f"{content} init")
        logger.info("%s: starting", content)

        while total < ex.max_per_session:
            raise_if_cancelled()
            await self._ensure_session_active(f"{content} batch")
            await self.helper.scroll_to_top()

            attempted: set[str] = set()
            first = await self._next_tweet(attempted)
            if first is None:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    await self._assert_not_rate_limited(f"{content} listing")
                    logger.info("%s: nothing left", content)
                    break
                await self.helper.scroll_to_bottom()
                continue
            attempted.add(first.key)
            done_in_batch = 0

            for i in range(ex.per_batch):
                try:
                    raise_if_cancelled()
                    candidate = first if i == 0 else await self._next_tweet(attempted)
                    if candidate is None:
                        break
                    attempted.add(candidate.key)
                    self._log_target(content, candidate)
                    target = candidate.tweet.tweet_id if candidate.tweet else None
                    ok = await self._run_action(action_name, lambda c=candidate: action(c.element))
                    if ok:
                        done_in_batch += 1
                        self._audit(audit_action, target, ok=True)
                        await self._pause_between_actions()
                    else:
                        self.stats.errors += 1
                        self._audit(audit_action, target, ok=False, error="action returned false")
                except Exception as exc:
                    if self._should_abort(exc):
                        raise
                    self.stats.errors += 1
                    logger.warning("%s: %s failed, skipping: %s", content, action_name, exc)

            if done_in_batch == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    logger.info("%s: several rounds without progress, assuming done", content)
                    break
            else:
                consecutive_empty = 0
                total += done_in_batch
                logger.info("%s: %s/%s", content, total, ex.max_per_session)

            if total >= ex.max_per_session:
                logger.info("%s: per-session cap reached (%s)", content, ex.max_per_session)
                break

            batch_count += 1
            if self._should_refresh(batch_count):
                await self._reload_or_goto(url, content)
        return total

    async def _unlike(self, url: str) -> int:
        return await self._per_tweet_loop(
            url, "likes", "unlike", self.helper.unlike, "likes.unlike"
        )

    async def _remove_bookmarks(self, url: str) -> int:
        return await self._per_tweet_loop(
            url, "bookmarks", "remove bookmark", self.helper.remove_bookmark, "bookmarks.remove"
        )

    # -------------------------------------------------------------- following

    async def _open_following_list(self, url: str, reason: str) -> int:
        logger.info("Opening following list (%s): %s", reason, url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await sleep_ms(1500)
        wait_ms = max(self.config.execution.page_ready_timeout_ms, 8000)
        try:
            await self.page.wait_for_selector(
                self.helper.selector("userCell", DEFAULT_USER_CELL),
                timeout=wait_ms,
                state="visible",
            )
        except Exception:
            logger.warning(
                "Following list did not render in %sms; scrolling to trigger it", wait_ms
            )
        await self._ensure_session_active(f"following {reason}")
        for _ in range(4):
            await self.helper.scroll_to_bottom()
            await sleep_ms(700)
        await self.helper.scroll_to_top()
        await sleep_ms(500)
        visible = len(await self.helper.user_cells())
        logger.info("Following list: url=%s visible=%s", self.page.url, visible)
        return visible

    async def _unfollow_users(self, url: str) -> int:
        ex = self.config.execution
        total = 0
        consecutive_empty = 0
        processed: set[str] = set()
        batch_count = 0

        if await self._open_following_list(url, "init") == 0:
            await sleep_ms(2000)
            await self._open_following_list(url, "retry on empty list")
        logger.info("following: starting")

        while total < ex.max_per_session:
            raise_if_cancelled()
            await self._ensure_session_active("following batch")
            await self.helper.scroll_to_top()
            await sleep_ms(300)

            first = await self._next_user(processed)
            if first is None:
                consecutive_empty += 1
                cells = len(await self.helper.user_cells())
                logger.warning(
                    "following: no unfollowable card (%s/8, cells=%s)", consecutive_empty, cells
                )
                if consecutive_empty >= 8:
                    # An empty list is either really empty or a 429 blank page.
                    # Treating the latter as "done" would skip hundreds of accounts.
                    await self._assert_not_rate_limited("following listing")
                    logger.info("following: nothing left")
                    break
                for _ in range(6):
                    await self.helper.scroll_to_bottom()
                    await sleep_ms(500)
                if consecutive_empty >= 2:
                    await self._open_following_list(url, f"recover #{consecutive_empty}")
                continue

            processed.add(first.key)
            done_in_batch = 0
            for i in range(ex.per_batch):
                try:
                    raise_if_cancelled()
                    candidate = first if i == 0 else await self._next_user(processed)
                    if candidate is None:
                        break
                    processed.add(candidate.key)
                    self._log_target("following", candidate)
                    handle = candidate.user.handle if candidate.user else None
                    # No heavy retry for unfollow: skip failures and let the
                    # next refresh move the list forward.
                    ok = await self.helper.unfollow(candidate.element)
                    if ok:
                        done_in_batch += 1
                        self._audit("following.unfollow", handle, ok=True)
                        await self._pause_between_actions()
                    else:
                        self.stats.errors += 1
                        self._audit("following.unfollow", handle, ok=False, error="click failed")
                except Exception as exc:
                    if self._should_abort(exc):
                        raise
                    self.stats.errors += 1
                    logger.warning("following: unfollow failed, skipping: %s", exc)

            if done_in_batch == 0:
                consecutive_empty += 1
                if consecutive_empty >= 8:
                    logger.info("following: several rounds without progress, assuming done")
                    break
            else:
                consecutive_empty = 0
                total += done_in_batch
                logger.info("following: %s/%s unfollowed", total, ex.max_per_session)

            if total >= ex.max_per_session:
                logger.info("following: per-session cap reached (%s)", ex.max_per_session)
                break

            batch_count += 1
            if self._should_refresh(batch_count):
                await self._open_following_list(url, f"refresh #{batch_count}")
            else:
                await self.helper.scroll_to_bottom()
                await sleep_ms(ex.delay_between_batches_ms)
                await self.helper.scroll_to_top()
        return total
