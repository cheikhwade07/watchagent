"""Application configuration.

Future home for environment-variable-driven settings such as the SQLite
database path (read from WATCHAGENT_DB_PATH), poll interval, and the list of
monitored locations.

Phase 2 implements this (likely via pydantic-settings or a small dataclass).
"""

# Phase 2: define a Settings object loaded from environment variables.
