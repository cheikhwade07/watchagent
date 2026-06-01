"""WatchAgent FastAPI application entrypoint.

Creates the FastAPI app, wires up routers, and defines the application
lifespan. On startup the SQLite schema is initialized and the in-process
weather poller is started as a background task (unless POLLER_ENABLED is
false); it is cancelled cleanly on shutdown.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging
from app.poller.scheduler import start_poller, stop_poller
from app.routers import events, health, readings
from app.storage.db import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    init_db()

    poller_task = None
    if settings.poller_enabled:
        poller_task = start_poller()
    else:
        logger.info("Poller disabled (POLLER_ENABLED is false)")

    try:
        yield
    finally:
        if poller_task is not None:
            await stop_poller(poller_task)


app = FastAPI(title="WatchAgent", lifespan=lifespan)

app.include_router(health.router)
app.include_router(readings.router)
app.include_router(events.router)
