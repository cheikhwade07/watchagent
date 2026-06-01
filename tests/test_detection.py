"""Synthetic, deterministic unit tests for the event-detection logic.

Every test builds a controlled reading sequence by hand and asserts EXACTLY
what fires and what does not. There are no network calls and no database
access — detectors are pure functions (readings in, events out).

Cadence: readings are spaced one hour apart (matching the detectors'
``READINGS_PER_DAY`` assumption), produced by :func:`_ts`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.detection.detectors import (
    APPARENT_GAP_C,
    TEMP_MAX_OPEN_READINGS,
    detect_apparent_gap,
    detect_frontal_passage,
    detect_precipitation,
    detect_temperature_anomaly,
)
from app.detection.pipeline import run_detectors
from app.storage.models import Reading

# A stable, low-variability baseline: alternating 17/19 °C has median 18 °C and
# a scaled MAD of exactly 1.0 * 1.4826, which makes the expected MAD distances
# below easy to reason about by hand.
_BASELINE = [17.0, 19.0] * 6  # 12 readings, more than the 10-reading cold-start guard

_BASE_TIME = datetime(2026, 5, 31, 0, 0)


def _ts(index: int) -> str:
    """Hourly ISO timestamp; lexically sortable and chronological."""
    return (_BASE_TIME + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")


def _reading(
    index: int,
    *,
    city: str = "Ottawa",
    temp: float | None = None,
    apparent: float | None = None,
    precip: float | None = None,
    wind: float | None = None,
) -> Reading:
    """Build one Reading at hour ``index`` (fetched_at is irrelevant to detectors)."""
    return Reading(
        city=city,
        observed_at=_ts(index),
        fetched_at="2026-05-31T00:00:00+00:00",
        temperature_2m=temp,
        apparent_temperature=apparent,
        precipitation=precip,
        wind_speed_10m=wind,
        weather_code=0,
    )


def _temp_seq(temps: list[float], *, city: str = "Ottawa") -> list[Reading]:
    """A dry, calm sequence carrying only temperatures (apparent tracks temp)."""
    return [
        _reading(i, city=city, temp=t, apparent=t - 1.0, precip=0.0, wind=10.0)
        for i, t in enumerate(temps)
    ]


# ===========================================================================
# Detector 1: temperature MAD anomaly
# ===========================================================================
def test_temp_anomaly_minor_severity():
    """A spike ~3.4 MAD above the baseline fires a single minor anomaly."""
    events = detect_temperature_anomaly(_temp_seq(_BASELINE + [23.0]))

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "temp_anomaly"
    assert event.severity == "minor"
    assert event.started_at == _ts(12)
    assert event.ended_at is None  # still ongoing at end of sequence
    assert "MAD" in event.reason


def test_temp_anomaly_moderate_severity():
    """A spike ~5.4 MAD above the baseline fires a moderate anomaly."""
    events = detect_temperature_anomaly(_temp_seq(_BASELINE + [26.0]))

    assert len(events) == 1
    assert events[0].severity == "moderate"
    assert events[0].detail["baseline_median"] == 18.0


def test_temp_anomaly_major_severity():
    """A spike ~8.8 MAD above the baseline fires a major anomaly."""
    events = detect_temperature_anomaly(_temp_seq(_BASELINE + [31.0]))

    assert len(events) == 1
    assert events[0].severity == "major"


def test_temp_stable_sequence_fires_nothing():
    """A purely stable baseline (no spike) produces no events at all."""
    events = detect_temperature_anomaly(_temp_seq([17.0, 19.0] * 7))

    assert events == []


def test_temp_cold_start_guard_suppresses_firing():
    """A huge spike with too little baseline history must NOT fire."""
    # Only 8 baseline readings precede the spike -> below the 10-reading guard.
    events = detect_temperature_anomaly(_temp_seq([17.0, 19.0] * 4 + [31.0]))

    assert events == []


def test_temp_anomaly_fires_once_then_closes():
    """State model: onset fires once, persistence does not re-fire, then closes."""
    # 12 baseline, then three hot readings (ongoing), then back to normal.
    temps = _BASELINE + [26.0, 26.0, 26.0] + [18.0, 18.0]
    events = detect_temperature_anomaly(_temp_seq(temps))

    assert len(events) == 1  # exactly one event despite three hot readings
    event = events[0]
    assert event.severity == "moderate"
    assert event.started_at == _ts(12)  # first hot reading
    assert event.ended_at == _ts(15)  # first normal reading after the spike


def test_temp_anomaly_sustained_shift_force_closes_at_max_duration():
    """A regime shift that never returns must NOT stay open forever.

    The frozen baseline (median 18) would keep the event open indefinitely while
    the temperature stays elevated, so the max-duration fallback must force-close
    it exactly TEMP_MAX_OPEN_READINGS readings after onset.
    """
    # 12 baseline readings, then a sustained elevated level that never returns.
    temps = _BASELINE + [30.0, 32.0] * 15  # 30 hot readings (indices 12..41)
    events = detect_temperature_anomaly(_temp_seq(temps))

    assert len(events) == 1
    event = events[0]
    assert event.started_at == _ts(12)  # onset at the first hot reading
    assert event.ended_at is not None  # it resolves rather than hanging open
    # Closed exactly at the max-duration limit: onset + TEMP_MAX_OPEN_READINGS.
    assert event.ended_at == _ts(12 + TEMP_MAX_OPEN_READINGS)


def test_temp_anomaly_rebaselines_after_max_duration_close():
    """After a force-close, the elevated level becomes the new normal.

    Given plenty of subsequent readings at the new level (long enough for the
    old baseline to age out of the rolling window entirely), the now-normal
    elevated temperature must NOT keep re-firing.
    """
    # 12 baseline, then 64 hot readings (indices 12..75) — far beyond both the
    # max-duration limit and the 48-reading baseline window.
    temps = _BASELINE + [30.0, 32.0] * 32
    events = detect_temperature_anomaly(_temp_seq(temps))

    # Exactly one event total: the original anomaly, force-closed at the limit.
    assert len(events) == 1
    closed_at = _ts(12 + TEMP_MAX_OPEN_READINGS)
    assert events[0].ended_at == closed_at
    # Nothing re-fires once the elevated level has become the new baseline.
    assert all(event.started_at <= closed_at for event in events)


# ===========================================================================
# Detector 2: precipitation event
# ===========================================================================
def test_precip_onset_fires_once_persists_then_closes():
    """Onset fires once; persistence does not re-fire; return to 0 closes it."""
    seq = [
        _reading(0, precip=0.0),
        _reading(1, precip=2.4),  # onset
        _reading(2, precip=3.0),  # still raining
        _reading(3, precip=5.0),  # still raining
        _reading(4, precip=0.0),  # dried up -> close
    ]
    events = detect_precipitation(seq)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "precipitation"
    assert event.severity == "moderate"  # 2.4 mm at onset
    assert event.started_at == _ts(1)
    assert event.ended_at == _ts(4)


def test_precip_severity_bands():
    """Light / moderate / heavy bands map to the documented mm amounts."""
    light = detect_precipitation([_reading(0, precip=0.5)])
    moderate = detect_precipitation([_reading(0, precip=2.4)])
    heavy = detect_precipitation([_reading(0, precip=9.0)])

    assert light[0].severity == "light"
    assert moderate[0].severity == "moderate"
    assert heavy[0].severity == "heavy"


def test_precip_dry_sequence_fires_nothing():
    """A sequence that never rains produces no precipitation events."""
    seq = [_reading(i, precip=0.0) for i in range(5)]

    assert detect_precipitation(seq) == []


# ===========================================================================
# Detector 3: compound "frontal passage"
# ===========================================================================
def test_frontal_passage_fires_on_co_occurrence():
    """Temp drop + wind spike + precip onset within 3 readings fires once."""
    seq = [
        _reading(0, temp=20.0, wind=10.0, precip=0.0),
        _reading(1, temp=16.0, wind=20.0, precip=0.0),
        _reading(2, temp=11.0, wind=27.0, precip=1.5),  # all three co-occur here
    ]
    events = detect_frontal_passage(seq)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "frontal_passage"
    assert event.started_at == _ts(0)  # start of the 3-reading window
    # reason must name all three contributing signals
    assert "temp dropped" in event.reason
    assert "wind rose" in event.reason
    assert "precipitation began" in event.reason
    assert event.detail["temp_drop_c"] == 9.0
    assert event.detail["wind_rise_kmh"] == 17.0


def test_frontal_passage_requires_all_three_signals():
    """Temp drop + wind spike but NO precipitation onset does not fire."""
    seq = [
        _reading(0, temp=20.0, wind=10.0, precip=0.0),
        _reading(1, temp=16.0, wind=20.0, precip=0.0),
        _reading(2, temp=11.0, wind=27.0, precip=0.0),  # never rains
    ]

    assert detect_frontal_passage(seq) == []


def test_frontal_passage_signals_spread_too_far_apart():
    """Signals that occur in different windows (not within 3 readings) do not fire."""
    seq = [
        # temp drop happens here (wind flat, dry)
        _reading(0, temp=20.0, wind=10.0, precip=0.0),
        _reading(1, temp=16.0, wind=10.0, precip=0.0),
        _reading(2, temp=11.0, wind=10.0, precip=0.0),
        # wind spike happens here (temp flat, dry)
        _reading(3, temp=11.0, wind=10.0, precip=0.0),
        _reading(4, temp=11.0, wind=20.0, precip=0.0),
        _reading(5, temp=11.0, wind=27.0, precip=0.0),
        # precip onset happens here (temp flat, wind flat)
        _reading(6, temp=11.0, wind=27.0, precip=0.0),
        _reading(7, temp=11.0, wind=27.0, precip=2.0),
    ]

    assert detect_frontal_passage(seq) == []


# ===========================================================================
# Detector 4: apparent-temperature gap
# ===========================================================================
def test_apparent_gap_large_gap_fires_and_closes():
    """A large feels-like gap fires once and closes when it narrows."""
    seq = [
        _reading(0, temp=-5.0, apparent=-14.0),  # gap 9.0 -> onset
        _reading(1, temp=-5.0, apparent=-13.5),  # gap 8.5 -> still ongoing
        _reading(2, temp=-5.0, apparent=-7.0),  # gap 2.0 -> close
    ]
    events = detect_apparent_gap(seq)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "apparent_gap"
    assert event.started_at == _ts(0)
    assert event.ended_at == _ts(2)
    assert "colder" in event.reason


def test_apparent_gap_small_gap_fires_nothing():
    """A gap below the threshold does not fire."""
    seq = [_reading(i, temp=20.0, apparent=18.0) for i in range(3)]  # gap 2.0 < 7.0

    assert detect_apparent_gap(seq) == []
    # sanity: the threshold really is the documented value
    assert APPARENT_GAP_C == 7.0


# ===========================================================================
# Pipeline + multi-city / state-model integration
# ===========================================================================
def test_pipeline_collects_events_per_city_independently():
    """The pipeline runs all detectors and keeps per-city state separate."""
    ottawa = _temp_seq(_BASELINE + [31.0], city="Ottawa")  # major temp anomaly
    toronto = [
        _reading(0, city="Toronto", temp=20.0, apparent=19.0, precip=0.0, wind=10.0),
        _reading(1, city="Toronto", temp=20.0, apparent=19.0, precip=2.4, wind=10.0),
    ]  # precipitation onset only

    events = run_detectors(ottawa + toronto)

    by_city = {(e.city, e.event_type) for e in events}
    assert ("Ottawa", "temp_anomaly") in by_city
    assert ("Toronto", "precipitation") in by_city
    # Ottawa's dry/calm baseline must not have produced a precipitation event.
    assert ("Ottawa", "precipitation") not in by_city
