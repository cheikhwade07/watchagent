"""Readings endpoint.

Exposes GET /readings to return stored weather readings, most-recent-first,
with an optional city filter and a positive-integer limit.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.storage.repository import get_readings

router = APIRouter()


@router.get("/readings")
def list_readings(
    city: str | None = Query(default=None),
    limit: int = Query(default=50, gt=0),
) -> dict:
    readings = get_readings(city=city, limit=limit)
    return {"readings": readings}
