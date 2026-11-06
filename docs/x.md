# X (Twitter)

X has no affordable API for this, so the tool drives the logged-in x.com web
UI with Playwright in a persistent browser profile on your machine.

## Setup

```bash
pip install social-cleaner
playwright install chromium          # once
social-cleaner x login               # opens a visible browser; log in, close nothing
social-cleaner x whoami
```

The profile (cookies, storage) lives in `~/.social-cleaner/x/browser-data/`
(`X_USER_DATA_DIR`), plus a `state.json` cookie snapshot re-imported on every
launch. Optional `X_USERNAME` / `X_PASSWORD` fill the login form for you; any
verification step hands over to you in the visible browser.

### Headless policy — `X_HEADLESS`

| value | behaviour |
|---|---|
| `false` (default) | visible browser for everything |
| `auto` | headless when a saved session exists; if it is invalid, relaunch visible for login, then continue headless |
| `true` | headless always; fails if a login is needed |

`--headless` / `--headful` override per command. `followings execute` and
`resume` always require a visible browser.

## Cleaning content

```bash
social-cleaner x clean                                 # defaults: tweets + replies + reposts, 5 each
social-cleaner x clean --likes --bookmarks --max 100 --batch 5 --yes
social-cleaner x clean --no-tweets --no-replies --retweets --max 50
```

`clean` opens each listing page, acts on the first few visible items, refreshes,
and repeats until the cap or several empty rounds. It only deletes posts
whose author is the logged-in handle. Before starting it prints the plan and
counts down `X_START_COUNTDOWN_SECONDS` (10) unless `--yes`.

Unfollowing is not part of `clean`. Use the reviewed flow below or, if you
accept the risk, `x followings clear --max N --yes`.

## Reviewed unfollowing

```bash
social-cleaner x followings export
#  → ~/.social-cleaner/x/followings/<run_id>/followings.jsonl + .csv
social-cleaner x followings classify --input ~/.social-cleaner/x/followings/<run_id>/followings.jsonl
#  → candidates.jsonl · keep-list.jsonl · review.csv · approved-unfollow.jsonl (empty)
```

Open `review.csv`, decide, and copy the lines you want gone from
`candidates.jsonl` into `approved-unfollow.jsonl` (one JSON object per line,
only `handle` is required). Then:

```bash
social-cleaner x followings dry-run --input …/approved-unfollow.jsonl     # no browser
social-cleaner x followings execute --confirm-file …/approved-unfollow.jsonl
social-cleaner x followings resume --run-id <run_id>                     # after Ctrl+C or a stop
```

`execute` writes `session.json` after every handle. It stops on its own when:

- `max_unfollow_per_session` (10) is reached → run it again later;
- `max_consecutive_failures` (3) in a row;
- a risk signal appears (login wall, "unusual activity", configured patterns).

Classification rules (config `following.rules`): `keep_handles` and
`keep_keywords` always win; `drop_handles`, `drop_keywords` and
`low_info_candidate` (no bio, not verified, does not follow you) mark
candidates. Everything else is kept. Rules only produce a proposal — nothing
is unfollowed until you write the approval file.

## Configuration

Machine-level knobs are `X_*` environment variables (see `.env.example`).
Behaviour is a JSON file passed with `--config` or `X_CONFIG_FILE`; any
subset of keys overrides the defaults. Full example:
[`x-config.example.json`](x-config.example.json).

| Key | Default | Meaning |
|---|---|---|
| `execution.max_per_session` | 5 | cap per content type per run |
| `execution.per_batch` | 3 | actions between pauses |
| `execution.delay_between_actions_ms` / `delay_jitter_ms` | 1500 / 500 | pause after each action, plus random jitter |
| `execution.delay_between_batches_ms` | 2500 | pause after each batch |
| `execution.refresh_every_batches` | 2 | reload the page every N batches |
| `retry.max_attempts` / `delay_ms` | 3 / 5000 | per-action retry with exponential backoff |
| `following.execution.*` | slow | delays, per-run cap, cooldown for reviewed unfollow |
| `following.safety.require_headful_for_execute` | true | refuse headless execute |

Speeds that held up in practice: ~12 unfollows per minute long-term. Faster
gets 429s.

### Selectors

x.com changes its markup; selectors are data. Override any key with a JSON
file (`--selectors` / `X_SELECTORS_FILE`), format in
[`x-selectors.example.json`](x-selectors.example.json). Prefer
`data-testid`; `primary` and `fallback` are joined into one comma-separated
list that matches in DOM order, so a fallback must never match a button with
a different meaning.

## When X pushes back

A rate-limited account gets HTTP 429 on x.com's internal API and the page
renders **blank**. That is indistinguishable, in the DOM, from "logged out"
and from "nothing left to delete" — and both misreadings make it worse. The
tool watches the network for the 429 and:

- refuses to conclude "list is empty" while a 429 was seen recently;
- refuses to re-login (re-login deepens the limit);
- exits with code **75** so a wrapper script can back off for a long time.

| exit code | meaning |
|---|---|
| 0 | completed |
| 1 | failed (see the run summary) |
| 2 | configuration error, or another run holds the profile lock |
| 75 | rate-limited by X; wait 30–60 min, then lower the action rate |
| 130 | cancelled (Ctrl+C / SIGTERM) |

Every run writes `~/.social-cleaner/x/runs/run-summary-<id>.json` with the
effective config, environment, stats and error. Deletions are also in the
shared audit log.

## Guard rails

- one run per profile (`run.lock` with heartbeat; stale locks are reclaimed);
- `X_TARGET_USERNAME=handle` aborts if the browser is logged in as anyone else;
- `resume` aborts if `session.json` was written for another handle;
- deleting only touches posts authored by the logged-in handle.
