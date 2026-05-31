"""Open-Meteo HTTP client (httpx-based).

Fetches current conditions for the monitored cities. A failed fetch is logged
as a WARNING and yields ``None`` for that city rather than raising, so one bad
response never aborts a poll cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,precipitation,"
    "wind_speed_10m,weather_code"
)


@dataclass(frozen=True)
class City:
    name: str
    latitude: float
    longitude: float


# Hardcoded set of monitored cities.
CITIES: tuple[City, ...] = (
    City("Ottawa", 45.42, -75.69),
    City("Toronto", 43.70, -79.42),
    City("Vancouver", 49.25, -123.12),
)


@dataclass(frozen=True)
class CurrentReading:
    """A parsed Open-Meteo ``current`` block for one city."""

    city: str
    observed_at: str
    temperature_2m: float | None
    apparent_temperature: float | None
    precipitation: float | None
    wind_speed_10m: float | None
    weather_code: int | None


async def fetch_city(
    client: httpx.AsyncClient, city: City
) -> CurrentReading | None:
    """Fetch and parse current conditions for one city.

    Returns ``None`` (after logging a warning) if the request or parsing fails.
    """
    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "current": CURRENT_FIELDS,
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }
    try:
        response = await client.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        current = payload["current"]
        return CurrentReading(
            city=city.name,
            observed_at=current["time"],
            temperature_2m=current.get("temperature_2m"),
            apparent_temperature=current.get("apparent_temperature"),
            precipitation=current.get("precipitation"),
            wind_speed_10m=current.get("wind_speed_10m"),
            weather_code=current.get("weather_code"),
        )
    except Exception as exc:  # noqa: BLE001 - skip this city, never abort cycle
        logger.warning("Failed to fetch weather for %s: %s", city.name, exc)
        return None
