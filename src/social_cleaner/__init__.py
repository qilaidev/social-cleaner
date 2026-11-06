"""social-cleaner: one CLI to inspect and clean your own social-media accounts.

Each platform lives under :mod:`social_cleaner.platforms` and plugs into the
shared core (settings, logging, audit trail, retry, rate limiting, task queue).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
