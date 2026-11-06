# social-cleaner

**One CLI to inspect and clean your own social-media accounts.** Bilibili
today via the official web API, X (Twitter) via browser automation of x.com,
more platforms behind the same core. Local-first: your credentials and your
data never leave your machine. Every destructive action lands in an
append-only audit log, because no platform offers an undo.

**一个命令行工具，清理你自己的社交账号。** 当前支持 B 站（官方 Web API）和
X / Twitter（Playwright 驱动 x.com 网页），后续平台复用同一套核心。本地运行，
凭证与数据不出本机；所有删除动作都写入只追加的审计日志——因为没有任何平台提供撤销。

[![CI](https://github.com/qilaidev/social-cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/qilaidev/social-cleaner/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[Bilibili guide](docs/bilibili.md) · [X guide](docs/x.md) ·
[Adding a platform](docs/adding-a-platform.md) · [AGENTS.md](AGENTS.md) · [Changelog](CHANGELOG.md)

## What it does

| Platform | Transport | Listing / review | Cleanup |
|---|---|---|---|
| **Bilibili** | HTTPS to the official web API with your `SESSDATA` / `bili_jct` | followings (with concurrent profile / stats / last upload enrichment), favorite folders and items, dynamics, watch history, following groups | selective unfollow, delete favorites, delete dynamics, delete / clear history, clear everything (`clean-all`) |
| **X** | Playwright driving x.com in a persistent browser profile | export following list to JSONL / CSV, rule-based candidate classification, dry-run | delete posts and replies, undo reposts, unlike, remove bookmarks, reviewed unfollow with resume, direct unfollow |

Shared by every platform: process-wide rate limiting, retry with backoff on
risk-control responses, cooperative cancellation (Ctrl+C finishes the
current step and writes a summary), and an append-only audit trail.

## Install

```bash
pip install social-cleaner                 # CLI + Python SDK, both platforms
playwright install chromium                # only for X; downloads a browser once
```

From source:

```bash
git clone https://github.com/qilaidev/social-cleaner.git
cd social-cleaner
pip install -e '.[dev]'
playwright install chromium
```

Python 3.10+. Everything the tool persists goes under `~/.social-cleaner`
(`SOCIAL_CLEANER_DATA_DIR`).

## Quick start

### Bilibili

```bash
social-cleaner bilibili login                       # QR code in the terminal, scan with the app
social-cleaner bilibili me
social-cleaner bilibili followings list --with-detail --page-size 50 | jq '.items[] | select(.detail.stat.follower < 1000) | .mid'
social-cleaner bilibili tag add-users 11 22 33 --tag-name to-review   # safety net: group first, audit in the app
social-cleaner bilibili followings unfollow 11 22 33
social-cleaner bilibili history clear --yes
social-cleaner bilibili clean-all --yes             # clear followings + favorites + dynamics + history
```

Every command prints JSON to stdout (`--pretty` for a repr); logs go to
stderr, so piping into `jq` works. See [docs/bilibili.md](docs/bilibili.md).

### X

```bash
social-cleaner x login                              # visible browser; log in once, the profile is kept
social-cleaner x clean --tweets --replies --max 5   # conservative first run: 5 items per type
social-cleaner x clean --likes --bookmarks --max 200 --headless
```

Reviewed unfollowing — the recommended way to prune who you follow:

```bash
social-cleaner x followings export                              # → followings.jsonl / .csv
social-cleaner x followings classify --input …/followings.jsonl # → candidates.jsonl, review.csv, empty approved-unfollow.jsonl
#   copy the handles you really want gone into approved-unfollow.jsonl
social-cleaner x followings dry-run --input …/approved-unfollow.jsonl
social-cleaner x followings execute --confirm-file …/approved-unfollow.jsonl
social-cleaner x followings resume --run-id <id>                # after an interruption
```

Defaults are deliberately slow (6–14 s between unfollows, 10 per run, cooldown
every 20) and refuse to run headless. Tune with a JSON file
(`docs/x-config.example.json`) or `X_*` variables. See [docs/x.md](docs/x.md).

## Safety model

- **Nothing is undoable, so nothing is silent.** Every unfollow / delete
  appends and flushes a JSON line to `~/.social-cleaner/audit.jsonl` (mode `0600`)
  with platform, action, target, outcome. Partial cleans report `stopped_reason`
  instead of pretending to have finished.
- **Rate limits are respected, not raced.** Bilibili: shared token bucket
  (1.5 req/s) plus exponential backoff on `-352` / `-799` / `-509` / HTTP
  412 / 429. X: a 429 on x.com's internal API renders the page blank, which
  looks like "logged out" or "nothing left"; the tool watches the network for
  it across both content cleanup and following export/execute and stops with
  exit code 75 instead of re-logging in or skipping work.
- **Humans in the loop where it matters.** X unfollowing goes export →
  classify → *you edit the approval file* → dry-run → execute, in a visible
  browser. `clean` counts down before starting; `clear` commands ask.
- **Guard rails against the wrong account.** One run per browser profile
  (atomic lock with heartbeat), `X_TARGET_USERNAME` refuses to touch any other
  session, resume refuses a session file written for another handle.
- **Your data stays yours.** No hosted service, no telemetry, no third-party
  server in the path. Exported following lists contain other people's
  profile data — they are written under your data dir and ignored by git.

## Configuration

All settings are environment variables with safe defaults; see
[`.env.example`](.env.example). Prefixes: `SOCIAL_CLEANER_*` (shared),
`BILI_*`, `X_*`. The X platform additionally takes a JSON config
(`--config` / `X_CONFIG_FILE`) and a selectors file
(`--selectors` / `X_SELECTORS_FILE`) so front-end changes on x.com need no
code change.

## Project layout

```
src/social_cleaner/
├── cli.py                 root command: bilibili · x
├── core/                  settings · logging · audit · errors · cancellation · retry
└── platforms/
    ├── bilibili/          api/ (client, WBI, endpoints) · services/ · cli/ · runtime · settings
    └── x/                 config · selectors · browser · login · signals · deleter · following · run_lock · runner · cli
```

Adding a platform is a new package under `platforms/` that provides a Typer
sub-app — see [docs/adding-a-platform.md](docs/adding-a-platform.md).

## Development

```bash
pip install -e '.[dev]'
ruff check src tests && ruff format --check src tests
pytest -q
```

## Limitations

- Bilibili has no batch unfollow and no "list my comments" API; deletes are
  sequential and comments cannot be cleaned.
- X is driven through its web UI. Selectors break when X changes markup;
  override them with a selectors file rather than patching code. Speed is
  bounded by X's per-account limits, not by this tool.

## Heritage

`social-cleaner` consolidates two earlier standalone projects by the same author — `bilibili-cleaner` (Python, v1.4.0) and `x-account-cleaner` (TypeScript, v1.0.0, ported to Python here) — into a single multi-platform codebase. The legacy web UI / HTTP server of `bilibili-cleaner` and the scattered shell/Node.js wrappers of `x-account-cleaner` were dropped on purpose; this is a unified, local-first command-line and Python SDK tool.

## License

MIT. Use it on your own accounts. It is not a tool for operating other
people's accounts, evading platform enforcement, or bulk engagement.

---

## Architecture & Ecosystem · qilai (`qilaidav.com`)

This open-source project is engineered and maintained by **[qilai · qilaidav](https://qilaidav.com)** — an independent systems engineering studio focused on **Deterministic Systems Engineering & High-Signal Technical Diagnostics**.

- **Pillar Alignment**: [02 · Cross-Platform Smart Ad Ops](https://qilaidav.com/ads/) · [English Hub](https://qilaidav.com/en/ads/)
- **Studio Website**: [https://qilaidav.com](https://qilaidav.com) ([EN](https://qilaidav.com/en/))
- **Technical Notes & Field Reports**: [https://qilaidav.com/notes/](https://qilaidav.com/notes/)
- **Direct Engineering Contact**: [Telegram @BD8888](https://t.me/BD8888) · [Public Channel @AIKB8](https://t.me/AIKB8) · `hello@qilaidav.com`
