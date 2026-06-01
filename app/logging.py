"""Structured logging setup.

Provides a single ``configure_logging()`` helper, called once at process
startup (API lifespan and the backfill script), so the existing
poller/detection ``logging`` calls all emit with a consistent level and format.
Deliberately minimal: a root-logger ``basicConfig`` with the level taken from
the ``LOG_LEVEL`` environment variable (defaulting to ``INFO``).
"""

from __future__ import annotations

import logging
import os

DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """Configure root logging once from the ``LOG_LEVEL`` env var.

    The level name is read from ``LOG_LEVEL`` (default ``INFO``); an
    unrecognised value falls back to ``INFO``. ``force=True`` ensures the
    format/level are applied even if another library (e.g. uvicorn) already
    installed a root handler, keeping all WatchAgent log output consistent.
    """
    level_name = os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)
