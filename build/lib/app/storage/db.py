"""SQLite engine and connection management (raw sqlite3, no ORM).

Provides a connection factory that points at the configured database file with
WAL journaling and foreign-key enforcement enabled, plus a schema initializer
that creates the readings table on startup.
"""

from __future__ import annotations

import sqlite3

from app.config import get_settings

# Hand-written schema. The UNIQUE(city, observed_at) constraint is what makes
# inserts idempotent: re-inserting the same reading is silently ignored.
CREATE_READINGS_TABLE = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    temperature_2m REAL,
    apparent_temperature REAL,
    precipitation REAL,
    wind_speed_10m REAL,
    weather_code INTEGER,
    UNIQUE(city, observed_at)
);
"""


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and dict-like rows.

    Args:
        db_path: Optional explicit path. Defaults to the configured DB path.
    """
    settings = get_settings()
    path = db_path if db_path is not None else settings.db_path
    if path != ":memory:":
        settings.ensure_db_parent()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create the readings table if it does not already exist."""
    conn = get_connection(db_path)
    try:
        conn.execute(CREATE_READINGS_TABLE)
        conn.commit()
    finally:
        conn.close()
