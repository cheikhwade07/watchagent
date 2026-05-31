"""WatchAgent FastAPI application entrypoint.

Creates the FastAPI app, wires up routers, and defines the application
lifespan. The in-process weather poller will be started from the lifespan
handler in a later phase.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 3+: start the in-process poller here (e.g. run_poller() as a
    # background task) and ensure graceful shutdown. Intentionally empty for
    # the walking skeleton — no background work is started yet.
    yield


app = FastAPI(title="WatchAgent", lifespan=lifespan)

app.include_router(health.router)
# Phase 2+: app.include_router(readings.router) and events.router once those
# endpoints are backed by real storage.
