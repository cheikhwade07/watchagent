# WatchAgent

A FastAPI weather-monitoring service. An in-process poller periodically fetches
current conditions for a small set of cities from the
[Open-Meteo](https://open-meteo.com/) API, stores them in SQLite (raw
`sqlite3`, no ORM) with idempotent dedup, and exposes them over a small HTTP
API.

## Endpoints

- `GET /health` — `{"status": "ok", "readings_stored": <int>, "events_stored": <int>}`
- `GET /readings?city=<name>&limit=<n>` — stored readings, most-recent-first.

## Configuration

Settings are read from environment variables (see `.env.example`):

| Variable | Default | Notes |
| --- | --- | --- |
| `WATCHAGENT_DB_PATH` | `./data/watchagent.db` | Local runs use `./data` relative to the project. Docker compose overrides this to `/data/watchagent.db` on a persistent volume. The parent directory is created automatically. |
| `WATCHAGENT_POLL_INTERVAL_SECONDS` | `120` | How often the poller fetches each city (2 minutes). |
| `POLLER_ENABLED` | `true` | Set to `false` to disable polling. The test suite sets this to `false` so no real network calls are made. |

## Running locally

```bash
pip install ".[dev]"
uvicorn app.main:app --reload
```

## Running tests

```bash
pytest
```

Tests use an isolated temporary SQLite database and never make real network
calls (`POLLER_ENABLED=false` plus a mocked Open-Meteo client).
