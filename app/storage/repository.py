"""Data access layer (raw sqlite3, parameterized queries only).

Insert-with-dedup plus the query helpers used by the /readings and /health
endpoints. SQL values are always bound as parameters — never string-formatted.
"""

from __future__ import annotations

from app.storage.db import get_connection
from app.storage.models import Reading

# How much per-city reading history to feed the detection pipeline each cycle.
# The temperature detector's rolling baseline spans ~2 days, so ~3 days of
# hourly readings gives a comfortable warm-up margin while keeping the per-city
# fetch bounded (independent of the wall clock, so it is deterministic in tests
# and never unbounded as the table grows).
DETECTION_HISTORY_PER_CITY = 72  # ~3 days at one reading/hour


def insert_reading(
    city: str,
    observed_at: str,
    fetched_at: str,
    temperature_2m: float | None = None,
    apparent_temperature: float | None = None,
    precipitation: float | None = None,
    wind_speed_10m: float | None = None,
    weather_code: int | None = None,
    db_path: str | None = None,
) -> bool:
    """Insert a reading, ignoring duplicates on (city, observed_at).

    Returns:
        True if a new row was inserted, False if it was a duplicate that the
        UNIQUE constraint caused to be ignored.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO readings (
                city, observed_at, fetched_at,
                temperature_2m, apparent_temperature, precipitation,
                wind_speed_10m, weather_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                city,
                observed_at,
                fetched_at,
                temperature_2m,
                apparent_temperature,
                precipitation,
                wind_speed_10m,
                weather_code,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_readings(
    city: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict]:
    """Return readings most-recent-first, with optional city filter and limit."""
    conn = get_connection(db_path)
    try:
        if city is not None:
            cursor = conn.execute(
                """
                SELECT id, city, observed_at, fetched_at,
                       temperature_2m, apparent_temperature, precipitation,
                       wind_speed_10m, weather_code
                FROM readings
                WHERE city = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (city, limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, city, observed_at, fetched_at,
                       temperature_2m, apparent_temperature, precipitation,
                       wind_speed_10m, weather_code
                FROM readings
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def count_readings(db_path: str | None = None) -> int:
    """Return the total number of stored readings."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) AS n FROM readings")
        row = cursor.fetchone()
        return int(row["n"])
    finally:
        conn.close()


def get_readings_for_detection(db_path: str | None = None) -> list[Reading]:
    """Return recent readings per city, OLDEST-FIRST, for the detection pipeline.

    This is intentionally a SEPARATE fetch from :func:`get_readings`. The API
    wants readings most-recent-first (ORDER BY observed_at DESC), but the
    detectors consume *chronological* (oldest-first) sequences — they step
    through readings in observation order to drive their onset/close state
    machines. Passing DESC order would be wrong, so here we explicitly re-sort
    ascending rather than relying on any incidental ordering.

    Per city we take the most recent ``DETECTION_HISTORY_PER_CITY`` readings
    (an inner ORDER BY observed_at DESC + LIMIT) and then re-order that slice
    ascending, so the detectors receive the freshest history in chronological
    order. ``Reading`` objects (not dicts) are returned so detectors can use
    attribute access (the ``ReadingLike`` protocol).
    """
    conn = get_connection(db_path)
    try:
        cities = [
            row["city"]
            for row in conn.execute(
                "SELECT DISTINCT city FROM readings"
            ).fetchall()
        ]
        readings: list[Reading] = []
        for city in cities:
            cursor = conn.execute(
                """
                SELECT id, city, observed_at, fetched_at,
                       temperature_2m, apparent_temperature, precipitation,
                       wind_speed_10m, weather_code
                FROM (
                    SELECT id, city, observed_at, fetched_at,
                           temperature_2m, apparent_temperature, precipitation,
                           wind_speed_10m, weather_code
                    FROM readings
                    WHERE city = ?
                    ORDER BY observed_at DESC
                    LIMIT ?
                )
                ORDER BY observed_at ASC
                """,
                (city, DETECTION_HISTORY_PER_CITY),
            )
            readings.extend(Reading.from_row(row) for row in cursor.fetchall())
        return readings
    finally:
        conn.close()
