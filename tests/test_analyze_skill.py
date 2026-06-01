"""Tests for the data-analysis skill CLI (.cursor/skills/data-analysis).

The skill script lives outside the ``app`` package, so it is loaded by file
path. Each test seeds a controlled, isolated SQLite database (via the autouse
``temp_db`` fixture) and asserts the analysis output. No network is used.

The near-miss tests are the most important: they prove the diagnostic flags a
constructed 2-of-3 frontal window, but NOT a real 3-of-3 frontal nor a 1-of-3.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from app.detection.detectors import (
    FRONTAL_TEMP_DROP_C,
    FRONTAL_WIND_RISE_KMH,
    FRONTAL_WINDOW,
    Event,
)
from app.storage.events import upsert_event
from app.storage.repository import insert_reading

# --- Load the skill script by path (it is not an importable package) ---------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ANALYZE_PATH = (
    _REPO_ROOT / ".cursor" / "skills" / "data-analysis" / "scripts" / "analyze.py"
)
_spec = importlib.util.spec_from_file_location("analyze_skill", _ANALYZE_PATH)
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)

_BASE_TIME = datetime(2026, 5, 31, 0, 0)
_FETCHED_AT = "2026-05-31T00:00:00+00:00"


def _ts(index: int) -> str:
    """Hourly ISO timestamp matching the stored format (lexically sortable)."""
    return (_BASE_TIME + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")


def _seed_reading(
    db_path: str,
    index: int,
    *,
    city: str,
    temp: float | None = None,
    wind: float | None = None,
    precip: float | None = None,
) -> None:
    insert_reading(
        city=city,
        observed_at=_ts(index),
        fetched_at=_FETCHED_AT,
        temperature_2m=temp,
        apparent_temperature=temp,
        precipitation=precip,
        wind_speed_10m=wind,
        weather_code=0,
        db_path=db_path,
    )


def _seed_event(
    db_path: str,
    *,
    city: str,
    event_type: str,
    severity: str | None,
    index: int,
) -> None:
    upsert_event(
        Event(
            city=city,
            event_type=event_type,
            severity=severity,
            started_at=_ts(index),
            ended_at=None,
            reason="seeded test event",
        ),
        db_path=db_path,
    )


def _assert_envelope(env: dict) -> None:
    assert set(env) == {"query", "result", "summary"}
    assert isinstance(env["query"], str) and env["query"]
    assert isinstance(env["summary"], str) and env["summary"]
    assert isinstance(env["result"], dict)


# ===========================================================================
# summary
# ===========================================================================
def test_summary_counts_events_and_readings(temp_db):
    for i in range(5):
        _seed_reading(temp_db, i, city="Testville", temp=15.0)
    _seed_event(temp_db, city="Testville", event_type="precipitation", severity="light", index=0)
    _seed_event(temp_db, city="Testville", event_type="precipitation", severity="moderate", index=1)
    _seed_event(temp_db, city="Testville", event_type="temp_anomaly", severity="minor", index=2)
    # A frontal_passage event has no severity -> reported as "unrated".
    _seed_event(temp_db, city="Testville", event_type="frontal_passage", severity=None, index=3)

    env = analyze.run_summary(db_path=temp_db)
    _assert_envelope(env)

    result = env["result"]
    assert result["total_readings"] == 5
    assert result["total_events"] == 4

    city = result["cities"]["Testville"]
    assert city["readings"] == 5
    assert city["events"] == 4
    assert city["by_type"] == {"precipitation": 2, "temp_anomaly": 1, "frontal_passage": 1}
    assert city["by_severity"] == {"light": 1, "minor": 1, "moderate": 1, "unrated": 1}

    assert "Testville" in env["summary"]
    assert "4 events" in env["summary"]


def test_summary_city_filter_scopes_results(temp_db):
    _seed_reading(temp_db, 0, city="Alpha", temp=10.0)
    _seed_reading(temp_db, 0, city="Beta", temp=10.0)
    _seed_event(temp_db, city="Alpha", event_type="precipitation", severity="light", index=0)
    _seed_event(temp_db, city="Beta", event_type="precipitation", severity="light", index=0)

    env = analyze.run_summary(city="Alpha", db_path=temp_db)

    assert env["result"]["total_readings"] == 1
    assert env["result"]["total_events"] == 1
    assert set(env["result"]["cities"]) == {"Alpha"}


# ===========================================================================
# trends
# ===========================================================================
def test_trends_computes_stats_over_window(temp_db):
    # Recent cluster (hours 48-50) plus an OLD cluster (hours 0-2) that a 24h
    # window must exclude.
    for i in (0, 1, 2):
        _seed_reading(temp_db, i, city="Trendville", temp=100.0)
    for i, t in ((48, 10.0), (49, 20.0), (50, 30.0)):
        _seed_reading(temp_db, i, city="Trendville", temp=t)

    env = analyze.run_trends(city="Trendville", hours=24, db_path=temp_db)
    _assert_envelope(env)

    stats = env["result"]["cities"]["Trendville"]
    assert env["result"]["window_hours"] == 24
    assert stats["count"] == 3  # only the recent cluster is in the window
    assert stats["min"] == 10.0
    assert stats["max"] == 30.0  # the old 100.0 readings are excluded
    assert stats["mean"] == 20.0
    assert stats["median"] == 20.0
    assert stats["window_start"] == _ts(48)
    assert stats["window_end"] == _ts(50)


def test_trends_default_window_includes_all_recent(temp_db):
    for i, t in enumerate([12.0, 14.0, 16.0]):
        _seed_reading(temp_db, i, city="Ottawa", temp=t)

    env = analyze.run_trends(db_path=temp_db)
    stats = env["result"]["cities"]["Ottawa"]
    assert stats["count"] == 3
    assert stats["mean"] == 14.0


# ===========================================================================
# compare
# ===========================================================================
def test_compare_identifies_warmest_and_coolest(temp_db):
    _seed_reading(temp_db, 0, city="Toronto", temp=5.0)
    _seed_reading(temp_db, 1, city="Toronto", temp=19.0)  # most recent
    _seed_reading(temp_db, 0, city="Vancouver", temp=13.3)

    env = analyze.run_compare(db_path=temp_db)
    _assert_envelope(env)

    result = env["result"]
    assert result["cities"]["Toronto"]["latest"]["temperature_2m"] == 19.0
    assert result["warmest_now"] == {"city": "Toronto", "temperature_2m": 19.0}
    assert result["coolest_now"] == {"city": "Vancouver", "temperature_2m": 13.3}
    assert "Warmest now: Toronto" in env["summary"]


# ===========================================================================
# near-miss (most important) — uses the REAL frontal constants
# ===========================================================================
def _seed_window(db_path, city, temps, winds, precs) -> None:
    """Seed exactly FRONTAL_WINDOW readings forming a single rolling window."""
    assert len(temps) == len(winds) == len(precs) == FRONTAL_WINDOW
    for i in range(FRONTAL_WINDOW):
        _seed_reading(db_path, i, city=city, temp=temps[i], wind=winds[i], precip=precs[i])


def test_near_miss_flags_two_of_three_but_not_three_or_one(temp_db):
    # Sanity: thresholds are the real detector values we build windows against.
    assert (FRONTAL_WINDOW, FRONTAL_TEMP_DROP_C, FRONTAL_WIND_RISE_KMH) == (3, 5.0, 15.0)

    # 2-of-3 NEAR-MISS: temp drop (20->11=9) + wind spike (10->27=17), NO precip.
    _seed_window(temp_db, "NearMiss", [20.0, 16.0, 11.0], [10.0, 20.0, 27.0], [0.0, 0.0, 0.0])
    # 3-of-3 REAL FRONTAL: temp drop + wind spike + precip onset -> NOT a near-miss.
    _seed_window(temp_db, "FullFrontal", [20.0, 16.0, 11.0], [10.0, 20.0, 27.0], [0.0, 0.0, 1.5])
    # 1-of-3: only temp drop (wind flat, dry) -> NOT a near-miss.
    _seed_window(temp_db, "OneSignal", [20.0, 16.0, 11.0], [10.0, 10.0, 10.0], [0.0, 0.0, 0.0])

    env = analyze.run_near_miss(db_path=temp_db)
    _assert_envelope(env)
    cities = env["result"]["cities"]

    # NearMiss: exactly one near-miss, missing the precip-onset signal.
    near = cities["NearMiss"]
    assert near["count"] == 1
    nm = near["near_misses"][0]
    assert nm["missing"] == "precip onset"
    assert set(nm["present"]) == {"temp drop", "wind spike"}
    assert nm["window_start"] == _ts(0)
    assert nm["window_end"] == _ts(2)
    assert near["missing_tally"] == {"precip onset": 1}

    # FullFrontal (real 3-of-3) and OneSignal must NOT be near-misses.
    assert cities["FullFrontal"]["count"] == 0
    assert cities["OneSignal"]["count"] == 0

    assert env["result"]["total_near_misses"] == 1
    assert "NearMiss: 1 near-miss" in env["summary"]


def test_near_miss_city_filter(temp_db):
    _seed_window(temp_db, "NearMiss", [20.0, 16.0, 11.0], [10.0, 20.0, 27.0], [0.0, 0.0, 0.0])
    _seed_window(temp_db, "FullFrontal", [20.0, 16.0, 11.0], [10.0, 20.0, 27.0], [0.0, 0.0, 1.5])

    env = analyze.run_near_miss(city="NearMiss", db_path=temp_db)
    assert set(env["result"]["cities"]) == {"NearMiss"}
    assert env["result"]["total_near_misses"] == 1


# ===========================================================================
# Robustness: empty / missing DB and bad CLI usage
# ===========================================================================
def test_empty_db_returns_graceful_no_data(temp_db):
    # temp_db is initialized but contains no rows.
    for run in (analyze.run_summary, analyze.run_trends, analyze.run_compare, analyze.run_near_miss):
        env = run(db_path=temp_db)
        _assert_envelope(env)
        assert "No data available" in env["summary"]


def test_missing_db_file_returns_graceful_no_data(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    env = analyze.run_summary(db_path=missing)
    _assert_envelope(env)
    assert "No data available" in env["summary"]
    assert env["result"]["total_readings"] == 0


def test_cli_bad_subcommand_prints_json_error_and_nonzero(capsys):
    import json

    code = analyze.main(["totally-bogus"])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert code != 0
    assert "error" in payload
    assert payload["summary"].startswith("Error:")


def test_cli_bad_hours_argument_errors(capsys):
    import json

    code = analyze.main(["trends", "--hours", "-5"])
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert "error" in payload


def test_cli_summary_runs_end_to_end(capsys, temp_db):
    import json

    code = analyze.main(["summary"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert set(payload) == {"query", "result", "summary"}
