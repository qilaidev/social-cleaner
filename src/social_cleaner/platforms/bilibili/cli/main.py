from __future__ import annotations

import typer

from ..services import CleanerService
from ._runtime import emit, make_client, resolve_mid, run_async
from .commands import auth as auth_cmd
from .commands import dynamics as dynamics_cmd
from .commands import favorites as favorites_cmd
from .commands import followings as followings_cmd
from .commands import history as history_cmd
from .commands import tag as tag_cmd
from .commands import users as users_cmd

app = typer.Typer(
    name="bilibili",
    help=(
        "Inspect and clean a Bilibili account through the official web API. "
        "Commands emit JSON by default (--pretty for a repr). "
        "Log in first with `social-cleaner bilibili login`."
    ),
    no_args_is_help=True,
)

app.add_typer(auth_cmd.app, name="auth")
app.add_typer(users_cmd.app, name="users")
app.add_typer(followings_cmd.app, name="followings")
app.add_typer(favorites_cmd.app, name="favorites")
app.add_typer(dynamics_cmd.app, name="dynamics")
app.add_typer(history_cmd.app, name="history")
app.add_typer(tag_cmd.app, name="tag")


# Shortcuts so `social-cleaner bilibili me` works without `auth me`.
@app.command(help="Alias of `auth me` — verify session and print user info.")
def me(json_output: bool = typer.Option(True, "--json/--pretty")) -> None:
    auth_cmd.me(json_output=json_output)


@app.command(help="Alias of `auth login`.")
def login(poll_interval: float = typer.Option(2.0)) -> None:
    auth_cmd.login(poll_interval=poll_interval)


@app.command(help="Alias of `auth logout`.")
def logout() -> None:
    auth_cmd.logout()


@app.command("clean-all")
def clean_all(
    mid: int | None = typer.Option(None, help="Owning account mid; defaults to logged-in user."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_output: bool = typer.Option(True, "--json/--pretty"),
) -> None:
    """Clear followings + favorites + dynamics + watch history in one run. Irreversible."""
    real_mid = resolve_mid(mid)
    if not yes and not typer.confirm(
        f"Clear ALL followings, favorites, dynamics and history for mid={real_mid}?"
    ):
        raise typer.Abort()

    async def run() -> None:
        async with make_client() as client:
            payload = await CleanerService(client).clean_all(real_mid)
        emit(payload, json_output=json_output)

    run_async(run())
