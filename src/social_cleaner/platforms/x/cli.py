"""``social-cleaner x …`` commands.

Playwright is imported lazily inside the commands so the root CLI stays fast
and the Bilibili commands keep working on a machine without browsers.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from social_cleaner.core.errors import ConfigError

from .config import (
    DeleteOptions,
    XConfig,
    XEnv,
    default_config_path,
    default_selectors_path,
    load_config,
    load_env,
)
from .selectors import selectors_metadata

app = typer.Typer(
    name="x",
    help=(
        "Clean an X (Twitter) account by driving the logged-in x.com web UI in "
        "your own browser. Start with `social-cleaner x login`. Defaults are "
        "deliberately conservative: visible browser, 5 items per run."
    ),
    no_args_is_help=True,
)
followings_app = typer.Typer(
    help=(
        "Reviewed unfollowing: export -> classify -> (edit approved-unfollow.jsonl) "
        "-> dry-run -> execute -> resume."
    ),
    no_args_is_help=True,
)
app.add_typer(followings_app, name="followings")

ConfigOpt = typer.Option(
    None, "--config", "-c", help="JSON config overriding the defaults (X_CONFIG_FILE)."
)
SelectorsOpt = typer.Option(
    None, "--selectors", help="JSON file overriding CSS selectors (X_SELECTORS_FILE)."
)
HeadlessOpt = typer.Option(None, "--headless/--headful", help="Override X_HEADLESS for this run.")
OutputDirOpt = typer.Option(
    None, "--output-dir", help="Where following exports live (default <data_dir>/x/followings)."
)


def _load(config: Path | None, selectors: Path | None) -> tuple[XEnv, XConfig, dict[str, Any]]:
    config_path = config or default_config_path()
    selectors_path = selectors or default_selectors_path()
    try:
        env = load_env()
        cfg = load_config(config_path, selectors_file=selectors_path)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    return env, cfg, selectors_metadata(selectors_path)


def _emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _exit(code: int) -> None:
    raise typer.Exit(code=code)


# ------------------------------------------------------------------ session


@app.command()
def login(config: Path | None = ConfigOpt, selectors: Path | None = SelectorsOpt) -> None:
    """Open a visible browser, let you log in, and save the session for later runs."""
    from .runner import run_with_browser

    env, cfg, meta = _load(config, selectors)

    async def command(session, ctx):  # noqa: ANN001
        return {"username": session.username, "profile": str(env.profile_dir)}

    _exit(
        run_with_browser(
            env, cfg, "login", command, headless=False, work_headless=False, selectors_meta=meta
        )
    )


@app.command()
def whoami(
    config: Path | None = ConfigOpt,
    selectors: Path | None = SelectorsOpt,
    headless: bool | None = HeadlessOpt,
) -> None:
    """Verify the saved session and print the logged-in handle."""
    from .runner import run_with_browser

    env, cfg, meta = _load(config, selectors)

    async def command(session, ctx):  # noqa: ANN001
        _emit({"username": session.username, "headless": session.headless})
        return {"username": session.username}

    _exit(
        run_with_browser(
            env,
            cfg,
            "whoami",
            command,
            headless=headless,
            work_headless=headless,
            selectors_meta=meta,
        )
    )


# -------------------------------------------------------------------- clean


@app.command()
def clean(
    tweets: bool | None = typer.Option(None, "--tweets/--no-tweets", help="Delete your posts."),
    replies: bool | None = typer.Option(
        None, "--replies/--no-replies", help="Delete your replies."
    ),
    retweets: bool | None = typer.Option(
        None, "--retweets/--no-retweets", help="Undo your reposts."
    ),
    likes: bool | None = typer.Option(None, "--likes/--no-likes", help="Unlike posts."),
    bookmarks: bool | None = typer.Option(
        None, "--bookmarks/--no-bookmarks", help="Remove bookmarks."
    ),
    max_per_session: int | None = typer.Option(
        None, "--max", min=1, help="Cap per content type for this run."
    ),
    per_batch: int | None = typer.Option(
        None, "--batch", min=1, help="Items per batch before pausing."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the start countdown."),
    headless: bool | None = HeadlessOpt,
    config: Path | None = ConfigOpt,
    selectors: Path | None = SelectorsOpt,
) -> None:
    """Delete posts / replies, undo reposts, unlike, remove bookmarks. Irreversible.

    Unfollowing is not part of `clean`: use `x followings …` (reviewed) or
    `x followings clear` (direct, with its own confirmation).
    """
    from .deleter import ContentDeleter
    from .runner import countdown, run_with_browser

    env, cfg, meta = _load(config, selectors)
    overrides = {
        k: v
        for k, v in dict(
            tweets=tweets, replies=replies, retweets=retweets, likes=likes, bookmarks=bookmarks
        ).items()
        if v is not None
    }
    delete = replace(cfg.delete, **overrides) if overrides else cfg.delete
    execution = cfg.execution
    if max_per_session is not None:
        execution = replace(execution, max_per_session=max_per_session)
    if per_batch is not None:
        execution = replace(execution, per_batch=per_batch)
    if execution.max_per_session < execution.per_batch:
        execution = replace(execution, per_batch=execution.max_per_session)
    cfg = replace(cfg, delete=delete, execution=execution)
    if not delete.enabled():
        typer.echo(
            "nothing selected: enable at least one of "
            "--tweets/--replies/--retweets/--likes/--bookmarks",
            err=True,
        )
        _exit(2)

    async def command(session, ctx):  # noqa: ANN001
        typer.echo(
            f"About to clean @{session.username}: {', '.join(delete.enabled())} "
            f"(max {execution.max_per_session} per type, {execution.per_batch} per batch)",
            err=True,
        )
        await countdown(0 if yes else env.start_countdown_seconds, "Cleanup")
        deleter = ContentDeleter(session.page, cfg, env)
        try:
            stats = await deleter.run(session.username)
        finally:
            ctx.stats = deleter.stats.to_dict()
        _emit({"username": session.username, "stats": stats.to_dict()})
        return {"stats": stats.to_dict()}

    _exit(
        run_with_browser(
            env,
            cfg,
            "clean",
            command,
            headless=headless,
            work_headless=headless,
            selectors_meta=meta,
        )
    )


# --------------------------------------------------------------- followings


@followings_app.command("export")
def followings_export(
    run_id: str | None = typer.Option(
        None, "--run-id", help="Reuse a run id instead of generating one."
    ),
    output_dir: Path | None = OutputDirOpt,
    headless: bool | None = HeadlessOpt,
    config: Path | None = ConfigOpt,
    selectors: Path | None = SelectorsOpt,
) -> None:
    """Scroll your following list and export it as JSONL + CSV (read-only)."""
    from .following import FollowingCollector
    from .runner import run_with_browser

    env, cfg, meta = _load(config, selectors)

    async def command(session, ctx):  # noqa: ANN001
        result = await FollowingCollector(session.page, cfg, session.username, output_dir).export(
            run_id
        )
        payload = {
            "run_id": result.run_id,
            "count": result.count,
            "jsonl": str(result.jsonl_path),
            "csv": str(result.csv_path),
            "next": f"social-cleaner x followings classify --input {result.jsonl_path}",
        }
        _emit(payload)
        return payload

    _exit(
        run_with_browser(
            env,
            cfg,
            "followings export",
            command,
            headless=headless,
            work_headless=headless,
            selectors_meta=meta,
        )
    )


@followings_app.command("classify")
def followings_classify(
    input_file: Path = typer.Option(
        ..., "--input", "-i", exists=True, dir_okay=False, help="followings.jsonl from export."
    ),
    config: Path | None = ConfigOpt,
) -> None:
    """Apply keep/drop rules locally.

    Writes candidates.jsonl, keep-list.jsonl, review.csv and an empty
    approved-unfollow.jsonl next to the input file.
    """
    from .following import classify_file

    _, cfg, _ = _load(config, None)
    try:
        result = classify_file(input_file, cfg.following)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        _exit(2)
        return
    _emit(
        {
            "total": result.total,
            "candidates": result.candidates,
            "keep": result.keep,
            "candidates_file": str(result.candidates_path),
            "keep_list_file": str(result.keep_list_path),
            "review_file": str(result.review_path),
            "approved_file": str(result.approved_template_path),
            "next": (
                f"edit {result.approved_template_path.name}, then "
                f"social-cleaner x followings dry-run --input {result.approved_template_path}"
            ),
        }
    )


@followings_app.command("dry-run")
def followings_dry_run(
    input_file: Path = typer.Option(
        ..., "--input", "-i", exists=True, dir_okay=False, help="approved-unfollow.jsonl."
    ),
) -> None:
    """Show which handles `execute` would unfollow. Opens no browser."""
    from .following import dry_run

    try:
        items = dry_run(input_file)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        _exit(2)
        return
    _emit(
        {
            "count": len(items),
            "items": [{"handle": i.handle, "display_name": i.display_name} for i in items],
        }
    )


@followings_app.command("execute")
def followings_execute(
    confirm_file: Path = typer.Option(
        ...,
        "--confirm-file",
        exists=True,
        dir_okay=False,
        help="approved-unfollow.jsonl you edited.",
    ),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Store session.json under this run id instead of next to the file."
    ),
    output_dir: Path | None = OutputDirOpt,
    config: Path | None = ConfigOpt,
    selectors: Path | None = SelectorsOpt,
) -> None:
    """Unfollow the approved handles slowly in a visible browser; resumable."""
    from .following import FollowingExecutor
    from .runner import run_with_browser

    env, cfg, meta = _load(config, selectors)

    async def command(session, ctx):  # noqa: ANN001
        executor = FollowingExecutor(
            session.page,
            cfg,
            env,
            session.username,
            headless=session.headless,
            output_dir=output_dir,
        )
        result = await executor.execute(confirm_file, run_id)
        s = result.session
        payload = {
            "run_id": s.run_id,
            "status": s.status,
            "stop_reason": s.stop_reason,
            "success": s.success,
            "failed": s.failed,
            "pending": s.pending,
            "session": str(result.session_path),
        }
        _emit(payload)
        return payload

    _exit(
        run_with_browser(
            env,
            cfg,
            "followings execute",
            command,
            headless=False,
            work_headless=False,
            selectors_meta=meta,
        )
    )


@followings_app.command("resume")
def followings_resume(
    run_id: str = typer.Option(..., "--run-id"),
    output_dir: Path | None = OutputDirOpt,
    config: Path | None = ConfigOpt,
    selectors: Path | None = SelectorsOpt,
) -> None:
    """Continue an interrupted `execute` from its session.json."""
    from .following import FollowingExecutor
    from .runner import run_with_browser

    env, cfg, meta = _load(config, selectors)

    async def command(session, ctx):  # noqa: ANN001
        executor = FollowingExecutor(
            session.page,
            cfg,
            env,
            session.username,
            headless=session.headless,
            output_dir=output_dir,
        )
        result = await executor.resume(run_id)
        s = result.session
        payload = {
            "run_id": s.run_id,
            "status": s.status,
            "stop_reason": s.stop_reason,
            "success": s.success,
            "failed": s.failed,
            "pending": s.pending,
            "session": str(result.session_path),
        }
        _emit(payload)
        return payload

    _exit(
        run_with_browser(
            env,
            cfg,
            "followings resume",
            command,
            headless=False,
            work_headless=False,
            selectors_meta=meta,
        )
    )


@followings_app.command("clear")
def followings_clear(
    max_per_session: int = typer.Option(..., "--max", min=1, help="How many to unfollow this run."),
    per_batch: int | None = typer.Option(None, "--batch", min=1),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm without prompting."),
    headless: bool | None = HeadlessOpt,
    config: Path | None = ConfigOpt,
    selectors: Path | None = SelectorsOpt,
) -> None:
    """Unfollow from the top of your following list without review.

    Prefer the export -> classify -> execute flow.
    """
    from .deleter import ContentDeleter
    from .runner import countdown, run_with_browser

    env, cfg, meta = _load(config, selectors)
    execution = replace(cfg.execution, max_per_session=max_per_session)
    if per_batch is not None:
        execution = replace(execution, per_batch=per_batch)
    if execution.max_per_session < execution.per_batch:
        execution = replace(execution, per_batch=execution.max_per_session)
    cfg = replace(cfg, execution=execution)
    if not yes and not typer.confirm(
        f"Unfollow up to {max_per_session} account(s) without review?"
    ):
        raise typer.Abort()

    async def command(session, ctx):  # noqa: ANN001
        await countdown(0 if yes else env.start_countdown_seconds, "Direct unfollow")
        deleter = ContentDeleter(session.page, cfg, env)
        try:
            stats = await deleter.unfollow_all(session.username)
        finally:
            ctx.stats = deleter.stats.to_dict()
        _emit({"username": session.username, "unfollowed": stats.following, "errors": stats.errors})
        return {"stats": stats.to_dict()}

    _exit(
        run_with_browser(
            env,
            cfg,
            "followings clear",
            command,
            headless=headless,
            work_headless=headless,
            selectors_meta=meta,
        )
    )


__all__ = ["app", "DeleteOptions"]
