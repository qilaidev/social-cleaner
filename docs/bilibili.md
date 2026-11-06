# Bilibili

Talks to Bilibili's official web API over HTTPS with your own session
cookies. No browser, no third-party server.

## Login

```bash
social-cleaner bilibili login        # prints a QR code; scan it in the Bilibili app
social-cleaner bilibili me           # verifies the session, prints mid / uname
social-cleaner bilibili logout
```

Credentials are stored at `~/.social-cleaner/bilibili/credentials.json`
(0600). Alternatives, in priority order: `BILI_SESSDATA` + `BILI_JCT`
environment variables (optionally `BILI_MID`, `BILI_UNAME`), or
`BILI_CREDENTIALS_PATH` pointing at a JSON file with `sessdata` / `bili_jct`.

`code == -101` from any command means the session expired: log in again.

## Commands

All commands print JSON to stdout (add `--pretty` for a Python repr) and log
to stderr. `--mid` defaults to the logged-in account.

| Area | Command | Notes |
|---|---|---|
| Identity | `me` | first call in any script; gives you `mid` |
| Users | `users info <mid>` · `users stat <mid>` · `users videos <mid> [--page-size 1]` | WBI-signed; `videos --page-size 1` is the cheap "last upload" probe |
| Followings | `followings list [--with-detail] [--page-size 50]` · `followings all [--with-detail]` · `followings detail <mid>` | `--with-detail` adds profile + stat + latest video per item (extra requests, rate-limited) |
| | `followings unfollow <mid>…` · `followings clear --yes` | sequential; Bilibili has no batch unfollow |
| Favorites | `favorites folders` · `favorites items <media_id>` · `favorites delete <media_id> <aid>…` · `favorites clear --yes` | delete takes video aids (type 2) |
| Dynamics | `dynamics list [--offset …]` · `dynamics delete <id>…` · `dynamics clear --yes` | cursor pagination via `offset` |
| History | `history list [--max-id … --view-at …]` · `history delete archive_<aid>` · `history clear --yes` | `clear` is one upstream call |
| Groups | `tag list` · `tag create <name>` · `tag delete <tagid>` · `tag add-users <mid>… --tag-name to-review` · `tag list-users <tagid>` | following groups ("关注分组") as a review safety net |
| Full clean | `clean-all --yes` | clears followings, favorites, dynamics and watch history in sequence |

## Recommended workflow: quality-based unfollow

```bash
mid=$(social-cleaner bilibili me | jq .mid)
social-cleaner bilibili followings all --with-detail > followings.json
jq '[.[] | select(.detail.stat.follower < 500 and (.detail.latest_video.created // 0) < (now - 180*86400)) | .mid]' followings.json > candidates.json
# put them in a group, audit in the app, then:
social-cleaner bilibili tag add-users $(jq -r '.[]' candidates.json) --tag-name to-review
social-cleaner bilibili followings unfollow $(jq -r '.[]' candidates.json)
```

The tool exposes building blocks reliably; filtering is yours (or your
agent's) to do locally.

## Rate limiting and risk control

- One token bucket per process, default **1.5 requests/s** (`BILI_API_QPS`).
  All clients in the same event loop share it. Do not scale out to go faster
  — you will trip risk control.
- Responses `-352`, `-799`, `-509` and HTTP 412 / 429 are retried with full
  jitter exponential backoff: `BILI_MAX_RETRIES` (3) retries, base
  `BILI_RETRY_BASE_DELAY` (1 s), capped at 30 s.
- WBI signing keys are cached process-wide for an hour and refreshed once on
  `-101` / `-111` / `-403`.
- `clear` commands stop with `stopped_reason: no_progress` when a whole page
  fails (expired session, risk control) instead of grinding through the rest,
  and with `page_limit` after 200 pages.

## Audit

Every unfollow / delete / clear appends a line to
`~/.social-cleaner/audit.jsonl`:

```json
{"ts": 1757400000.1, "platform": "bilibili", "action": "following.unfollow", "target": 12345, "ok": true}
```

## Limits

- No batch unfollow upstream; expect ~1 unfollow per second at the default rate.
- No API to list your own comments; they cannot be cleaned.
- All deletes are permanent.
