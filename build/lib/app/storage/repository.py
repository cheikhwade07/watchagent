"""Data access layer (raw sqlite3, parameterized queries only).

Insert-with-dedup plus the query helpers used by the /readings and /health
endpoints. SQL values are always bound as parameters — never string-formatted.
"""

from __future__ import annotations

from app.storage.db import get_connection


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
