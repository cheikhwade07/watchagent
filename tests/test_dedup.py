"""Idempotent-dedup tests for the storage + poll path.

No real network calls are made: the Open-Meteo HTTP call is mocked with an
httpx MockTransport that always returns the same ``current`` block.
"""

from __future__ import annotations

import asyncio

import httpx

from app.poller.scheduler import poll_once
from app.storage.repository import count_readings, get_readings, insert_reading

FIXED_OBSERVED_AT = "2026-05-31T12:00"


def test_insert_or_ignore_dedups_same_city_and_observed_at():
    """Inserting the identical (city, observed_at) twice yields exactly one row."""
    first = insert_reading(
        city="Ottawa",
        observed_at=FIXED_OBSERVED_AT,
        fetched_at="2026-05-31T16:00:00+00:00",
        temperature_2m=21.0,
    )
    second = insert_reading(
        city="Ottawa",
        observed_at=FIXED_OBSERVED_AT,
        fetched_at="2026-05-31T16:02:00+00:00",  # later fetch, same observation
        temperature_2m=21.0,
    )

    assert first is True  # first insert created a row
    assert second is False  # duplicate was ignored

    ottawa = get_readings(city="Ottawa")
    assert len(ottawa) == 1


def _mock_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "current": {
                "time": FIXED_OBSERVED_AT,
                "temperature_2m": 21.0,
                "apparent_temperature": 20.0,
                "precipitation": 0.0,
                "wind_speed_10m": 12.0,
                "weather_code": 1,
            }
        },
    )


def test_poll_cycle_is_idempotent_across_runs():
    """Running the same poll cycle twice does not duplicate rows."""

    async def run() -> tuple[int, int]:
        transport = httpx.MockTransport(_mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            first = await poll_once(client)
            second = await poll_once(client)
            return first, second

    first_inserted, second_inserted = asyncio.run(run())

    # Three cities inserted on the first cycle, zero on the second (all dupes).
    assert first_inserted == 3
    assert second_inserted == 0
    assert count_readings() == 3
    # And exactly one row per (city, observed_at).
    assert len(get_readings(city="Ottawa")) == 1
