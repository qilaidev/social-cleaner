"""X (formerly Twitter) support.

X exposes no affordable API for account cleanup, so this platform drives the
logged-in x.com web UI with Playwright on the user's own machine. Everything
here is built around one fact learned the hard way: **when X rate-limits an
account it returns HTTP 429 on its internal APIs and the page renders blank**.
That looks exactly like "logged out" or "nothing left to delete", and both
misreadings make things worse (re-login deepens the limit; "nothing left"
silently skips unfinished work). :mod:`.signals` watches the network for the
429 and every loop consults it before concluding anything.

Layers:

- :mod:`.config` / :mod:`.selectors` — typed config with safe defaults;
  CSS selectors are data so X front-end changes need no code change.
- :mod:`.browser` / :mod:`.login` — persistent Chromium profile, manual or
  credential login, headless/headful switching.
- :mod:`.deleter` — tweets, replies, reposts, likes, bookmarks, direct unfollow.
- :mod:`.following` — export → classify → dry-run → execute → resume
  workflow for reviewed unfollowing.
- :mod:`.runner` — run lock, run summary, signal handling.
- :mod:`.cli` — ``social-cleaner x …``.
"""

PLATFORM = "x"
