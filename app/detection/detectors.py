"""Event detection strategies (pure logic, no I/O).

This module turns a time-ordered sequence of weather readings into a list of
:class:`Event` objects. Everything here is intentionally *pure*: a detector
takes readings in and returns events out — it never touches the database, the
network, the filesystem, or any global state. That keeps the logic trivial to
unit-test with hand-built synthetic sequences.

State model (applies to every detector)
----------------------------------------
Events use **onset + close** semantics. A detector fires an event ONCE when a
condition begins, stays silent while the condition persists, and CLOSES the
event (sets ``ended_at``) when conditions return to normal. The same ongoing
condition is never re-emitted on every reading. Each detector is an explicit
per-(city, event_type) state machine: a detector instance owns one
``event_type`` and keys its "currently open" state by city
(see :class:`_StatefulDetector`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Protocol, Sequence


# ---------------------------------------------------------------------------
# Event data object
# ---------------------------------------------------------------------------
@dataclass
class Event:
    """A detected weather event.

    NOTE: deliberately *not* frozen — ``ended_at`` is mutated in place when an
    open event is closed, so the object returned at onset is the same object
    that later carries the close timestamp.
    """

    city: str
    event_type: str  # "temp_anomaly" | "precipitation" | "frontal_passage" | "apparent_gap"
    severity: str | None  # vocabulary depends on event_type (see below); None where not graded
    started_at: str  # observed_at when the event began
    ended_at: str | None  # observed_at when it cleared; None while ongoing
    reason: str  # plain-language explanation of WHY it fired
    detail: dict | None = field(default=None)  # optional supporting numbers

    def to_dict(self) -> dict:
        return asdict(self)


class ReadingLike(Protocol):
    """Structural type for the reading objects detectors consume.

    Matches ``app.storage.models.Reading`` but is duck-typed so tests (and any
    future source) can pass any object exposing these attributes.
    """

    city: str
    observed_at: str
    temperature_2m: float | None
    apparent_temperature: float | None
    precipitation: float | None
    wind_speed_10m: float | None
    weather_code: int | None


# ===========================================================================
# TUNABLE CONSTANTS — every threshold/window lives here so the detectors are
# easy to retune without reading the algorithms. Grouped by detector.
# ===========================================================================

# --- Shared cadence assumption -------------------------------------------------
# Detection reasons describe windows in "days"/"hours", which only makes sense
# relative to an observation cadence. We assume roughly one reading per hour
# (the frontal-passage rule also leans on this: 3 readings ~= 3 hours).
READINGS_PER_DAY = 24

# --- Detector 1: temperature MAD anomaly --------------------------------------
# Rolling baseline spanning ~2 days of recent readings. Window is expressed as a
# count of readings derived from the cadence assumption above so it stays in
# sync if READINGS_PER_DAY is retuned.
TEMP_BASELINE_WINDOW_DAYS = 2
TEMP_BASELINE_WINDOW = TEMP_BASELINE_WINDOW_DAYS * READINGS_PER_DAY  # 48 readings (~2 days)

# Cold-start guard: with fewer than this many readings in the baseline window we
# do NOT have enough history to trust a median/MAD, so we refuse to fire.
TEMP_MIN_BASELINE_READINGS = 10

# Consistency constant for the median absolute deviation. Multiplying the raw
# MAD by 1.4826 makes it an unbiased estimator of the standard deviation for
# normally distributed data, so the resulting "MAD distance" reads in the same
# sigma-like units people expect from a z-score.
MAD_SCALE = 1.4826

# Severity bands on the scaled-MAD distance from the baseline median.
TEMP_MAD_MINOR = 3.0  # >= minor
TEMP_MAD_MODERATE = 4.5  # >= moderate
TEMP_MAD_MAJOR = 6.0  # >= major  (below TEMP_MAD_MINOR => no event)

# Fail-safe maximum lifetime for an open anomaly. Because the baseline is frozen
# at onset, a sustained regime shift (temperature steps up and stays there) would
# otherwise keep the event open forever (ended_at == None). After an event has
# been open this many readings we force-close it and drop the frozen baseline so
# a fresh rolling baseline re-forms: the elevated level then becomes the new
# normal and only re-fires if it is still abnormal against the UPDATED baseline.
# Derived from READINGS_PER_DAY so it tracks the cadence assumption (~24h).
TEMP_MAX_OPEN_READINGS = READINGS_PER_DAY  # ~24 readings (~1 day)

# --- Detector 2: precipitation event ------------------------------------------
# MAD is useless here because precipitation is 0 the vast majority of the time,
# so we use plain thresholds instead.
PRECIP_ONSET_MM = 0.1  # strictly above this counts as "raining" (onset)
PRECIP_CLEAR_MM = 0.1  # at or below this counts as "dry" again (close)
# Severity-by-amount bands (mm in the onset reading).
PRECIP_MODERATE_MM = 2.0  # >= moderate
PRECIP_HEAVY_MM = 7.6  # >= heavy   (between onset and moderate => light)

# --- Detector 3: compound "frontal passage" -----------------------------------
# All three signals must co-occur inside one window of FRONTAL_WINDOW consecutive
# readings (~3 hours at the assumed cadence).
FRONTAL_WINDOW = 3
FRONTAL_TEMP_DROP_C = 5.0  # net temperature fall across the window (degC)
FRONTAL_WIND_RISE_KMH = 15.0  # net wind-speed rise across the window (km/h)
# Precipitation onset inside the window reuses PRECIP_ONSET_MM / PRECIP_CLEAR_MM.

# --- Detector 4: apparent-temperature gap -------------------------------------
APPARENT_GAP_C = 7.0  # |temperature_2m - apparent_temperature| >= this fires


# ===========================================================================
# Small pure helpers
# ===========================================================================
def _group_by_city(readings: Sequence[ReadingLike]) -> dict[str, list[ReadingLike]]:
    """Group readings by city and time-order each group.

    ``observed_at`` is an ISO-8601 string in a fixed, zero-padded format, so a
    plain lexicographic sort is also chronological.
    """
    groups: dict[str, list[ReadingLike]] = {}
    for reading in readings:
        groups.setdefault(reading.city, []).append(reading)
    for city_readings in groups.values():
        city_readings.sort(key=lambda r: r.observed_at)
    return groups


def _scaled_mad(values: Sequence[float], med: float) -> float:
    """Median absolute deviation scaled to sigma-like units."""
    return median([abs(v - med) for v in values]) * MAD_SCALE


def _classify_temp_severity(distance: float) -> str | None:
    if distance >= TEMP_MAD_MAJOR:
        return "major"
    if distance >= TEMP_MAD_MODERATE:
        return "moderate"
    if distance >= TEMP_MAD_MINOR:
        return "minor"
    return None


def _classify_precip_severity(precip_mm: float) -> str:
    if precip_mm >= PRECIP_HEAVY_MM:
        return "heavy"
    if precip_mm >= PRECIP_MODERATE_MM:
        return "moderate"
    return "light"


# ===========================================================================
# State-machine base class
# ===========================================================================
class _StatefulDetector:
    """Carries per-city open-event state for one ``event_type``.

    Subclasses implement :meth:`run`, using :meth:`_emit_onset` to fire a new
    event and :meth:`_close` to stamp ``ended_at`` on the matching open event.
    """

    event_type: str = ""

    def __init__(self) -> None:
        self._open: dict[str, Event] = {}  # city -> currently open event

    def _is_open(self, city: str) -> bool:
        return city in self._open

    def _emit_onset(self, events: list[Event], event: Event) -> None:
        self._open[event.city] = event
        events.append(event)

    def _close(self, city: str, ended_at: str) -> None:
        event = self._open.pop(city, None)
        if event is not None:
            event.ended_at = ended_at


# ===========================================================================
# Detector 1: temperature MAD anomaly
# ===========================================================================
class TemperatureAnomalyDetector(_StatefulDetector):
    """Flags readings far from a robust rolling baseline of recent temperatures.

    While an event is OPEN the baseline (median + scaled MAD) is *frozen* at the
    value computed at onset. This is deliberate: if we kept recomputing the
    rolling window it would absorb the very anomaly we are tracking, drag the
    median toward the spike, and prematurely "normalize" an ongoing event. The
    frozen reference is also the natural yardstick for deciding when things have
    returned to normal (close).

    An event closes in one of two ways:
      1. Normal close: a reading returns within TEMP_MAD_MINOR of the frozen
         baseline median (the condition genuinely cleared).
      2. Max-duration fallback: a sustained regime shift never returns to the
         old baseline, so after TEMP_MAX_OPEN_READINGS readings we force-close
         the event and drop the frozen baseline. Subsequent readings rebuild a
         fresh rolling baseline, letting the elevated level become the new
         normal — and only re-fire if still abnormal against that new baseline.
    """

    event_type = "temp_anomaly"

    def __init__(self) -> None:
        super().__init__()
        self._baseline: dict[str, tuple[float, float]] = {}  # city -> (median, scaled_mad)
        self._open_age: dict[str, int] = {}  # city -> readings elapsed since onset

    def run(self, readings: Sequence[ReadingLike]) -> list[Event]:
        events: list[Event] = []
        for city, city_readings in _group_by_city(readings).items():
            for index, reading in enumerate(city_readings):
                temp = reading.temperature_2m
                if temp is None:
                    continue

                # --- OPEN: compare against the frozen onset baseline ---
                if self._is_open(city):
                    med, scaled = self._baseline[city]
                    distance = abs(temp - med) / scaled
                    if distance < TEMP_MAD_MINOR:  # back to normal -> normal close
                        self._close(city, reading.observed_at)
                        self._baseline.pop(city, None)
                        self._open_age.pop(city, None)
                        continue
                    # Still anomalous vs the frozen baseline: age the open event.
                    self._open_age[city] += 1
                    if self._open_age[city] >= TEMP_MAX_OPEN_READINGS:
                        # Sustained regime shift: force-close and drop the frozen
                        # baseline so a fresh rolling baseline re-forms below.
                        self._close(city, reading.observed_at)
                        self._baseline.pop(city, None)
                        self._open_age.pop(city, None)
                    continue  # otherwise still ongoing: stay silent

                # --- CLOSED: evaluate onset from the rolling baseline window ---
                window = city_readings[max(0, index - TEMP_BASELINE_WINDOW):index]
                temps = [r.temperature_2m for r in window if r.temperature_2m is not None]
                if len(temps) < TEMP_MIN_BASELINE_READINGS:
                    continue  # cold start: not enough baseline to trust

                med = median(temps)
                scaled = _scaled_mad(temps, med)
                if scaled == 0:
                    # Zero-variability baseline (degenerate/synthetic): a robust
                    # distance is undefined, so we conservatively do not fire.
                    continue

                distance = abs(temp - med) / scaled
                severity = _classify_temp_severity(distance)
                if severity is None:
                    continue

                direction = "above" if temp > med else "below"
                reason = (
                    f"{city} temperature {temp:.1f}°C is {distance:.1f} MAD "
                    f"{direction} the {TEMP_BASELINE_WINDOW_DAYS}-day median of "
                    f"{med:.1f}°C (severity: {severity})."
                )
                self._emit_onset(
                    events,
                    Event(
                        city=city,
                        event_type=self.event_type,
                        severity=severity,
                        started_at=reading.observed_at,
                        ended_at=None,
                        reason=reason,
                        detail={
                            "mad_distance": round(distance, 2),
                            "baseline_median": round(med, 2),
                            "scaled_mad": round(scaled, 3),
                            "window_size": len(temps),
                        },
                    ),
                )
                self._baseline[city] = (med, scaled)
                self._open_age[city] = 0  # onset reading; counts readings since
        return events


# ===========================================================================
# Detector 2: precipitation event
# ===========================================================================
class PrecipitationDetector(_StatefulDetector):
    """Threshold-based wet/dry state machine (MAD is unsuitable for precip)."""

    event_type = "precipitation"

    def run(self, readings: Sequence[ReadingLike]) -> list[Event]:
        events: list[Event] = []
        for city, city_readings in _group_by_city(readings).items():
            for reading in city_readings:
                precip = reading.precipitation
                if precip is None:
                    continue

                if self._is_open(city):
                    if precip <= PRECIP_CLEAR_MM:  # dried up -> close
                        self._close(city, reading.observed_at)
                    continue  # still raining: stay silent

                if precip > PRECIP_ONSET_MM:  # dry -> wet : onset
                    severity = _classify_precip_severity(precip)
                    reason = (
                        f"{city} precipitation began "
                        f"({precip:.1f} mm, severity: {severity})."
                    )
                    self._emit_onset(
                        events,
                        Event(
                            city=city,
                            event_type=self.event_type,
                            severity=severity,
                            started_at=reading.observed_at,
                            ended_at=None,
                            reason=reason,
                            detail={
                                "precip_mm": round(precip, 2),
                                "onset_threshold_mm": PRECIP_ONSET_MM,
                            },
                        ),
                    )
        return events


# ===========================================================================
# Detector 3: compound "frontal passage"
# ===========================================================================
class FrontalPassageDetector(_StatefulDetector):
    """Explainable CO-OCCURRENCE rule (NOT a composite score).

    Fires only when a sharp temperature drop, a wind spike, AND precipitation
    onset all happen inside one window of FRONTAL_WINDOW consecutive readings.

    KNOWN LIMITATION: this recognizes exactly one named signature. Genuinely
    novel multi-field patterns are not given a name here — but their individual
    components are still caught by the temperature anomaly detector (1), the
    precipitation detector (2), and the per-field logic, so nothing is silently
    dropped; it just is not labelled "frontal passage".
    """

    event_type = "frontal_passage"

    def run(self, readings: Sequence[ReadingLike]) -> list[Event]:
        events: list[Event] = []
        for city, city_readings in _group_by_city(readings).items():
            for index in range(len(city_readings)):
                if index + 1 < FRONTAL_WINDOW:
                    continue  # not enough readings for a full window yet
                window = city_readings[index - FRONTAL_WINDOW + 1: index + 1]

                signature = self._signature(window)
                if signature is None:
                    if self._is_open(city):  # signature gone -> close
                        self._close(city, city_readings[index].observed_at)
                    continue

                if not self._is_open(city):  # co-occurrence detected -> onset
                    temp_drop, wind_rise = signature
                    reason = (
                        f"{city} frontal passage: temp dropped {temp_drop:.1f}°C, "
                        f"wind rose {wind_rise:.1f} km/h, precipitation began — "
                        f"within {FRONTAL_WINDOW} hours."
                    )
                    self._emit_onset(
                        events,
                        Event(
                            city=city,
                            event_type=self.event_type,
                            severity=None,  # co-occurrence rule, not graded
                            started_at=window[0].observed_at,
                            ended_at=None,
                            reason=reason,
                            detail={
                                "temp_drop_c": round(temp_drop, 1),
                                "wind_rise_kmh": round(wind_rise, 1),
                                "window_readings": FRONTAL_WINDOW,
                            },
                        ),
                    )
        return events

    @staticmethod
    def _signature(window: Sequence[ReadingLike]) -> tuple[float, float] | None:
        """Return (temp_drop, wind_rise) if all three signals co-occur, else None."""
        temps = [r.temperature_2m for r in window]
        winds = [r.wind_speed_10m for r in window]
        precs = [r.precipitation for r in window]
        if any(v is None for v in (*temps, *winds, *precs)):
            return None

        temp_drop = temps[0] - temps[-1]  # net fall across the window
        wind_rise = winds[-1] - winds[0]  # net rise across the window
        precip_onset = any(
            precs[k - 1] <= PRECIP_CLEAR_MM and precs[k] > PRECIP_ONSET_MM
            for k in range(1, len(window))
        )

        if (
            temp_drop >= FRONTAL_TEMP_DROP_C
            and wind_rise >= FRONTAL_WIND_RISE_KMH
            and precip_onset
        ):
            return temp_drop, wind_rise
        return None


# ===========================================================================
# Detector 4: apparent-temperature gap (bonus)
# ===========================================================================
class ApparentGapDetector(_StatefulDetector):
    """Fires when "feels-like" diverges sharply from the air temperature."""

    event_type = "apparent_gap"

    def run(self, readings: Sequence[ReadingLike]) -> list[Event]:
        events: list[Event] = []
        for city, city_readings in _group_by_city(readings).items():
            for reading in city_readings:
                temp = reading.temperature_2m
                apparent = reading.apparent_temperature
                if temp is None or apparent is None:
                    continue
                gap = abs(temp - apparent)

                if self._is_open(city):
                    if gap < APPARENT_GAP_C:  # gap closed -> close
                        self._close(city, reading.observed_at)
                    continue

                if gap >= APPARENT_GAP_C:  # large divergence -> onset
                    colder = apparent < temp
                    direction = "colder" if colder else "hotter"
                    cause = "wind chill" if colder else "humidity"
                    reason = (
                        f"{city} feels {gap:.1f}°C {direction} than air "
                        f"temperature ({cause})."
                    )
                    self._emit_onset(
                        events,
                        Event(
                            city=city,
                            event_type=self.event_type,
                            severity=None,  # simple threshold, not graded
                            started_at=reading.observed_at,
                            ended_at=None,
                            reason=reason,
                            detail={
                                "temp_c": round(temp, 1),
                                "apparent_c": round(apparent, 1),
                                "gap_c": round(gap, 1),
                            },
                        ),
                    )
        return events


# ===========================================================================
# Functional entry points (pure: readings in, events out)
# ===========================================================================
def detect_temperature_anomaly(readings: Sequence[ReadingLike]) -> list[Event]:
    return TemperatureAnomalyDetector().run(readings)


def detect_precipitation(readings: Sequence[ReadingLike]) -> list[Event]:
    return PrecipitationDetector().run(readings)


def detect_frontal_passage(readings: Sequence[ReadingLike]) -> list[Event]:
    return FrontalPassageDetector().run(readings)


def detect_apparent_gap(readings: Sequence[ReadingLike]) -> list[Event]:
    return ApparentGapDetector().run(readings)


__all__ = [
    "Event",
    "ReadingLike",
    "TemperatureAnomalyDetector",
    "PrecipitationDetector",
    "FrontalPassageDetector",
    "ApparentGapDetector",
    "detect_temperature_anomaly",
    "detect_precipitation",
    "detect_frontal_passage",
    "detect_apparent_gap",
]
