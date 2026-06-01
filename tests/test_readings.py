"""Tests for the GET /readings endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.storage.repository import insert_reading

client = TestClient(app)


def _seed():
    # Inserted out of chronological order to prove ORDER BY observed_at DESC.
    insert_reading("Ottawa", "2026-05-31T10:00", "2026-05-31T14:00:00+00:00", 18.0)
    insert_reading("Ottawa", "2026-05-31T12:00", "2026-05-31T16:00:00+00:00", 22.0)
    insert_reading("Ottawa", "2026-05-31T11:00", "2026-05-31T15:00:00+00:00", 20.0)
    insert_reading("Toronto", "2026-05-31T12:00", "2026-05-31T16:00:00+00:00", 24.0)


def test_readings_shape_and_ordering():
    _seed()
    response = client.get("/readings")
    assert response.status_code == 200

    body = response.json()
    assert set(body.keys()) == {"readings"}
    readings = body["readings"]
    assert len(readings) == 4

    expected_fields = {
        "id",
        "city",
        "observed_at",
        "fetched_at",
        "temperature_2m",
        "apparent_temperature",
        "precipitation",
        "wind_speed_10m",
        "weather_code",
    }
    assert set(readings[0].keys()) == expected_fields

    observed = [r["observed_at"] for r in readings]
    assert observed == sorted(observed, reverse=True)


def test_readings_city_filter():
    _seed()
    response = client.get("/readings", params={"city": "Toronto"})
    assert response.status_code == 200

    readings = response.json()["readings"]
    assert len(readings) == 1
    assert all(r["city"] == "Toronto" for r in readings)


def test_readings_limit_respected():
    _seed()
    response = client.get("/readings", params={"limit": 2})
    assert response.status_code == 200

    readings = response.json()["readings"]
    assert len(readings) == 2
    # Still most-recent-first within the limit.
    assert readings[0]["observed_at"] >= readings[1]["observed_at"]


def test_readings_rejects_non_positive_limit():
    response = client.get("/readings", params={"limit": 0})
    assert response.status_code == 422


def test_readings_rejects_limit_above_cap():
    # Above the 1000 cap is rejected; a normal/at-cap limit is accepted.
    over = client.get("/readings", params={"limit": 1001})
    assert over.status_code == 422

    at_cap = client.get("/readings", params={"limit": 1000})
    assert at_cap.status_code == 200

    normal = client.get("/readings", params={"limit": 50})
    assert normal.status_code == 200
