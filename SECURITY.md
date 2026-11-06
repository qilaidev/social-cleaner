# Security

## Reporting

Email the maintainer via the address on the GitHub profile, or open a private
security advisory on the repository. Please do not file public issues for
credential leaks or account-takeover vectors. Expect an acknowledgement within
a week.

## What the tool handles

- **Bilibili session cookies** (`SESSDATA`, `bili_jct`) — stored atomically at
  `~/.social-cleaner/bilibili/credentials.json` with mode `0600`, or supplied
  via environment variables. Never logged (`httpx` logging is set to
  `WARNING`; audit entries carry targets, not credentials).
- **X browser profile** — a full Chromium profile under
  `~/.social-cleaner/x/browser-data/`, plus a `state.json` cookie snapshot
  (`0600`). Anyone with read access to that directory has your X session.
- **Audit trail** — `~/.social-cleaner/audit.jsonl` (mode `0600`), recording
  deleted item IDs and unfollowed handles locally.
- **Exported following lists** — other people's public profile data. Kept
  under the data directory, ignored by git, never uploaded, and sanitized
  against spreadsheet formula injection in CSV exports.
- **Optional `X_USERNAME` / `X_PASSWORD`** — only used to fill the login
  form locally; manual login is recommended instead.

## Supply chain

Runtime dependencies are pinned with upper bounds in `pyproject.toml`; CI
installs from a clean environment on every push. Playwright downloads
browsers from Microsoft's CDN on `playwright install`.
