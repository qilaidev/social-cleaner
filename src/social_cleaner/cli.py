"""``social-cleaner`` entry point.

One root command, one sub-app per platform. Platform sub-apps are imported
eagerly so ``--help`` lists them, while heavy dependencies (such as Playwright)
are imported lazily inside the commands that need them.
"""

from __future__ import annotations

import typer

from . import __version__
from .core.logging import configure_logging
from .platforms.bilibili.cli import app as bilibili_app
from .platforms.x.cli import app as x_app

app = typer.Typer(
    name="social-cleaner",
    help=(
        "Inspect and clean your own social-media accounts from one CLI.\n\n"
        "Platforms: bilibili (official web API), x (browser automation of x.com). "
        "Every destructive action is written to an append-only audit log; nothing "
        "is ever sent to a third-party server."
    ),
    no_args_is_help=True,
    rich_markup_mode=None,
)

app.add_typer(bilibili_app, name="bilibili")
app.add_typer(x_app, name="x")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print the version and exit.",
    ),
) -> None:
    # Logs go to stderr, so piping stdout JSON into jq works cleanly;
    # SOCIAL_CLEANER_LOG_LEVEL=ERROR silences informational output.
    configure_logging()


if __name__ == "__main__":  # pragma: no cover
    app()
