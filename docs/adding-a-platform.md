# Adding a platform

A platform is a package under `src/social_cleaner/platforms/<name>/`. The
two existing ones show the two transport styles: `bilibili` (HTTP API
client) and `x` (browser automation). Reuse the core; keep platform
specifics inside the package.

## Contract

1. `platforms/<name>/__init__.py` defines `PLATFORM = "<name>"` and a
   docstring explaining the transport and its failure modes.
2. **Settings**: `platforms/<name>/settings.py` reads `<PREFIX>_*` variables
   with the helpers in `social_cleaner.core.settings`. Shared knobs
   (`data_dir`, logging, audit) come from `core.settings.settings`.
3. **Audit**: call `social_cleaner.core.audit.record(PLATFORM, "<noun>.<verb>",
   target, ok=…, error=…)` for every destructive action, success or failure.
4. **Errors**: raise `core.errors` classes so retry and runners behave:
   `RetryableError`, `RateLimitError(retry_after_ms=…)`, `NonRetryableError`,
   `ConfigError`. Never swallow `CancellationError`.
5. **Sleeps**: use `core.retry.sleep_ms` / `random_sleep_ms`, never
   `asyncio.sleep`, so Ctrl+C is honoured mid-wait.
6. **CLI**: expose `app = typer.Typer(name="<name>", no_args_is_help=True)` in
   `platforms/<name>/cli.py`; commands print JSON to stdout and log to
   stderr. Register it in `social_cleaner/cli.py` and add the name to
   `platforms.PLATFORMS`. Import heavy dependencies inside commands.
7. **Tests** under `tests/<name>/`: pure logic with plain pytest; HTTP with
   `respx`; browser code behind fakes (see `tests/x/test_signals_and_login.py`).
8. **Docs**: `docs/<name>.md` and a row in the README table; note anything
   the platform cannot do.

## Design rules that already paid for themselves

- Distinguish "empty" from "blocked". A platform that hides content when it
  rate-limits you will make an unfinished clean look finished.
- Stop when a whole page fails; report `stopped_reason` instead of success.
- Conservative defaults. The first run should touch five things, visibly.
- Anything that cannot be undone gets a human step or a `--yes`.
