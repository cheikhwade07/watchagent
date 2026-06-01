"""Detection pipeline (pure orchestration).

Runs every configured detector over a time-ordered sequence of readings and
collects the emitted events. This step is intentionally pure — no DB writes,
no network, no I/O. Wiring events to storage and the API is a later phase.
"""

from __future__ import annotations

from typing import Callable, Sequence

from app.detection.detectors import (
    Event,
    ReadingLike,
    detect_apparent_gap,
    detect_frontal_passage,
    detect_precipitation,
    detect_temperature_anomaly,
)

# Detectors run in a fixed order so the collected event list is deterministic.
DETECTORS: tuple[Callable[[Sequence[ReadingLike]], list[Event]], ...] = (
    detect_temperature_anomaly,
    detect_precipitation,
    detect_frontal_passage,
    detect_apparent_gap,
)


def run_detectors(readings: Sequence[ReadingLike]) -> list[Event]:
    """Run all detectors over ``readings`` and return every emitted event.

    Each detector groups readings by city and time-orders them internally, so
    the caller may pass a mixed, unsorted multi-city sequence.
    """
    events: list[Event] = []
    for detect in DETECTORS:
        events.extend(detect(readings))
    return events


__all__ = ["DETECTORS", "run_detectors"]
