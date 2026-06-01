"""Application configuration.

Environment-variable-driven settings with sensible defaults that work both
inside Docker (where docker-compose overrides WATCHAGENT_DB_PATH to /data) and
when running locally (where the database lives under ./data relative to the
project root).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Default DB path is relative to the project root so the app runs OUTSIDE
# Docker without extra setup. Docker compose overrides WATCHAGENT_DB_PATH to
# /data/watchagent.db via its environment block.
DEFAULT_DB_PATH = "./data/watchagent.db"
DEFAULT_POLL_INTERVAL_SECONDS = 120

# Smallest interval we will ever sleep between poll cycles. A 0/negative value
# would turn asyncio.sleep into a tight loop hammering the upstream API.
MIN_POLL_INTERVAL_SECONDS = 1


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_poll_interval() -> int:
    """Parse WATCHAGENT_POLL_INTERVAL_SECONDS defensively.

    get_settings() runs on every DB operation, so a malformed value must never
    raise here; on a missing/invalid/non-positive value we fall back to
    DEFAULT_POLL_INTERVAL_SECONDS and always enforce a sane minimum.
    """
    raw = os.environ.get(
        "WATCHAGENT_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_POLL_INTERVAL_SECONDS
    if value < MIN_POLL_INTERVAL_SECONDS:
        value = DEFAULT_POLL_INTERVAL_SECONDS
    return value


@dataclass(frozen=True)
class Settings:
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "WATCHAGENT_DB_PATH", DEFAULT_DB_PATH
        )
    )
    poll_interval_seconds: int = field(default_factory=_env_poll_interval)
    poller_enabled: bool = field(
        default_factory=lambda: _env_bool(
            os.environ.get("POLLER_ENABLED", "true")
        )
    )

    def ensure_db_parent(self) -> None:
        """Create the parent directory of the SQLite file if it is missing."""
        parent = Path(self.db_path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Build a fresh Settings instance from the current environment.

    Settings are read on each call (rather than cached at import time) so that
    tests can adjust environment variables — e.g. POLLER_ENABLED and
    WATCHAGENT_DB_PATH — before the application reads them.
    """
    return Settings()
