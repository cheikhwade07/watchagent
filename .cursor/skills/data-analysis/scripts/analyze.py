"""Read-only analysis CLI over WatchAgent's stored weather data.

This script is the runnable half of the ``data-analysis`` skill. It answers
questions about the SAME SQLite database the app writes to, using raw
``sqlite3`` with parameterized queries (consistent with the
``storage-sql-discipline`` rule). It NEVER modifies the database: the connection
is opened with ``PRAGMA query_only = ON``, and only ``SELECT`` statements are
issued.

Run as::

    python .cursor/skills/data-analysis/scripts/analyze.py <subcommand> [options]

Subcommands: ``summary``, ``trends``, ``compare``, ``near-miss``. Every
subcommand prints a single JSON object to stdout with a consistent envelope::

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
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

# The app package lives at the project root, four directories above this script
# (.cursor/skills/data-analysis/scripts/analyze.py). Put it on sys.path so the
# skill can run standalone from any working directory and still reuse the app's
# real config and detection constants.
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import get_settings  # noqa: E402  (import after sys.path setup)
from app.detection.detectors import (  # noqa: E402  (reuse the REAL constants)
    FRONTAL_TEMP_DROP_C,
    FRONTAL_WIND_RISE_KMH,
    FRONTAL_WINDOW,
    PRECIP_CLEAR_MM,
    PRECIP_ONSET_MM,
)

# Default analysis windows (hours). Overridable per-invocation where noted.
DEFAULT_TRENDS_HOURS = 72
COMPARE_RECENT_HOURS = 24

# Canonical labels for the three frontal-passage signals, reused in the
# near-miss report and summary line.
SIG_TEMP = "temp drop"
SIG_WIND = "wind spike"
SIG_PRECIP = "precip onset"
FRONTAL_SIGNALS = (SIG_TEMP, SIG_WIND, SIG_PRECIP)

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
    engine level, so this analysis can never modify the stored data while still
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


def _count(conn: sqlite3.Connection, table: str, city: str | None) -> int:
    """COUNT(*) for ``readings`` or ``events`` with an optional city filter.

    ``table`` is one of two trusted in-code literals (never user input); the
    city value is always bound as a parameter.
    """
    query = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - literal table name
    params: tuple = ()
    if city is not None:
        query += " WHERE city = ?"
        params = (city,)
    try:
        row = conn.execute(query, params).fetchone()
        return int(row["n"])
    except sqlite3.OperationalError:
        return 0  # table missing (uninitialized DB) -> treat as empty


def _distinct_cities(conn: sqlite3.Connection, city: str | None) -> list[str]:
    """Sorted distinct city names across readings and events (or just ``city``)."""
    if city is not None:
        return [city]
    cities: set[str] = set()
    for table in ("readings", "events"):
        try:
            rows = conn.execute(
                f"SELECT DISTINCT city FROM {table}"  # noqa: S608 - literal table
            ).fetchall()
            cities.update(r["city"] for r in rows)
        except sqlite3.OperationalError:
            continue
    return sorted(cities)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO ``observed_at`` string into a datetime, or None if invalid."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _round(value: float | None, ndigits: int = 2) -> float | None:
    return None if value is None else round(value, ndigits)


def _fmt_temp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _envelope(query: str, result: dict, summary: str) -> dict:
    return {"query": query, "result": result, "summary": summary}


def _error_envelope(query: str, message: str) -> dict:
    return {"query": query, "error": message, "summary": f"Error: {message}"}


def _print(obj: dict) -> None:
    # ensure_ascii=False so the degree sign in summaries renders as "°C".
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


# ===========================================================================
# Subcommand 1: summary
# ===========================================================================
def run_summary(city: str | None = None, db_path: str | None = None) -> dict:
    """Per-city event counts (by type and severity) plus reading/event totals."""
    scope = city if city else "all cities"
    query = f"Event and reading summary for {scope}."

    conn = _open_readonly(db_path)
    if conn is None:
        return _envelope(query, _empty_summary_result(city), NO_DATA_SUMMARY)
    try:
        total_readings = _count(conn, "readings", city)
        total_events = _count(conn, "events", city)
        if total_readings == 0 and total_events == 0:
            return _envelope(query, _empty_summary_result(city), NO_DATA_SUMMARY)

        cities: dict[str, dict] = {}
        for name in _distinct_cities(conn, city):
            cities[name] = {
                "readings": _count(conn, "readings", name),
                "events": _count(conn, "events", name),
                "by_type": _group_counts(conn, "event_type", name),
                "by_severity": _group_counts(conn, "severity", name),
            }

        result = {
            "total_readings": total_readings,
            "total_events": total_events,
            "cities": cities,
        }
        summary = _summary_line(cities)
        return _envelope(query, result, summary)
    finally:
        conn.close()


def _empty_summary_result(city: str | None) -> dict:
    return {"total_readings": 0, "total_events": 0, "cities": {}}


def _group_counts(conn: sqlite3.Connection, column: str, city: str) -> dict:
    """Return ``{value: count}`` for events of one city grouped by ``column``.

    ``column`` is a trusted literal (``event_type`` or ``severity``); the city
    value is bound as a parameter. A NULL severity is reported as ``"unrated"``.
    """
    try:
        rows = conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n "  # noqa: S608 - literal column
            "FROM events WHERE city = ? GROUP BY k",
            (city,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        key = row["k"] if row["k"] is not None else "unrated"
        counts[key] = int(row["n"])
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _summary_line(cities: dict[str, dict]) -> str:
    """e.g. 'Vancouver: 8 events (5 precipitation, 3 temp_anomaly); 210 readings.'"""
    phrases = []
    for name, data in cities.items():
        by_type = data["by_type"]
        breakdown = ", ".join(f"{n} {t}" for t, n in by_type.items())
        breakdown = f" ({breakdown})" if breakdown else ""
        phrases.append(
            f"{name}: {data['events']} events{breakdown}; "
            f"{data['readings']} readings"
        )
    return "; ".join(phrases) + "." if phrases else NO_DATA_SUMMARY


# ===========================================================================
# Subcommand 2: trends
# ===========================================================================
def run_trends(
    city: str | None = None,
    hours: int = DEFAULT_TRENDS_HOURS,
    db_path: str | None = None,
) -> dict:
    """Per-city temperature stats (min/max/mean/median) over the last N hours.

    The window is anchored to each city's most recent reading and extends back
    ``hours`` hours, so the analysis produces results for historical/backfilled
    data as well as live data.
    """
    scope = city if city else "all cities"
    query = f"Temperature trend for {scope} over the last {hours}h."

    conn = _open_readonly(db_path)
    if conn is None or _count(conn, "readings", city) == 0:
        if conn is not None:
            conn.close()
        return _envelope(query, {"window_hours": hours, "cities": {}}, NO_DATA_SUMMARY)
    try:
        cities: dict[str, dict] = {}
        for name in _distinct_cities(conn, city):
            stats = _temp_window_stats(conn, name, hours)
            if stats is not None:
                cities[name] = stats

        result = {"window_hours": hours, "cities": cities}
        summary = _trends_summary(cities, hours)
        return _envelope(query, result, summary)
    finally:
        conn.close()


def _temp_window_stats(
    conn: sqlite3.Connection, city: str, hours: int
) -> dict | None:
    """Temperature stats over the last ``hours`` for one city, or None if no data."""
    rows = conn.execute(
        "SELECT observed_at, temperature_2m FROM readings "
        "WHERE city = ? ORDER BY observed_at ASC",
        (city,),
    ).fetchall()

    parsed = [
        (dt, row["observed_at"], row["temperature_2m"])
        for row in rows
        if (dt := _parse_dt(row["observed_at"])) is not None
    ]
    if not parsed:
        return None

    anchor = max(dt for dt, _, _ in parsed)
    cutoff = anchor - timedelta(hours=hours)
    window = [(dt, raw, temp) for dt, raw, temp in parsed if dt >= cutoff]
    temps = [temp for _, _, temp in window if temp is not None]

    if not temps:
        return {
            "count": 0,
            "window_start": None,
            "window_end": None,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
        }
    return {
        "count": len(temps),
        "window_start": window[0][1],
        "window_end": window[-1][1],
        "min": _round(min(temps)),
        "max": _round(max(temps)),
        "mean": _round(mean(temps)),
        "median": _round(median(temps)),
    }


def _trends_summary(cities: dict[str, dict], hours: int) -> str:
    """e.g. 'Ottawa last 72h: mean 14.2°C, min 6.1, max 23.4.'"""
    phrases = []
    for name, s in cities.items():
        if s["count"] == 0:
            phrases.append(f"{name} last {hours}h: no temperature readings")
            continue
        phrases.append(
            f"{name} last {hours}h: mean {_fmt_temp(s['mean'])}°C, "
            f"min {_fmt_temp(s['min'])}, max {_fmt_temp(s['max'])}"
        )
    return "; ".join(phrases) + "." if phrases else NO_DATA_SUMMARY


# ===========================================================================
# Subcommand 3: compare
# ===========================================================================
def run_compare(db_path: str | None = None) -> dict:
    """Side-by-side: most recent reading per city plus a recent temperature mean."""
    query = "Cross-city comparison of current conditions and recent means."

    conn = _open_readonly(db_path)
    if conn is None or _count(conn, "readings", None) == 0:
        if conn is not None:
            conn.close()
        empty = {
            "recent_mean_hours": COMPARE_RECENT_HOURS,
            "cities": {},
            "warmest_now": None,
            "coolest_now": None,
        }
        return _envelope(query, empty, NO_DATA_SUMMARY)
    try:
        cities: dict[str, dict] = {}
        now_temps: list[tuple[str, float]] = []
        for name in _distinct_cities(conn, None):
            latest = _latest_reading(conn, name)
            stats = _temp_window_stats(conn, name, COMPARE_RECENT_HOURS)
            recent_mean = stats["mean"] if stats else None
            cities[name] = {"latest": latest, "recent_mean_temp": recent_mean}
            if latest is not None and latest["temperature_2m"] is not None:
                now_temps.append((name, latest["temperature_2m"]))

        warmest = max(now_temps, key=lambda ct: ct[1]) if now_temps else None
        coolest = min(now_temps, key=lambda ct: ct[1]) if now_temps else None

        result = {
            "recent_mean_hours": COMPARE_RECENT_HOURS,
            "cities": cities,
            "warmest_now": _city_temp(warmest),
            "coolest_now": _city_temp(coolest),
        }
        return _envelope(query, result, _compare_summary(warmest, coolest))
    finally:
        conn.close()


def _latest_reading(conn: sqlite3.Connection, city: str) -> dict | None:
    row = conn.execute(
        "SELECT observed_at, temperature_2m, apparent_temperature, "
        "precipitation, wind_speed_10m, weather_code "
        "FROM readings WHERE city = ? ORDER BY observed_at DESC LIMIT 1",
        (city,),
    ).fetchone()
    return dict(row) if row is not None else None


def _city_temp(item: tuple[str, float] | None) -> dict | None:
    if item is None:
        return None
    return {"city": item[0], "temperature_2m": _round(item[1])}


def _compare_summary(
    warmest: tuple[str, float] | None, coolest: tuple[str, float] | None
) -> str:
    """e.g. 'Warmest now: Toronto 19.0°C; coolest: Vancouver 13.3°C.'"""
    if warmest is None:
        return "No current temperatures available to compare."
    if warmest[0] == coolest[0]:
        return f"Only {warmest[0]} has a current temperature: {_fmt_temp(warmest[1])}°C."
    return (
        f"Warmest now: {warmest[0]} {_fmt_temp(warmest[1])}°C; "
        f"coolest: {coolest[0]} {_fmt_temp(coolest[1])}°C."
    )


# ===========================================================================
# Subcommand 4: near-miss (frontal-passage diagnostic)
# ===========================================================================
def run_near_miss(city: str | None = None, db_path: str | None = None) -> dict:
    """Find windows where 2 of the 3 frontal signals crossed but not all 3.

    Reuses the REAL frontal-passage windowing and thresholds from
    ``app.detection`` (``FRONTAL_WINDOW``, ``FRONTAL_TEMP_DROP_C``,
    ``FRONTAL_WIND_RISE_KMH``, ``PRECIP_ONSET_MM``/``PRECIP_CLEAR_MM``). A
    near-miss is a rolling window of ``FRONTAL_WINDOW`` readings where exactly
    two signals cross their thresholds — it nearly fired a ``frontal_passage``
    but one signal was missing. This exposes whether the compound threshold is
    too strict for calm data.
    """
    scope = city if city else "all cities"
    query = f"Frontal-passage near-miss diagnostic for {scope}."
    thresholds = {
        "window": FRONTAL_WINDOW,
        "temp_drop_c": FRONTAL_TEMP_DROP_C,
        "wind_rise_kmh": FRONTAL_WIND_RISE_KMH,
        "precip_onset_mm": PRECIP_ONSET_MM,
    }

    conn = _open_readonly(db_path)
    if conn is None or _count(conn, "readings", city) == 0:
        if conn is not None:
            conn.close()
        empty = {"frontal_thresholds": thresholds, "total_near_misses": 0, "cities": {}}
        return _envelope(query, empty, NO_DATA_SUMMARY)
    try:
        cities: dict[str, dict] = {}
        total = 0
        for name in _distinct_cities(conn, city):
            near_misses = _scan_near_misses(conn, name)
            tally: dict[str, int] = {}
            for nm in near_misses:
                tally[nm["missing"]] = tally.get(nm["missing"], 0) + 1
            cities[name] = {
                "count": len(near_misses),
                "missing_tally": dict(
                    sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
                ),
                "near_misses": near_misses,
            }
            total += len(near_misses)

        result = {
            "frontal_thresholds": thresholds,
            "total_near_misses": total,
            "cities": cities,
        }
        return _envelope(query, result, _near_miss_summary(cities, total))
    finally:
        conn.close()


def _scan_near_misses(conn: sqlite3.Connection, city: str) -> list[dict]:
    """Rolling-window scan of one city's chronological readings for near-misses."""
    rows = conn.execute(
        "SELECT observed_at, temperature_2m, wind_speed_10m, precipitation "
        "FROM readings WHERE city = ? ORDER BY observed_at ASC",
        (city,),
    ).fetchall()

    near_misses: list[dict] = []
    for index in range(len(rows)):
        if index + 1 < FRONTAL_WINDOW:
            continue  # not enough readings for a full window yet
        window = rows[index - FRONTAL_WINDOW + 1 : index + 1]
        present, temp_drop, wind_rise = _evaluate_window(window)
        if len(present) != 2:  # need exactly 2 of 3 (>=2 but not all 3)
            continue
        missing = next(s for s in FRONTAL_SIGNALS if s not in present)
        near_misses.append(
            {
                "window_start": window[0]["observed_at"],
                "window_end": window[-1]["observed_at"],
                "present": [s for s in FRONTAL_SIGNALS if s in present],
                "missing": missing,
                "temp_drop_c": _round(temp_drop, 1),
                "wind_rise_kmh": _round(wind_rise, 1),
            }
        )
    return near_misses


def _evaluate_window(
    window: list,
) -> tuple[set[str], float | None, float | None]:
    """Return (signals that crossed, net temp drop, net wind rise) for a window.

    Mirrors ``FrontalPassageDetector._signature`` exactly: net temperature fall
    across the window, net wind-speed rise across the window, and a dry->wet
    precipitation onset somewhere inside it. A signal whose required values are
    missing is treated as absent.
    """
    temps = [r["temperature_2m"] for r in window]
    winds = [r["wind_speed_10m"] for r in window]
    precs = [r["precipitation"] for r in window]

    present: set[str] = set()

    temp_drop = None
    if temps[0] is not None and temps[-1] is not None:
        temp_drop = temps[0] - temps[-1]
        if temp_drop >= FRONTAL_TEMP_DROP_C:
            present.add(SIG_TEMP)

    wind_rise = None
    if winds[0] is not None and winds[-1] is not None:
        wind_rise = winds[-1] - winds[0]
        if wind_rise >= FRONTAL_WIND_RISE_KMH:
            present.add(SIG_WIND)

    precip_onset = any(
        precs[k - 1] is not None
        and precs[k] is not None
        and precs[k - 1] <= PRECIP_CLEAR_MM
        and precs[k] > PRECIP_ONSET_MM
        for k in range(1, len(window))
    )
    if precip_onset:
        present.add(SIG_PRECIP)

    return present, temp_drop, wind_rise


def _near_miss_summary(cities: dict[str, dict], total: int) -> str:
    """e.g. 'Vancouver: 4 near-misses (missing: precip onset in 3, wind spike in 1).'"""
    if total == 0:
        return (
            "No frontal near-misses found — the compound threshold was not "
            "nearly met anywhere in the data."
        )
    phrases = []
    for name, data in cities.items():
        if data["count"] == 0:
            continue
        missing = ", ".join(
            f"{sig} in {n}" for sig, n in data["missing_tally"].items()
        )
        label = _plural(data["count"], "near-miss", "near-misses")
        phrases.append(f"{name}: {data['count']} {label} (missing: {missing})")
    return "; ".join(phrases) + "."


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
        prog="analyze.py",
        description="Read-only analysis of WatchAgent's stored weather data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_summary = sub.add_parser("summary", help="event/reading counts per city")
    p_summary.add_argument("--city", default=None)

    p_trends = sub.add_parser("trends", help="temperature stats over a window")
    p_trends.add_argument("--city", default=None)
    p_trends.add_argument("--hours", type=int, default=DEFAULT_TRENDS_HOURS)

    sub.add_parser("compare", help="cross-city current conditions")

    p_near = sub.add_parser("near-miss", help="frontal-passage near-miss diagnostic")
    p_near.add_argument("--city", default=None)

    return parser


def _dispatch(args: argparse.Namespace) -> dict:
    if args.command == "summary":
        return run_summary(city=args.city)
    if args.command == "trends":
        if args.hours <= 0:
            raise _ArgError("--hours must be a positive integer")
        return run_trends(city=args.city, hours=args.hours)
    if args.command == "compare":
        return run_compare()
    if args.command == "near-miss":
        return run_near_miss(city=args.city)
    raise _ArgError(f"unknown command: {args.command}")  # unreachable via argparse


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
        _print(_error_envelope(" ".join(argv) or "<no arguments>", f"unexpected error: {exc}"))
        return 1
    _print(envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
