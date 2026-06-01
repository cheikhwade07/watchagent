"""Read-only DRY-RUN replay of the CURRENT detection logic over stored readings.

This script is the runnable half of the ``detection-replay`` skill. It answers a
"what-if" question: *if we re-ran the detection pipeline, as it is coded right
now, over a chosen set of stored readings, which events WOULD fire?* It is the
threshold-tuning companion to the ``data-analysis`` skill — where
``data-analysis`` reports what the detectors DID fire (it queries the ``events``
table), this script re-runs detection in memory and shows what the current logic
WOULD produce, WITHOUT writing anything back.

It NEVER modifies the database:

  * the connection is opened with ``PRAGMA query_only = ON`` and only ``SELECT``
    statements are issued (same discipline as the ``data-analysis`` skill), and
  * the detected events are returned/printed only — ``upsert_event`` is never
    called, so the ``events`` table is untouched.

CRITICAL — it REUSES the real detectors, it does not reimplement them:

  * detection is the EXACT pipeline the live poll cycle and the historical
    backfill run — ``app.detection.pipeline.run_detectors`` — imported and
    called here, never re-coded; and
  * readings are loaded into the SAME ``app.storage.models.Reading`` objects the
    repository builds, via the SAME per-city "most recent N, oldest-first" query
    shape as ``app.storage.repository.get_readings_for_detection`` (the function
    ``detect_and_store`` feeds the live pipeline). The only differences are that
    the read is read-only and ``--last`` replaces the live cadence bound. So the
    replay produces the SAME events the real pipeline would for the same input.

Run as::

    python .cursor/skills/detection-replay/scripts/replay.py [options]

It prints a single JSON object to stdout with the same envelope the
``data-analysis`` skill uses::

    {"query": "...", "result": {...}, "summary": "one-line answer"}

The database path is resolved from the app's own configuration
(``app.config.get_settings().db_path`` / the ``WATCHAGENT_DB_PATH`` environment
variable) — it is never hardcoded here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# The app package lives at the project root, four directories above this script
# (.cursor/skills/detection-replay/scripts/replay.py). Put it on sys.path so the
# skill can run standalone from any working directory and still reuse the app's
# real config, models, and detection pipeline.
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import get_settings  # noqa: E402  (import after sys.path setup)
from app.detection.pipeline import run_detectors  # noqa: E402  (the REAL pipeline)
from app.storage.models import Reading  # noqa: E402  (the REAL reading object)

# Default per-city replay bound. Kept deliberately bounded (and independent of
# the wall clock) so a replay is deterministic and never scans an unbounded
# table. This is the skill's own knob, separate from the live cadence bound
# (``app.storage.repository.DETECTION_HISTORY_PER_CITY``); override with --last.
DEFAULT_LAST_N = 200

NO_DATA_SUMMARY = (
    "No data available — run the backfill or let the poller collect readings."
)


# ===========================================================================
# Connection + small helpers (read-only)
# ===========================================================================
def _resolve_db_path(db_path: str | None) -> str:
    """Return the explicit path if given, else the app-configured DB path."""
    return db_path if db_path is not None else get_settings().db_path


def _open_readonly(db_path: str | None) -> sqlite3.Connection | None:
    """Open the configured DB read-only, or return None if it does not exist.

    ``PRAGMA query_only = ON`` makes the connection reject any write at the
    engine level, so this replay can never modify the stored data while still
    reading a WAL-mode database correctly.
    """
    path = _resolve_db_path(db_path)
    if path != ":memory:" and not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(path)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON;")
    except sqlite3.OperationalError:
        pass
    return conn


def _distinct_cities(conn: sqlite3.Connection, city: str | None) -> list[str]:
    """Sorted distinct city names in ``readings`` (or just the requested city)."""
    if city is not None:
        return [city]
    try:
        rows = conn.execute("SELECT DISTINCT city FROM readings").fetchall()
    except sqlite3.OperationalError:
        return []  # table missing (uninitialized DB) -> treat as empty
    return sorted(r["city"] for r in rows)


def _load_city_readings(
    conn: sqlite3.Connection, city: str, last_n: int
) -> list[Reading]:
    """Load one city's most recent ``last_n`` readings, OLDEST-FIRST.

    This mirrors ``app.storage.repository.get_readings_for_detection`` exactly
    (inner ORDER BY observed_at DESC + LIMIT to take the freshest slice, then an
    outer ORDER BY observed_at ASC so the detectors receive chronological
    input), and builds the SAME ``Reading`` objects — only it reads through the
    read-only connection above. Feeding identical input to the identical
    ``run_detectors`` is what makes the replay match the live pipeline.
    """
    try:
        rows = conn.execute(
            """
            SELECT id, city, observed_at, fetched_at,
                   temperature_2m, apparent_temperature, precipitation,
                   wind_speed_10m, weather_code
            FROM (
                SELECT id, city, observed_at, fetched_at,
                       temperature_2m, apparent_temperature, precipitation,
                       wind_speed_10m, weather_code
                FROM readings
                WHERE city = ?
                ORDER BY observed_at DESC
                LIMIT ?
            )
            ORDER BY observed_at ASC
            """,
            (city, last_n),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # table missing (uninitialized DB) -> no readings to replay
    return [Reading.from_row(row) for row in rows]


def _tally(values) -> dict:
    """Count occurrences, sorted by descending count then key (deterministic)."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _event_view(event) -> dict:
    """Project a detected ``Event`` to the fields this skill reports."""
    return {
        "city": event.city,
        "event_type": event.event_type,
        "severity": event.severity,
        "started_at": event.started_at,
        "ended_at": event.ended_at,
        "reason": event.reason,
    }


def _envelope(query: str, result: dict, summary: str) -> dict:
    return {"query": query, "result": result, "summary": summary}


def _error_envelope(query: str, message: str) -> dict:
    return {"query": query, "error": message, "summary": f"Error: {message}"}


def _print(obj: dict) -> None:
    # ensure_ascii=False so the degree sign in detector reasons renders as "°C".
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _empty_result(last_n: int) -> dict:
    return {"last_n": last_n, "total_events": 0, "by_type": {}, "cities": {}}


# ===========================================================================
# Replay (the one action this skill performs)
# ===========================================================================
def run_replay(
    city: str | None = None,
    last_n: int = DEFAULT_LAST_N,
    db_path: str | None = None,
) -> dict:
    """Re-run the REAL detector pipeline over stored readings, WITHOUT persisting.

    Loads each city's most recent ``last_n`` readings (read-only, oldest-first),
    runs them through ``run_detectors`` — the exact pipeline the live poll cycle
    uses — and reports the events that WOULD fire under the current detection
    logic. Nothing is written to the database.
    """
    scope = city if city else "all cities"
    query = (
        f"Dry-run replay of current detection logic over the last {last_n} "
        f"readings/city for {scope} (would-fire, not persisted)."
    )

    conn = _open_readonly(db_path)
    if conn is None:
        return _envelope(query, _empty_result(last_n), NO_DATA_SUMMARY)
    try:
        per_city_readings: dict[str, list[Reading]] = {}
        all_readings: list[Reading] = []
        for name in _distinct_cities(conn, city):
            readings = _load_city_readings(conn, name, last_n)
            if readings:
                per_city_readings[name] = readings
                all_readings.extend(readings)

        if not all_readings:
            return _envelope(query, _empty_result(last_n), NO_DATA_SUMMARY)

        # REUSE: the identical pipeline the live poll cycle and backfill run.
        # run_detectors groups by city and time-orders internally, so a mixed
        # multi-city list is fine. Nothing here writes to the DB.
        events = run_detectors(all_readings)

        events_by_city: dict[str, list] = {}
        for event in events:
            events_by_city.setdefault(event.city, []).append(event)

        cities_result: dict[str, dict] = {}
        overall_by_type: dict[str, int] = {}
        total_events = 0
        for name in sorted(per_city_readings):
            city_events = sorted(
                events_by_city.get(name, []),
                key=lambda e: (e.started_at, e.event_type),
            )
            by_type = _tally(e.event_type for e in city_events)
            cities_result[name] = {
                "readings_replayed": len(per_city_readings[name]),
                "events": len(city_events),
                "by_type": by_type,
                "by_severity": _tally(
                    e.severity if e.severity is not None else "unrated"
                    for e in city_events
                ),
                "replayed_events": [_event_view(e) for e in city_events],
            }
            total_events += len(city_events)
            for event_type, count in by_type.items():
                overall_by_type[event_type] = (
                    overall_by_type.get(event_type, 0) + count
                )

        result = {
            "last_n": last_n,
            "total_events": total_events,
            "by_type": dict(
                sorted(overall_by_type.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "cities": cities_result,
        }
        summary = _replay_summary(total_events, overall_by_type, last_n, scope)
        return _envelope(query, result, summary)
    finally:
        conn.close()


def _replay_summary(
    total_events: int,
    overall_by_type: dict[str, int],
    last_n: int,
    scope: str,
) -> str:
    """e.g. 'Replay over last 200 readings/city produced 5 events (3 temp_anomaly, 2 precipitation).'"""
    scope_phrase = "" if scope == "all cities" else f" for {scope}"
    if total_events == 0:
        return (
            f"Replay over last {last_n} readings/city{scope_phrase} produced "
            "no events under the current detection logic."
        )
    breakdown = ", ".join(
        f"{count} {event_type}"
        for event_type, count in sorted(
            overall_by_type.items(), key=lambda kv: (-kv[1], kv[0])
        )
    )
    label = _plural(total_events, "event", "events")
    return (
        f"Replay over last {last_n} readings/city{scope_phrase} produced "
        f"{total_events} {label} ({breakdown})."
    )


# ===========================================================================
# CLI plumbing
# ===========================================================================
class _ArgError(Exception):
    """Raised instead of argparse's hard sys.exit so we can emit JSON."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):  # noqa: D401 - argparse hook
        raise _ArgError(message)


def _build_parser() -> _JsonArgumentParser:
    parser = _JsonArgumentParser(
        prog="replay.py",
        description=(
            "Dry-run replay of WatchAgent's CURRENT detection logic over stored "
            "readings (read-only; nothing is persisted)."
        ),
    )
    parser.add_argument(
        "--city",
        default=None,
        help="restrict the replay to one city (default: all cities)",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=DEFAULT_LAST_N,
        help=(
            "replay only the most recent N readings per city "
            f"(default: {DEFAULT_LAST_N})"
        ),
    )
    return parser


def _dispatch(args: argparse.Namespace) -> dict:
    if args.last <= 0:
        raise _ArgError("--last must be a positive integer")
    return run_replay(city=args.city, last_n=args.last)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        envelope = _dispatch(args)
    except _ArgError as exc:
        _print(_error_envelope(" ".join(argv) or "<no arguments>", str(exc)))
        return 2
    except Exception as exc:  # never leak a traceback to the caller
        _print(
            _error_envelope(
                " ".join(argv) or "<no arguments>", f"unexpected error: {exc}"
            )
        )
        return 1
    _print(envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
