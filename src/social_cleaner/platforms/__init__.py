"""Platform implementations.

Each subpackage owns one platform end to end: transport (HTTP API client or
browser automation), services with the cleanup logic, a Typer sub-app for the
CLI and, optionally, FastAPI routers for ``social-cleaner serve``.

See ``docs/adding-a-platform.md`` for the contract a new platform must meet.
"""

from __future__ import annotations

PLATFORMS: tuple[str, ...] = ("bilibili", "x")
