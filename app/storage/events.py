"""Event data access layer (raw sqlite3, parameterized queries only).

Mirrors the readings repository style, but for detected events. Detection is
recomputed every poll cycle, so writes go through :func:`upsert_event`: the
UNIQUE(city, event_type, started_at) key plus an INSERT ... ON CONFLICT DO
UPDATE keep a recurring event as a single row whose ``ended_at`` (and other
mutable fields) are refreshed in place when a later cycle closes it.

``detail`` is a free-form dict in Python but SQLite has no dict type, so it is
JSON-encoded on write and JSON-decoded on read. All SQL values are bound as
parameters — never string-formatted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.storage.db import get_connection

if TYPE_CHECKING:  # avoid a runtime storage -> detection import; only for typing
    from app.detection.detectors import Event


def _row_to_event_dict(row: Any) -> dict:
    """Convert a sqlite3.Row into a plain dict, decoding ``detail`` JSON."""
    data = dict(row)
    raw_detail = data.get("detail")
    data["detail"] = json.loads(raw_detail) if raw_detail is not None else None
    return data


def upsert_event(event: "Event", db_path: str | None = None) -> str:
    """Insert an event, or update the matching row on conflict.

    The event identity is (city, event_type, started_at). On conflict the
    mutable fields — ended_at, severity, reason, detail — are refreshed from the
    incoming event. This is how an event that was open one cycle gets its
    ``ended_at`` filled in a later cycle without creating a duplicate row.

    Returns:
        "inserted" if a new row was created, "updated" if an existing row was
        refreshed (used for per-cycle logging).
    """
    detail_json = json.dumps(event.detail) if event.detail is not None else None

    conn = get_connection(db_path)
    try:
        # Determine insert-vs-update up front so callers can log accurate
        # counts. Safe in our single-threaded poll cycle; the write below is
        # still a single atomic upsert.
        existing = conn.execute(
            """
            SELECT 1 FROM events
            WHERE city = ? AND event_type = ? AND started_at = ?
            """,
            (event.city, event.event_type, event.started_at),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO events (
                city, event_type, severity, started_at, ended_at, reason, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(city, event_type, started_at) DO UPDATE SET
                ended_at = excluded.ended_at,
                severity = excluded.severity,
                reason   = excluded.reason,
                detail   = excluded.detail
            """,
            (
                event.city,
                event.event_type,
                event.severity,
                event.started_at,
                event.ended_at,
                event.reason,
                detail_json,
            ),
        )
        conn.commit()
        return "updated" if existing is not None else "inserted"
    finally:
        conn.close()


def count_events(db_path: str | None = None) -> int:
    """Return the total number of stored events (used by /health later)."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) AS n FROM events")
        row = cursor.fetchone()
        return int(row["n"])
    finally:
        conn.close()


def get_events(
    city: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict]:
    """Return events most-recent-first, with optional city filter and limit.

    ``detail`` is decoded back into a dict (or None) in every returned row.
    """
    conn = get_connection(db_path)
    try:
        if city is not None:
            cursor = conn.execute(
                """
                SELECT id, city, event_type, severity,
                       started_at, ended_at, reason, detail
                FROM events
                WHERE city = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (city, limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, city, event_type, severity,
                       started_at, ended_at, reason, detail
                FROM events
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [_row_to_event_dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
