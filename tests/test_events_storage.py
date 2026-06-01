"""Storage-layer tests for detected events (Phase 2b: detection -> storage).

No network and no real poller: events are written directly through the events
repository, and the "detect from stored readings" path seeds readings via the
readings repository and calls the in-process detection step
(``detect_and_store``). POLLER_ENABLED=false is set globally in conftest, so the
live poll loop never starts.
"""

from __future__ import annotations

from app.detection.detectors import Event
from app.poller.scheduler import detect_and_store
from app.storage.events import count_events, get_events, upsert_event
from app.storage.repository import get_readings_for_detection, insert_reading

FETCHED_AT = "2026-05-31T16:00:00+00:00"


def _event(
    *,
    city: str = "Ottawa",
    event_type: str = "precipitation",
    severity: str | None = "moderate",
    started_at: str = "2026-05-31T12:00",
    ended_at: str | None = None,
    reason: str = "rain began",
    detail: dict | None = None,
) -> Event:
    return Event(
        city=city,
        event_type=event_type,
        severity=severity,
        started_at=started_at,
        ended_at=ended_at,
        reason=reason,
        detail=detail,
    )


# ===========================================================================
# Dedup / upsert: same event identity stored twice -> one row, ended_at updated
# ===========================================================================
def test_upsert_event_dedups_and_fills_ended_at_in_place():
    """The SAME event (city, event_type, started_at) persisted twice with a
    different ended_at yields exactly ONE row whose ended_at is the UPDATED
    (later) value — proving the upsert closes an open event without duplicating.
    """
    open_event = _event(ended_at=None, detail={"precip_mm": 2.4})
    closed_event = _event(ended_at="2026-05-31T15:00", detail={"precip_mm": 2.4})

    first = upsert_event(open_event)
    second = upsert_event(closed_event)

    assert first == "inserted"
    assert second == "updated"  # conflict on the identity key -> update, not insert
    assert count_events() == 1  # exactly one row, no duplicate

    rows = get_events()
    assert len(rows) == 1
    assert rows[0]["ended_at"] == "2026-05-31T15:00"  # reflects the UPDATED value


def test_upsert_event_refreshes_mutable_fields_on_conflict():
    """severity / reason / detail are also refreshed on conflict (not just ended_at)."""
    upsert_event(_event(severity="light", reason="first", detail={"precip_mm": 0.5}))
    upsert_event(
        _event(severity="heavy", reason="second", detail={"precip_mm": 9.0})
    )

    rows = get_events()
    assert len(rows) == 1
    assert rows[0]["severity"] == "heavy"
    assert rows[0]["reason"] == "second"
    assert rows[0]["detail"] == {"precip_mm": 9.0}


# ===========================================================================
# detail round-trip: JSON encode on write, decode on read (incl. None)
# ===========================================================================
def test_detail_dict_round_trips_through_json():
    """A detail dict is stored and read back intact (json.dumps/json.loads)."""
    detail = {
        "mad_distance": 3.42,
        "baseline_median": 18.0,
        "scaled_mad": 1.483,
        "window_size": 12,
    }
    upsert_event(
        _event(event_type="temp_anomaly", severity="minor", detail=detail)
    )

    rows = get_events()
    assert len(rows) == 1
    assert rows[0]["detail"] == detail  # dict survives the JSON encode/decode
    assert isinstance(rows[0]["detail"], dict)


def test_detail_none_round_trips_as_none():
    """An event with no detail stays None on read (None handled, not 'null')."""
    upsert_event(
        _event(
            event_type="frontal_passage",
            severity=None,
            started_at="2026-05-31T13:00",
            detail=None,
        )
    )

    rows = get_events()
    assert len(rows) == 1
    assert rows[0]["detail"] is None
    assert rows[0]["severity"] is None  # nullable severity preserved


# ===========================================================================
# Detect from STORED readings (recompute path) — no network, seeded directly
# ===========================================================================
def test_detection_over_stored_readings_persists_expected_event():
    """Seed a controlled dry->wet sequence directly, run the detection-over-
    stored-readings path, and assert the expected precipitation event lands.

    Readings are inserted OUT OF chronological order on purpose: the detection
    fetch must re-sort oldest-first, so the onset is detected at the wet reading
    regardless of insertion order.
    """
    # Insert wet BEFORE dry to make insertion order non-chronological.
    insert_reading("Ottawa", "2026-05-31T13:00", FETCHED_AT, precipitation=2.4)  # wet
    insert_reading("Ottawa", "2026-05-31T12:00", FETCHED_AT, precipitation=0.0)  # dry

    # The detection fetch must hand the detectors chronological (oldest-first) data.
    ordered = get_readings_for_detection()
    observed = [r.observed_at for r in ordered]
    assert observed == sorted(observed)  # oldest-first, not the DESC API order

    detected, inserted, updated = detect_and_store()

    assert inserted == 1  # the precipitation onset
    assert updated == 0

    events = get_events(city="Ottawa")
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "precipitation"
    assert event["severity"] == "moderate"  # 2.4 mm at onset
    assert event["started_at"] == "2026-05-31T13:00"  # onset at the wet reading
    assert event["ended_at"] is None  # never dried up within the seeded data


def test_detection_recompute_is_idempotent_across_cycles():
    """Running the recompute path twice does not duplicate the same event."""
    insert_reading("Ottawa", "2026-05-31T12:00", FETCHED_AT, precipitation=0.0)
    insert_reading("Ottawa", "2026-05-31T13:00", FETCHED_AT, precipitation=2.4)

    first_detected, first_inserted, first_updated = detect_and_store()
    second_detected, second_inserted, second_updated = detect_and_store()

    assert first_inserted == 1
    assert second_inserted == 0  # same event recomputed -> upsert, not a new row
    assert count_events() == 1


# ===========================================================================
# get_events: shape, most-recent-first ordering, city filter, limit
# ===========================================================================
def _seed_events():
    # Inserted out of chronological order to prove ORDER BY started_at DESC.
    upsert_event(
        _event(city="Ottawa", event_type="precipitation", severity="light",
               started_at="2026-05-31T10:00")
    )
    upsert_event(
        _event(city="Ottawa", event_type="temp_anomaly", severity="minor",
               started_at="2026-05-31T12:00", detail={"mad_distance": 3.4})
    )
    upsert_event(
        _event(city="Toronto", event_type="precipitation", severity="heavy",
               started_at="2026-05-31T11:00")
    )


def test_get_events_shape_and_ordering():
    _seed_events()
    events = get_events()

    assert len(events) == 3
    assert set(events[0].keys()) == {
        "id",
        "city",
        "event_type",
        "severity",
        "started_at",
        "ended_at",
        "reason",
        "detail",
    }

    starts = [e["started_at"] for e in events]
    assert starts == sorted(starts, reverse=True)  # most-recent-first


def test_get_events_city_filter():
    _seed_events()
    ottawa = get_events(city="Ottawa")

    assert len(ottawa) == 2
    assert all(e["city"] == "Ottawa" for e in ottawa)


def test_get_events_limit_respected():
    _seed_events()
    limited = get_events(limit=1)

    assert len(limited) == 1
    # Still the most-recent event within the limit.
    assert limited[0]["started_at"] == "2026-05-31T12:00"
