"""CSS selectors for the x.com web UI.

X ships no stable API for this, so everything hangs on DOM selectors. They are
data, not code: the defaults below can be overridden per key with a JSON file
(``X_SELECTORS_FILE`` or ``--selectors``) when X changes its front end.

Prefer ``data-testid`` — it is independent of the UI language. ``aria-label``
and text selectors change with the language, so where they are needed both
Chinese and English variants are listed.

``primary`` and ``fallback`` are joined into one comma-separated selector.
Such lists match in DOM order, not in the order written, so a fallback must
never match a button with a different meaning (e.g. ``[aria-label*='Repost']``
as a fallback for *undo* repost would hit the repost button itself).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from social_cleaner.core.errors import ConfigError


@dataclass(frozen=True)
class SelectorSpec:
    primary: str
    fallback: str | None = None
    description: str = ""

    def combined(self) -> str:
        return f"{self.primary}, {self.fallback}" if self.fallback else self.primary


DEFAULT_SELECTORS: dict[str, SelectorSpec] = {
    "tweet": SelectorSpec(
        "[data-testid='tweet']",
        "article[data-testid='tweet'], article[role='article']",
        "Post container",
    ),
    "tweetMoreButton": SelectorSpec(
        "[data-testid='caret']",
        "[aria-label*='更多'][role='button'], [aria-label='More'][role='button']",
        "The '…' menu button on a post",
    ),
    "deleteButton": SelectorSpec(
        "[role='menuitem']",
        "[data-testid='Dropdown'] [role='menuitem']",
        "Dropdown items; the delete item has no stable testid so code matches its text",
    ),
    "confirmDeleteButton": SelectorSpec(
        "[data-testid='confirmationSheetConfirm']",
        "[data-testid='confirmationSheetDialog'] [role='button'][data-testid*='onfirm']",
        "Confirm button in the delete dialog",
    ),
    "unretweet": SelectorSpec(
        "[data-testid='unretweet']",
        "[aria-label*='撤销转推'], [aria-label*='Undo repost'], [aria-label*='Undo Repost']",
        "Undo repost button. Never add [aria-label*='Repost'] here",
    ),
    "unretweetConfirm": SelectorSpec(
        "[data-testid='unretweetConfirm']",
        "[data-testid='confirmationSheetConfirm']",
        "Confirm undo repost",
    ),
    "likeButton": SelectorSpec(
        "[data-testid='like']", "[aria-label*='喜欢'], [aria-label*='Like']", "Like (not yet liked)"
    ),
    "unlikeButton": SelectorSpec(
        "[data-testid='unlike']",
        "[aria-label*='取消喜欢'], [aria-label*='Unlike']",
        "Unlike (already liked)",
    ),
    "bookmarkButton": SelectorSpec(
        "[data-testid='bookmark']", "[aria-label*='书签'], [aria-label*='Bookmark']", "Add bookmark"
    ),
    "removeBookmarkButton": SelectorSpec(
        "[data-testid='removeBookmark']",
        "[role='menuitem']:has-text('移除书签'), "
        "[role='menuitem']:has-text('Remove from Bookmarks'), "
        "[aria-label*='移除书签'], [aria-label*='Remove from Bookmarks']",
        "Remove bookmark, inline in the action bar or inside the '…' menu",
    ),
    "followingButton": SelectorSpec(
        "[data-testid$='-unfollow']",
        "[data-testid*='unfollow'], [aria-label^='正在关注'], [aria-label*='Following @']",
        "'Following' button in a user cell. X uses <userId>-unfollow, hence the suffix match",
    ),
    "unfollowConfirm": SelectorSpec(
        "[data-testid='confirmationSheetConfirm']",
        "[role='button']:has-text('取消关注'), [role='button']:has-text('Unfollow')",
        "Confirm unfollow",
    ),
    "userCell": SelectorSpec(
        "[data-testid='UserCell']",
        "[data-testid='cellInnerDiv'] [data-testid='UserCell'], [data-testid*='user-cell']",
        "User card in the following list",
    ),
    "profileLink": SelectorSpec(
        "a[data-testid='AppTabBar_Profile_Link']",
        "[data-testid='SideNav_AccountSwitcher_Button']",
        "Profile link in the nav bar; also a logged-in signal",
    ),
}

REQUIRED_KEYS: frozenset[str] = frozenset(DEFAULT_SELECTORS)


def _parse_spec(key: str, raw: Any, *, where: str) -> SelectorSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: selector '{key}' must be an object with 'primary'")
    primary = raw.get("primary")
    if not isinstance(primary, str) or not primary.strip():
        raise ConfigError(f"{where}: selector '{key}' needs a non-empty 'primary'")
    fallback = raw.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or not fallback.strip()):
        raise ConfigError(f"{where}: selector '{key}' fallback must be a non-empty string")
    description = raw.get("description")
    return SelectorSpec(
        primary.strip(),
        fallback.strip() if isinstance(fallback, str) else None,
        description if isinstance(description, str) else "",
    )


def load_selector_specs(path: Path | None = None) -> dict[str, SelectorSpec]:
    """Defaults, overridden per key by ``path`` when given.

    The file shape is ``{"selectors": {key: {primary, fallback?, description?}}}``
    with an optional ``customSelectors`` block for extra keys; ``version`` and
    ``lastUpdated`` are recorded in the run summary when present.
    """
    specs = dict(DEFAULT_SELECTORS)
    if path is None:
        return specs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"selectors file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"selectors file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"selectors file {path} must contain a JSON object")
    for block in ("selectors", "customSelectors"):
        raw_block = data.get(block)
        if raw_block is None:
            continue
        if not isinstance(raw_block, dict):
            raise ConfigError(f"selectors file {path}: '{block}' must be an object")
        for key, raw in raw_block.items():
            specs[key] = _parse_spec(key, raw, where=str(path))
    return specs


def load_selectors(path: Path | None = None) -> dict[str, str]:
    """Flattened ``{key: combined-selector}`` ready for Playwright."""
    return {key: spec.combined() for key, spec in load_selector_specs(path).items()}


def selectors_metadata(path: Path | None) -> dict[str, Any]:
    """What the run summary records about where selectors came from."""
    if path is None:
        return {"source": "builtin"}
    meta: dict[str, Any] = {"source": "file", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in ("version", "lastUpdated"):
                if isinstance(data.get(key), str):
                    meta[key] = data[key]
    except (OSError, ValueError) as exc:
        meta["error"] = str(exc)
    return meta
