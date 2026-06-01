"""Tests for the detection-replay skill CLI (.cursor/skills/detection-replay).

The skill script lives outside the ``app`` package, so it is loaded by file
path. Each test seeds a controlled, isolated SQLite database (via the autouse
``temp_db`` fixture) and asserts the replay output. No network is used.

The most important guarantees proven here:

  * the replay reports the SAME events the REAL pipeline (``run_detectors``)
    produces for the same readings — it reuses detection, never reimplements it;
    and
  * the replay is a DRY RUN: after running it the ``events`` table is still
    empty (nothing is persisted).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

from app.detection.detectors import PRECIP_MODERATE_MM, PRECIP_ONSET_MM
from app.detection.pipeline import run_detectors
from app.storage.events import count_events
from app.storage.models import Reading
from app.storage.repository import insert_reading

# --- Load the skill script by path (it is not an importable package) ---------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPLAY_PATH = (
    _REPO_ROOT / ".cursor" / "skills" / "detection-replay" / "scripts" / "replay.py"
)
_spec = importlib.util.spec_from_file_location("replay_skill", _REPLAY_PATH)
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)

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


def _seed_precip_event_sequence(db_path: str, city: str) -> None:
    """A dry->wet->dry run that fires exactly one precipitation event.

    precip crosses PRECIP_ONSET_MM at hour 2 (onset, moderate) and returns to
    dry at hour 4 (close), under the real PrecipitationDetector.
    """
    precip_series = [0.0, 0.0, PRECIP_MODERATE_MM + 1.0, PRECIP_MODERATE_MM + 1.0, 0.0, 0.0]
    for i, precip in enumerate(precip_series):
        _seed_reading(db_path, i, city=city, temp=15.0, wind=5.0, precip=precip)


def _assert_envelope(env: dict) -> None:
    assert set(env) == {"query", "result", "summary"}
    assert isinstance(env["query"], str) and env["query"]
    assert isinstance(env["summary"], str) and env["summary"]
    assert isinstance(env["result"], dict)


# ===========================================================================
# Core: replay reports a known event AND persists nothing (dry run)
# ===========================================================================
def test_replay_reports_known_event_and_persists_nothing(temp_db):
    _seed_precip_event_sequence(temp_db, "Rainville")
    assert count_events(db_path=temp_db) == 0  # nothing stored before the replay

    env = replay.run_replay(db_path=temp_db)
    _assert_envelope(env)

    result = env["result"]
    assert result["total_events"] == 1
    assert result["by_type"] == {"precipitation": 1}

    city = result["cities"]["Rainville"]
    assert city["readings_replayed"] == 6
    assert city["events"] == 1
    assert city["by_type"] == {"precipitation": 1}
    assert city["by_severity"] == {"moderate": 1}

    event = city["replayed_events"][0]
    assert event["city"] == "Rainville"
    assert event["event_type"] == "precipitation"
    assert event["severity"] == "moderate"
    assert event["started_at"] == _ts(2)  # onset reading
    assert event["ended_at"] == _ts(4)  # closed when it dried up
    assert "precipitation began" in event["reason"]

    assert "1 event" in env["summary"]
    assert "precipitation" in env["summary"]

    # THE DRY-RUN GUARANTEE: the replay wrote nothing to the events table.
    assert count_events(db_path=temp_db) == 0


def test_replay_matches_the_real_pipeline_exactly(temp_db):
    """Reuse proof: the replay output equals a direct run_detectors() call."""
    _seed_precip_event_sequence(temp_db, "Rainville")

    # Build the SAME Reading objects the replay loads and run the REAL pipeline
    # directly. The replay must produce identical events.
    readings = [
        Reading(
            city="Rainville",
            observed_at=_ts(i),
            fetched_at=_FETCHED_AT,
            temperature_2m=15.0,
            apparent_temperature=15.0,
            precipitation=p,
            wind_speed_10m=5.0,
            weather_code=0,
        )
        for i, p in enumerate(
            [0.0, 0.0, PRECIP_MODERATE_MM + 1.0, PRECIP_MODERATE_MM + 1.0, 0.0, 0.0]
        )
    ]
    expected = {
        (e.event_type, e.started_at, e.ended_at, e.severity)
        for e in run_detectors(readings)
    }

    replayed = {
        (e["event_type"], e["started_at"], e["ended_at"], e["severity"])
        for e in replay.run_replay(db_path=temp_db)["result"]["cities"]["Rainville"][
            "replayed_events"
        ]
    }
    assert replayed == expected
    assert len(expected) == 1  # sanity: the sequence really does fire one event


# ===========================================================================
# Options: --city and --last
# ===========================================================================
def test_replay_city_filter_scopes_to_one_city(temp_db):
    _seed_precip_event_sequence(temp_db, "Rainville")
    _seed_precip_event_sequence(temp_db, "Dryton")

    env = replay.run_replay(city="Rainville", db_path=temp_db)
    assert set(env["result"]["cities"]) == {"Rainville"}
    assert env["result"]["total_events"] == 1


def test_replay_last_n_bounds_the_input_window(temp_db):
    # A single wet reading at hour 0, then five dry hours. The onset only exists
    # in the earliest reading, so a tight --last window excludes it entirely.
    precip_series = [PRECIP_ONSET_MM + 5.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    for i, precip in enumerate(precip_series):
        _seed_reading(temp_db, i, city="Edgecity", temp=15.0, wind=5.0, precip=precip)

    full = replay.run_replay(last_n=10, db_path=temp_db)
    assert full["result"]["total_events"] == 1  # whole history -> sees the onset
    assert full["result"]["cities"]["Edgecity"]["readings_replayed"] == 6

    bounded = replay.run_replay(last_n=3, db_path=temp_db)
    # Only the 3 most recent (all-dry) readings are replayed -> no onset.
    assert bounded["result"]["total_events"] == 0
    assert bounded["result"]["cities"]["Edgecity"]["readings_replayed"] == 3

    # Still a pure dry run regardless of the window.
    assert count_events(db_path=temp_db) == 0


# ===========================================================================
# Robustness: empty / missing DB and bad CLI usage
# ===========================================================================
def test_empty_db_returns_graceful_no_data(temp_db):
    # temp_db is initialized but contains no rows.
    env = replay.run_replay(db_path=temp_db)
    _assert_envelope(env)
    assert "No data available" in env["summary"]
    assert env["result"]["total_events"] == 0
    assert env["result"]["cities"] == {}


def test_missing_db_file_returns_graceful_no_data(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    env = replay.run_replay(db_path=missing)
    _assert_envelope(env)
    assert "No data available" in env["summary"]
    assert env["result"]["total_events"] == 0


def test_cli_bad_last_argument_errors(capsys):
    code = replay.main(["--last", "0"])
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert "error" in payload
    assert payload["summary"].startswith("Error:")


def test_cli_bad_option_prints_json_error_and_nonzero(capsys):
    code = replay.main(["--totally-bogus"])
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert "error" in payload


def test_cli_runs_end_to_end_and_persists_nothing(capsys, temp_db):
    _seed_precip_event_sequence(temp_db, "Rainville")

    code = replay.main([])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert set(payload) == {"query", "result", "summary"}
    assert payload["result"]["total_events"] == 1
    assert count_events(db_path=temp_db) == 0  # CLI path persists nothing either
