# AGENTS.md — driving social-cleaner from an AI agent

Read this first; drill into `docs/` for platform detail.

Design assumption: **the agent does the reasoning (filters, priorities,
dry runs); the tool exposes reliable, cheap building blocks.** Ask for
"list everything with detail", filter locally, then call the selective
delete — never ask the tool to judge quality.

## Entry points

| Surface | Start | Best for |
|---|---|---|
| CLI | `social-cleaner --help` | shell-capable agents; JSON on stdout, logs on stderr |
| Python SDK | `social_cleaner.platforms.{bilibili,x}` | embedding in scripts or agent workflows |

Both drive the same service layer.

## Bilibili

- Auth: `social-cleaner bilibili login` (QR) once, or `BILI_SESSDATA` /
  `BILI_JCT` environment variables. `code == -101` ⇒ session expired.
- Rate: 1.5 req/s per process, retried with backoff on `-352` / `-799` /
  `-509` / 412 / 429. Do not parallelise processes to go faster.
- No batch unfollow upstream; no API for own comments; all deletes final.
- Pattern:
  1. `social-cleaner bilibili me` → `mid`
  2. `social-cleaner bilibili followings list --with-detail` page by page
  3. filter locally (follower count, last upload date, keywords in `sign`)
  4. safety net: `social-cleaner bilibili tag add-users … --tag-name to-review`, let the human audit in the app
  5. `social-cleaner bilibili followings unfollow <mids>`
- `… clear --yes` and `clean-all --yes` report `stopped_reason` if a partial
  clean stops early.

## X

- Needs a browser on the machine (`playwright install chromium`) and a human
  for the first `social-cleaner x login`.
- Content: `x clean --tweets --replies --retweets --likes --bookmarks --max N --yes`.
  Only the logged-in user's own posts are deleted.
- Unfollowing is a reviewed pipeline; the agent may run `export` and
  `classify` and may *propose* `approved-unfollow.jsonl`, but a human should
  confirm it before `execute` (visible browser, slow, resumable).
- Exit code 75 means X rate-limited the account: wait 30–60 minutes, lower
  the rate, do not retry immediately and do not re-login.
- Run summaries: `~/.social-cleaner/x/runs/run-summary-<id>.json`.

## Shared

- Audit log: `~/.social-cleaner/audit.jsonl`, one JSON per destructive
  action with `platform`, `action`, `target`, `ok`. Use it to report what
  was actually removed.
- Cooperative cancellation: SIGINT / SIGTERM finishes the current step and
  records `stopped_reason: "cancelled"`.
- Everything is permanent. Show the user a dry-run or a list before any
  write touching more than a handful of items.

## Embedding in Python

```python
from social_cleaner.platforms.bilibili.runtime import build_client
from social_cleaner.platforms.bilibili.services import FollowingService

async with build_client((sessdata, bili_jct)) as client:
    async for item in FollowingService(client).iter_all(mid):
        ...
```

All Bilibili services take `on_item` / `on_batch` progress callbacks. X
entry points: `platforms.x.runner.run_with_browser`, `platforms.x.deleter.ContentDeleter`,
`platforms.x.following.{FollowingCollector, classify_file, FollowingExecutor}`.
