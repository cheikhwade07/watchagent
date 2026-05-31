"""Lightweight data containers for the storage layer (no ORM).

These dataclasses describe the shape of a weather reading as it moves between
the poller, the repository, and the API. Persistence is handled with raw
sqlite3 in ``repository.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Reading:
    """A single weather reading for one city at one observation time."""

    city: str
    observed_at: str
    fetched_at: str
    temperature_2m: float | None = None
    apparent_temperature: float | None = None
    precipitation: float | None = None
    wind_speed_10m: float | None = None
    weather_code: int | None = None
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Reading":
        return cls(
            id=row["id"],
            city=row["city"],
            observed_at=row["observed_at"],
            fetched_at=row["fetched_at"],
            temperature_2m=row["temperature_2m"],
            apparent_temperature=row["apparent_temperature"],
            precipitation=row["precipitation"],
            wind_speed_10m=row["wind_speed_10m"],
            weather_code=row["weather_code"],
        )
