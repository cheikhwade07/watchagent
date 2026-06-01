"""SQLite engine and connection management (raw sqlite3, no ORM).

Provides a connection factory that points at the configured database file with
WAL journaling and foreign-key enforcement enabled, plus a schema initializer
that creates the readings and events tables on startup.
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

# Hand-written events schema. UNIQUE(city, event_type, started_at) is the event
# identity / dedup key: detection is recomputed each poll cycle, and this
# constraint (paired with the upsert in the events repository) ensures the same
# event is never duplicated across cycles. severity is nullable (frontal /
# apparent events are not graded), ended_at is nullable while an event is still
# ongoing, and detail holds a JSON-encoded dict (SQLite has no dict type).
CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reason TEXT NOT NULL,
    detail TEXT,
    UNIQUE(city, event_type, started_at)
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
    """Create the readings and events tables if they do not already exist."""
    conn = get_connection(db_path)
    try:
        conn.execute(CREATE_READINGS_TABLE)
        conn.execute(CREATE_EVENTS_TABLE)
        conn.commit()
    finally:
        conn.close()
