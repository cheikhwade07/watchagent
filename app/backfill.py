"""One-shot historical BACKFILL (Phase 2d).

Populates the database with ~7 days of *settled* past weather from the
Open-Meteo ARCHIVE (ERA5) API, then runs the existing detection pipeline over
the now-populated history so the detectors actually fire events and the demo
database looks realistic.

Run as a module::

    python -m app.backfill

This is intentionally separate from the live poller (``app.poller``):

  * it talks to a DIFFERENT endpoint — the archive API, which uses ``hourly=``
    plus a ``start_date``/``end_date`` window (NOT the live ``current=``
    endpoint), and
  * it is one-shot rather than a loop.

Everything else is REUSED, not reinvented: the SAME ``CITIES`` constant and the
SAME hourly variable list as the live client, the SAME idempotent
``insert_reading`` path (``INSERT OR IGNORE`` on ``UNIQUE(city, observed_at)``),
and the SAME detection flow (``detect_and_store`` -> ``run_detectors`` ->
``upsert_event``) the poll cycle uses.

ARCHIVE DELAY: the ERA5 archive lags real time by a few days, so the most recent
~2-5 days are not available yet. We therefore backfill a SETTLED window in the
past — from ``BACKFILL_START_DAYS_AGO`` to ``BACKFILL_END_DAYS_AGO`` days ago,
inclusive — never up to "today".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from app.poller.client import CITIES, CURRENT_FIELDS, City, CurrentReading
from app.poller.scheduler import detect_and_store
from app.storage.db import init_db
from app.storage.repository import insert_reading

logger = logging.getLogger(__name__)

# Archive endpoint — DISTINCT from the live forecast endpoint. It uses hourly=
# (not current=) and requires start_date/end_date in YYYY-MM-DD.
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# The hourly variable list is identical to the live client's CURRENT_FIELDS, so
# we reuse that constant rather than redefining it (keeps the two in lock-step).
ARCHIVE_HOURLY_FIELDS = CURRENT_FIELDS

# Settled backfill window, expressed as named day-offsets from "today" so the
# delay assumption is documented and tunable. The ERA5 archive is only complete
# a few days back, so we request 9..3 days ago (inclusive) and never up to today.
BACKFILL_START_DAYS_AGO = 9
BACKFILL_END_DAYS_AGO = 3

# Number of whole days the window spans (inclusive), and the corresponding count
# of hourly readings. Used to size the detection pass below.
BACKFILL_WINDOW_DAYS = BACKFILL_START_DAYS_AGO - BACKFILL_END_DAYS_AGO + 1  # 7

# The detection fetch is bounded per city by DETECTION_HISTORY_PER_CITY (72,
# ~3 days). Our backfill imports ~7 days (~168 hourly readings) per city, which
# is LARGER than that bound, so the default fetch would only scan the most
# recent ~3 days of what we just imported. We therefore widen the per-city
# limit for the backfill's detection pass so it sees the WHOLE imported window.
# (A one-day margin absorbs DST / inclusive-edge off-by-one hours.) The live
# poller never passes this, so its cadence-bound default behaviour is untouched.
BACKFILL_DETECTION_PER_CITY = BACKFILL_WINDOW_DAYS * 24 + 24  # 192


def archive_window(today: date | None = None) -> tuple[str, str]:
    """Return the (start_date, end_date) YYYY-MM-DD window to request.

    Computed from ``today`` (defaults to the current UTC date) using the named
    day-offset constants. Both bounds are inclusive.
    """
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=BACKFILL_START_DAYS_AGO)
    end = today - timedelta(days=BACKFILL_END_DAYS_AGO)
    return start.isoformat(), end.isoformat()


def _parse_hourly(city_name: str, payload: dict) -> list[CurrentReading]:
    """Zip the archive's parallel ``hourly`` arrays into per-hour readings.

    The archive response carries an ``hourly`` object whose ``time`` list and
    one list per variable are PARALLEL (index i across every list describes the
    same hour). We zip them on the ``time`` spine into one ``CurrentReading``
    per hour. ``observed_at`` is kept as the raw ISO ``time`` string so it
    matches the live poller's stored format exactly.
    """
    hourly = payload["hourly"]
    rows = zip(
        hourly["time"],
        hourly["temperature_2m"],
        hourly["apparent_temperature"],
        hourly["precipitation"],
        hourly["wind_speed_10m"],
        hourly["weather_code"],
    )
    return [
        CurrentReading(
            city=city_name,
            observed_at=observed_at,
            temperature_2m=temperature_2m,
            apparent_temperature=apparent_temperature,
            precipitation=precipitation,
            wind_speed_10m=wind_speed_10m,
            weather_code=weather_code,
        )
        for (
            observed_at,
            temperature_2m,
            apparent_temperature,
            precipitation,
            wind_speed_10m,
            weather_code,
        ) in rows
    ]


async def fetch_city_history(
    client: httpx.AsyncClient,
    city: City,
    start_date: str,
    end_date: str,
) -> list[CurrentReading]:
    """Fetch and parse hourly archive history for one city.

    Mirrors the live client's philosophy: a failed request or parse logs a
    WARNING (with the city and error) and returns an EMPTY list so one bad city
    never aborts the whole backfill.
    """
    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "hourly": ARCHIVE_HOURLY_FIELDS,
        "wind_speed_unit": "kmh",
        "timezone": "auto",
        "start_date": start_date,
        "end_date": end_date,
    }
    try:
        response = await client.get(ARCHIVE_URL, params=params)
        response.raise_for_status()
        return _parse_hourly(city.name, response.json())
    except Exception as exc:  # noqa: BLE001 - skip this city, never abort backfill
        logger.warning(
            "Failed to fetch archive weather for %s: %s", city.name, exc
        )
        return []


async def backfill_readings(client: httpx.AsyncClient) -> int:
    """Fetch history for every city and insert it. Returns total rows inserted.

    ``fetched_at`` is the current UTC time of this run (same convention as the
    live poller). Inserts go through the existing idempotent ``insert_reading``,
    so re-running the backfill never creates duplicate readings.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    start_date, end_date = archive_window()
    logger.info(
        "Backfilling archive weather for %d cities, %s..%s (inclusive)",
        len(CITIES),
        start_date,
        end_date,
    )

    total_inserted = 0
    for city in CITIES:
        readings = await fetch_city_history(client, city, start_date, end_date)
        inserted = 0
        for reading in readings:
            if insert_reading(
                city=reading.city,
                observed_at=reading.observed_at,
                fetched_at=fetched_at,
                temperature_2m=reading.temperature_2m,
                apparent_temperature=reading.apparent_temperature,
                precipitation=reading.precipitation,
                wind_speed_10m=reading.wind_speed_10m,
                weather_code=reading.weather_code,
            ):
                inserted += 1
        logger.info(
            "%s: %d new reading(s) inserted (%d fetched)",
            city.name,
            inserted,
            len(readings),
        )
        total_inserted += inserted
    return total_inserted


async def run_backfill() -> tuple[int, int, int, int]:
    """Full backfill: ensure schema, import history, then run detection.

    Returns ``(readings_inserted, events_detected, events_inserted,
    events_updated)``.
    """
    init_db()
    async with httpx.AsyncClient(timeout=60.0) as client:
        readings_inserted = await backfill_readings(client)

    # Reuse the EXACT poll-cycle detection flow, only widened (per_city) so it
    # scans the full imported window rather than the default ~3-day bound.
    detected, events_inserted, events_updated = detect_and_store(
        per_city=BACKFILL_DETECTION_PER_CITY
    )

    logger.info(
        "Backfill complete: %d reading(s) inserted; "
        "%d event(s) detected (%d new, %d updated)",
        readings_inserted,
        detected,
        events_inserted,
        events_updated,
    )
    return readings_inserted, detected, events_inserted, events_updated


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_backfill())


if __name__ == "__main__":
    main()
