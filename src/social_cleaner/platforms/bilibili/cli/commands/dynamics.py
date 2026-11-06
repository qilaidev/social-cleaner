from __future__ import annotations

import typer

from ...services import DynamicService
from .._runtime import emit, make_client, resolve_mid, run_async

app = typer.Typer(help="Inspect and selectively delete dynamics.")


@app.command("list")
def list_cmd(
    mid: int | None = typer.Option(None),
    offset: str = typer.Option("", help="Cursor from prior call."),
    json_output: bool = typer.Option(True, "--json/--pretty"),
) -> None:
    real_mid = resolve_mid(mid)

    async def run() -> None:
        async with make_client() as client:
            data = await DynamicService(client).list_page(real_mid, offset=offset or None)
        emit(data, json_output=json_output)

    run_async(run())


@app.command()
def delete(
    ids: list[str] = typer.Argument(...),
    json_output: bool = typer.Option(True, "--json/--pretty"),
) -> None:
    """Delete specific dynamic ids."""

    async def run() -> None:
        async with make_client() as client:
            result = await DynamicService(client).delete_many(ids)
        emit(result, json_output=json_output)

    run_async(run())


@app.command()
def clear(
    mid: int | None = typer.Option(None),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(True, "--json/--pretty"),
) -> None:
    """Delete every dynamic on the account."""
    real_mid = resolve_mid(mid)
    if not yes and not typer.confirm(f"Delete ALL dynamics for mid={real_mid}?"):
        raise typer.Abort()

    async def run() -> None:
        async with make_client() as client:
            result = await DynamicService(client).clear_all(real_mid)
        emit(result, json_output=json_output)

    run_async(run())
