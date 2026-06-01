"""Health check endpoint."""

from fastapi import APIRouter

from app.storage.events import count_events
from app.storage.repository import count_readings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "readings_stored": count_readings(),
        "events_stored": count_events(),
    }
