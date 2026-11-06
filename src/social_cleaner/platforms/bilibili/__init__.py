"""Bilibili (哔哩哔哩) support.

Talks to the official web API over HTTPS with the user's own ``SESSDATA`` /
``bili_jct`` cookies. No browser is involved.

Layers, bottom to top:

- :mod:`.api` — thin, WBI-signed wrappers around individual endpoints with a
  process-wide token bucket and risk-control retry.
- :mod:`.services` — cleanup logic (list, enrich, selectively delete, clear).
- :mod:`.cli` — ``social-cleaner bilibili …`` Typer sub-app.
- :mod:`.http` — FastAPI routers mounted by ``social-cleaner serve``.
"""

PLATFORM = "bilibili"
