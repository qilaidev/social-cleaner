# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-09

First release of the unified project. It consolidates the standalone
`bilibili-cleaner` (v1.4.0) and `x-account-cleaner` (v1.0.0) repositories into
one Python package with one CLI and one shared audit log.

### Added

- `social-cleaner` root CLI with `bilibili` and `x` sub-commands, including
  `social-cleaner bilibili clean-all` for full-account CLI cleanup.
- Shared core: `SOCIAL_CLEANER_*` settings with a single data directory
  (`~/.social-cleaner`), logging, append-only `0600` audit log (`audit.jsonl`)
  flushed on every write and tagged with `platform`, cooperative cancellation
  across both Bilibili and X loops/sleeps/token buckets, and generic retry
  with jittered backoff.
- X platform ported from TypeScript/Playwright to Python/Playwright:
  persistent profile with atomic `0600` cookie snapshot, manual / credential
  login with verification hand-over, headless policy (`false` / `auto` / `true`),
  network-level 429 detection and blank-render protection across both content
  cleanup and `followings export` / `execute`, risk-text detection, atomic
  run lock with heartbeat, atomic run summaries, exit code 75 on rate limit,
  content cleanup (posts, replies, reposts, likes, bookmarks), direct
  unfollow, and the reviewed unfollow pipeline (export → classify → dry-run →
  execute → resume) with single-IPC batch DOM lookup and CSV formula-injection
  neutralization. Deletions and unfollows are written to the audit log.
- Parallel Bilibili `FollowingService.get_detail` enrichment (`info` + `stat` +
  `latest_video` gathered concurrently under the shared token-bucket rate
  limiter), persistent HTTP keep-alive connection limits, and process-wide
  locked WBI key caching.
- X configuration as a typed, validated JSON overlay on conservative defaults;
  selectors as an overridable data file with built-in defaults.
- `docs/adding-a-platform.md` describing the contract for new platforms.
- CI on Python 3.10–3.12 with ruff and pytest.

### Changed

- Bilibili moved under `social_cleaner.platforms.bilibili`; CLI commands live
  under `social-cleaner bilibili …`. Credentials default to
  `~/.social-cleaner/bilibili/credentials.json` (written atomically with `0600`
  permissions).
- Shared settings renamed from `BILI_LOG_LEVEL` / `BILI_AUDIT_*` to the
  `SOCIAL_CLEANER_` prefix. `BILI_API_QPS`, `BILI_HTTP_TIMEOUT`,
  `BILI_MAX_RETRIES`, `BILI_RETRY_BASE_DELAY`, `BILI_CREDENTIALS_PATH`,
  `BILI_SESSDATA`, `BILI_JCT` are unchanged.
- The Bilibili CLI applies the configured timeout and retry policy everywhere
  via `runtime.build_client`.
- X environment variables are prefixed `X_` (`X_HEADLESS`, `X_USER_DATA_DIR`,
  `X_USERNAME`, `X_TARGET_USERNAME`, …); the browser profile defaults to
  `~/.social-cleaner/x/browser-data` instead of `./browser-data`.
- X `clean` no longer takes a `following` option; direct unfollow is
  `x followings clear --max N --yes`.

### Removed

- The Bilibili web UI (`frontend/`) and the Bilibili-only HTTP server /
  in-memory task queue (`server/`, `platforms/bilibili/http/`, `openapi.json`,
  `Dockerfile`, `docker-compose.yml`). Both platforms now share a symmetrical,
  local-first CLI and Python SDK surface.
- `x-account-cleaner`'s `full-clean.sh` / `watchdog.sh` shell wrappers,
  `config.json` in the working directory, and the `ALLOW_LEGACY_FOLLOWING_DELETE`
  switch (replaced by the explicit `followings clear` command).
