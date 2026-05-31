"""Health check endpoint."""

from fastapi import APIRouter

from app.storage.repository import count_readings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    # events_stored is 0 for now: the events table does not exist yet and is
    # wired to a real count in a later phase (detection pipeline).
    return {
        "status": "ok",
        "readings_stored": count_readings(),
        "events_stored": 0,
    }
