"""In-process poll loop.

Periodically fetches all monitored cities and persists each reading via the
repository (dedup handles repeats). Exposes run_poller() as the entry point
started from the app lifespan, plus clean start/stop helpers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.detection.pipeline import run_detectors
from app.poller.client import CITIES, fetch_city
from app.storage.events import upsert_event
from app.storage.repository import get_readings_for_detection, insert_reading

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def poll_once(client: httpx.AsyncClient) -> int:
    """Fetch all cities once and insert their readings. Returns rows inserted."""
    fetched_at = _utc_now_iso()
    inserted = 0
    for city in CITIES:
        reading = await fetch_city(client, city)
        if reading is None:
            continue
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
    logger.info("Poll cycle complete: %d new reading(s) inserted", inserted)
    detect_and_store()
    return inserted


def detect_and_store(per_city: int | None = None) -> tuple[int, int, int]:
    """Recompute detection over recent stored readings and persist results.

    Option A (recompute): every cycle we re-read the recent reading history
    (oldest-first, via ``get_readings_for_detection``), re-run the full detector
    pipeline, and upsert every emitted event. Because the events table has a
    UNIQUE(city, event_type, started_at) key and ``upsert_event`` does an
    ON CONFLICT update, recomputing is safe: an event seen in a previous cycle
    is not duplicated, and one that closes in a later cycle has its ``ended_at``
    (and other mutable fields) refreshed in place.

    ``per_city`` is forwarded to ``get_readings_for_detection``. The live poll
    cycle calls this with no argument (so the default per-city bound applies and
    its behaviour is unchanged); only the one-shot historical backfill passes a
    wider value to detect over its full imported window.

    Returns:
        (detected, inserted, updated) counts for the cycle.
    """
    readings = get_readings_for_detection(per_city=per_city)
    events = run_detectors(readings)

    inserted = 0
    updated = 0
    for event in events:
        if upsert_event(event) == "inserted":
            inserted += 1
        else:
            updated += 1

    logger.info(
        "Detection cycle complete: %d event(s) detected (%d new, %d updated)",
        len(events),
        inserted,
        updated,
    )
    return len(events), inserted, updated


async def run_poller() -> None:
    """Run the poll loop until cancelled."""
    settings = get_settings()
    interval = settings.poll_interval_seconds
    logger.info("Starting poller (interval=%ss)", interval)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            while True:
                try:
                    await poll_once(client)
                except Exception as exc:  # noqa: BLE001 - keep loop alive
                    logger.warning("Poll cycle failed: %s", exc)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Poller stopped")
            raise


def start_poller() -> asyncio.Task:
    """Launch run_poller() as a background task."""
    return asyncio.create_task(run_poller())


async def stop_poller(task: asyncio.Task) -> None:
    """Cancel a running poller task and wait for it to unwind."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
