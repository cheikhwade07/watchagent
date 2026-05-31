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
from app.poller.client import CITIES, fetch_city
from app.storage.repository import insert_reading

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
    return inserted


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
