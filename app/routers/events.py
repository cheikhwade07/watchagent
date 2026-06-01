"""Events endpoint.

Exposes GET /events to return detected weather events (e.g. temperature
spikes, sustained rain) produced by the detection pipeline, most-recent-first,
with an optional city filter and a positive-integer limit.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.storage.events import get_events

router = APIRouter()


@router.get("/events")
def list_events(
    city: str | None = Query(default=None),
    limit: int = Query(default=50, gt=0),
) -> dict:
    events = get_events(city=city, limit=limit)
    return {"events": events}
