"""Shared pytest fixtures.

Ensures the poller never starts during tests (no real Open-Meteo calls) and
gives every test an isolated temporary SQLite database.
"""

from __future__ import annotations

import os

# Disable the poller for the whole test session BEFORE the app is imported, so
# the lifespan never launches a background poll loop and no real network calls
# can happen.
os.environ["POLLER_ENABLED"] = "false"

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point the app at a fresh SQLite file per test and create the schema."""
    from app.storage.db import init_db

    db_file = tmp_path / "watchagent_test.db"
    monkeypatch.setenv("WATCHAGENT_DB_PATH", str(db_file))
    init_db()
    yield str(db_file)
