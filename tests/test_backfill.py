"""Tests for the one-shot historical backfill (Phase 2d).

NO REAL NETWORK: the Open-Meteo ARCHIVE call is mocked with an httpx
MockTransport that returns small synthetic ``hourly`` parallel-array payloads.
The tests assert that the parallel arrays are parsed and inserted correctly,
that re-running is idempotent, that the reused detection flow fires events over
the imported window, and that the date-window logic is correct.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx

from app import backfill
from app.backfill import (
    BACKFILL_END_DAYS_AGO,
    BACKFILL_START_DAYS_AGO,
    archive_window,
    backfill_readings,
    run_backfill,
)
from app.poller.client import CITIES
from app.storage.events import count_events, get_events
from app.storage.repository import (
    DETECTION_HISTORY_PER_CITY,
    count_readings,
    get_readings,
)

# A small synthetic dry -> wet -> dry hourly sequence (5 hours). The precip
# onset at index 2 is what the precipitation detector should fire on, letting us
# verify the detection-after-backfill path without a long sequence.
SYNTHETIC_TIMES = [
    "2026-05-22T00:00",
    "2026-05-22T01:00",
    "2026-05-22T02:00",
    "2026-05-22T03:00",
    "2026-05-22T04:00",
]
SYNTHETIC_PRECIP = [0.0, 0.0, 2.4, 3.0, 0.0]
HOURS_PER_CITY = len(SYNTHETIC_TIMES)


def _archive_payload() -> dict:
    """A well-formed archive response with PARALLEL hourly arrays."""
    return {
        "hourly": {
            "time": list(SYNTHETIC_TIMES),
            "temperature_2m": [17.0, 18.0, 12.0, 11.0, 16.0],
            "apparent_temperature": [16.0, 17.0, 11.0, 10.0, 15.0],
            "precipitation": list(SYNTHETIC_PRECIP),
            "wind_speed_10m": [10.0, 12.0, 25.0, 27.0, 14.0],
            "weather_code": [1, 2, 61, 63, 3],
        }
    }


def _make_client(captured: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """An AsyncClient whose archive call is mocked (no real network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=_archive_payload())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run_backfill_readings(captured: list[httpx.Request] | None = None) -> int:
    async def run() -> int:
        async with _make_client(captured) as client:
            return await backfill_readings(client)

    return asyncio.run(run())


# ===========================================================================
# Date-window logic
# ===========================================================================
def test_archive_window_is_settled_inclusive_range():
    """9..3 days ago, inclusive, formatted YYYY-MM-DD — never up to today."""
    start, end = archive_window(today=date(2026, 5, 31))

    assert start == "2026-05-22"  # 31 - 9
    assert end == "2026-05-28"  # 31 - 3
    assert BACKFILL_START_DAYS_AGO == 9
    assert BACKFILL_END_DAYS_AGO == 3


def test_archive_window_defaults_to_utc_today():
    """With no argument the window is derived from the current UTC date."""
    start, end = archive_window()

    assert len(start) == len("YYYY-MM-DD") and start.count("-") == 2
    assert len(end) == len("YYYY-MM-DD") and end.count("-") == 2
    assert start < end  # start is further in the past than end


# ===========================================================================
# Parse + insert: parallel arrays -> readings
# ===========================================================================
def test_backfill_parses_parallel_arrays_and_inserts_expected_count():
    """Each city's 5 parallel-array hours become 5 readings (3 cities -> 15)."""
    inserted = _run_backfill_readings()

    assert inserted == HOURS_PER_CITY * len(CITIES)
    assert count_readings() == HOURS_PER_CITY * len(CITIES)


def test_backfill_preserves_raw_observed_at_format():
    """observed_at is stored as the RAW archive ``time`` string (live format)."""
    _run_backfill_readings()

    ottawa = get_readings(city="Ottawa", limit=100)
    stored = sorted(r["observed_at"] for r in ottawa)
    assert stored == sorted(SYNTHETIC_TIMES)


def test_backfill_hits_archive_endpoint_with_window_params():
    """The request goes to the archive host with hourly= + start/end_date."""
    captured: list[httpx.Request] = []
    _run_backfill_readings(captured)

    assert len(captured) == len(CITIES)
    url = captured[0].url
    assert url.host == "archive-api.open-meteo.com"
    assert url.path == "/v1/archive"
    params = dict(url.params)
    assert "hourly" in params and "current" not in params
    assert "temperature_2m" in params["hourly"]
    expected_start, expected_end = archive_window()
    assert params["start_date"] == expected_start
    assert params["end_date"] == expected_end


# ===========================================================================
# Idempotency: re-running must not duplicate readings
# ===========================================================================
def test_backfill_is_idempotent_across_runs():
    """Re-running the backfill inserts zero new rows (INSERT OR IGNORE dedup)."""
    first = _run_backfill_readings()
    second = _run_backfill_readings()

    assert first == HOURS_PER_CITY * len(CITIES)
    assert second == 0  # all duplicates on the second run
    assert count_readings() == HOURS_PER_CITY * len(CITIES)


# ===========================================================================
# Per-city failure is isolated (one bad city does not abort the backfill)
# ===========================================================================
def test_backfill_skips_failing_city_and_continues():
    """A city whose archive fetch errors is skipped; the others still import."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Fail only Vancouver (matched by its latitude in the query string).
        if "49.25" in str(request.url):
            return httpx.Response(500, json={"error": True})
        return httpx.Response(200, json=_archive_payload())

    async def run() -> int:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await backfill_readings(client)

    inserted = asyncio.run(run())

    # Two healthy cities import; Vancouver contributes nothing but does not crash.
    assert inserted == HOURS_PER_CITY * (len(CITIES) - 1)
    assert get_readings(city="Vancouver") == []


# ===========================================================================
# Detection after backfill: reuse the poll-cycle flow, full-window
# ===========================================================================
def test_full_run_backfill_detects_and_stores_events():
    """End-to-end: import history then run detection -> precipitation events stored."""

    async def run() -> tuple[int, int, int, int]:
        async with _make_client() as client:
            inserted = await backfill_readings(client)
        from app.poller.scheduler import detect_and_store

        detected, ins, upd = detect_and_store(
            per_city=backfill.BACKFILL_DETECTION_PER_CITY
        )
        return inserted, detected, ins, upd

    inserted, detected, ins, upd = asyncio.run(run())

    assert inserted == HOURS_PER_CITY * len(CITIES)
    assert detected >= len(CITIES)  # at least one precipitation onset per city
    assert ins >= len(CITIES)
    assert count_events() >= len(CITIES)

    ottawa_precip = [
        e for e in get_events(city="Ottawa") if e["event_type"] == "precipitation"
    ]
    assert len(ottawa_precip) == 1
    assert ottawa_precip[0]["started_at"] == "2026-05-22T02:00"  # the wet hour


def test_run_backfill_end_to_end_via_mocked_client(monkeypatch):
    """run_backfill() wired end-to-end with a mocked AsyncClient (no network)."""
    client = _make_client()
    monkeypatch.setattr(backfill.httpx, "AsyncClient", lambda *a, **k: client)

    readings_inserted, detected, ins, upd = asyncio.run(run_backfill())

    assert readings_inserted == HOURS_PER_CITY * len(CITIES)
    assert detected >= len(CITIES)
    assert ins >= len(CITIES)


# ===========================================================================
# Backward-compat guard: live detection bound is untouched
# ===========================================================================
def test_live_detection_bound_default_is_unchanged():
    """The widening is opt-in: the default per-city detection bound is still 72."""
    assert DETECTION_HISTORY_PER_CITY == 72
    assert backfill.BACKFILL_DETECTION_PER_CITY > DETECTION_HISTORY_PER_CITY
