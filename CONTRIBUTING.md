# Contributing

## Setup

```bash
git clone https://github.com/qilaidev/social-cleaner.git
cd social-cleaner
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium        # only if you work on the X platform
```

## Before you push

```bash
ruff check src tests
ruff format src tests
pytest -q
```

CI runs the same on Python 3.10, 3.11 and 3.12.

## Ground rules

- **Never commit credentials, cookies, browser profiles or exported
  following lists.** `.gitignore` covers the default locations; check
  `git status` anyway.
- Every destructive code path records to the audit log and distinguishes
  "stopped early" from "finished".
- Conservative defaults. If a change makes the tool faster by default,
  explain in the PR why it does not raise the risk-control rate.
- Tests do not touch the network or a real browser: mock HTTP with `respx`,
  fake the Playwright page (see `tests/x/test_signals_and_login.py`).
- New platform? Follow `docs/adding-a-platform.md`.
- Keep the CHANGELOG current under *Unreleased*.

## Reporting a broken selector (X)

X changes its markup. Open an issue with the command, the log line naming the
selector, and the `data-testid` you found in DevTools. A PR against
`platforms/x/selectors.py` with the same information is even better.
