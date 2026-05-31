"""Health check endpoint."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    # Phase 2+: readings_stored and events_stored will be wired to real counts
    # from the storage layer. Hardcoded to 0 for the walking skeleton.
    return {"status": "ok", "readings_stored": 0, "events_stored": 0}
