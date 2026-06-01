"""Tests for the GET /events endpoint.

Events are seeded directly through the events repository (no network, no
poller) and read back over HTTP via the FastAPI TestClient.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.detection.detectors import Event
from app.main import app
from app.storage.events import upsert_event

client = TestClient(app)


def _seed():
    # Inserted out of chronological order to prove ORDER BY started_at DESC.
    upsert_event(
        Event(
            city="Ottawa",
            event_type="precipitation",
            severity="light",
            started_at="2026-05-31T10:00",
            ended_at=None,
            reason="rain began",
            detail={"precip_mm": 0.5},
        )
    )
    upsert_event(
        Event(
            city="Ottawa",
            event_type="temp_anomaly",
            severity="minor",
            started_at="2026-05-31T12:00",
            ended_at="2026-05-31T13:00",
            reason="temperature spike",
            detail={"mad_distance": 3.42, "window_size": 12},
        )
    )
    upsert_event(
        Event(
            city="Toronto",
            event_type="precipitation",
            severity="heavy",
            started_at="2026-05-31T11:00",
            ended_at=None,
            reason="rain began",
            detail=None,
        )
    )


def test_events_shape_and_ordering():
    _seed()
    response = client.get("/events")
    assert response.status_code == 200

    body = response.json()
    assert set(body.keys()) == {"events"}
    events = body["events"]
    assert len(events) == 3

    expected_fields = {
        "id",
        "city",
        "event_type",
        "severity",
        "started_at",
        "ended_at",
        "reason",
        "detail",
    }
    assert set(events[0].keys()) == expected_fields

    starts = [e["started_at"] for e in events]
    assert starts == sorted(starts, reverse=True)  # most-recent-first


def test_events_city_filter():
    _seed()
    response = client.get("/events", params={"city": "Toronto"})
    assert response.status_code == 200

    events = response.json()["events"]
    assert len(events) == 1
    assert all(e["city"] == "Toronto" for e in events)


def test_events_limit_respected():
    _seed()
    response = client.get("/events", params={"limit": 2})
    assert response.status_code == 200

    events = response.json()["events"]
    assert len(events) == 2
    # Still most-recent-first within the limit.
    assert events[0]["started_at"] >= events[1]["started_at"]


def test_events_rejects_non_positive_limit():
    response = client.get("/events", params={"limit": 0})
    assert response.status_code == 422


def test_events_rejects_limit_above_cap():
    # Above the 1000 cap is rejected; a normal/at-cap limit is accepted.
    over = client.get("/events", params={"limit": 1001})
    assert over.status_code == 422

    at_cap = client.get("/events", params={"limit": 1000})
    assert at_cap.status_code == 200

    normal = client.get("/events", params={"limit": 50})
    assert normal.status_code == 200


def test_events_detail_decoded_as_object_not_string():
    _seed()
    response = client.get("/events", params={"city": "Ottawa"})
    assert response.status_code == 200

    events = response.json()["events"]
    # Most-recent-first: the temp_anomaly (12:00) carries a detail dict.
    top = events[0]
    assert top["event_type"] == "temp_anomaly"
    assert isinstance(top["detail"], dict)  # decoded object, NOT a JSON string
    assert top["detail"] == {"mad_distance": 3.42, "window_size": 12}
