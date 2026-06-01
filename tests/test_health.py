"""Tests for the /health endpoint."""

from fastapi.testclient import TestClient

from app.detection.detectors import Event
from app.main import app
from app.storage.events import upsert_event
from app.storage.repository import insert_reading

client = TestClient(app)


def test_health_returns_ok_contract_when_empty():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "readings_stored": 0,
        "events_stored": 0,
    }


def test_health_reports_real_reading_count():
    n = 3
    for i in range(n):
        insert_reading(
            city="Ottawa",
            observed_at=f"2026-05-31T1{i}:00",
            fetched_at="2026-05-31T16:00:00+00:00",
            temperature_2m=20.0 + i,
        )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "readings_stored": n,
        "events_stored": 0,
    }


def test_health_reports_real_event_count():
    readings = 2
    for i in range(readings):
        insert_reading(
            city="Ottawa",
            observed_at=f"2026-05-31T1{i}:00",
            fetched_at="2026-05-31T16:00:00+00:00",
            temperature_2m=20.0 + i,
        )

    # Seed distinct events (different started_at -> distinct identity keys).
    events = 3
    for i in range(events):
        upsert_event(
            Event(
                city="Ottawa",
                event_type="precipitation",
                severity="moderate",
                started_at=f"2026-05-31T1{i}:00",
                ended_at=None,
                reason="rain began",
                detail={"precip_mm": 2.4},
            )
        )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "readings_stored": readings,
        "events_stored": events,
    }
