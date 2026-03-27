"""WeatherFetcher — retrieves current conditions and 7-day forecast from Open-Meteo.

No API key is required.  Data flows through two endpoints:
  1. Geocoding  — maps a place name to latitude/longitude
  2. Forecast   — retrieves weather data for those coordinates
"""

from pathlib import Path

import requests

from fetchers.base_fetcher import BaseFetcher, FetcherError

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Default coordinates (London) used when geocoding returns no results
_DEFAULT_LAT = 51.5
_DEFAULT_LON = -0.1
_DEFAULT_NAME = "London (default)"


class WeatherFetcher(BaseFetcher):
    """Fetches weather data for a given location topic from Open-Meteo."""

    def fetch(self, topic: str) -> list[Path]:
        """Fetch weather data for *topic* and write a plain-text document.

        Returns a single-element list containing the path of the written file.
        Raises :class:`~fetchers.base_fetcher.FetcherError` on any network or
        unexpected failure.
        """
        try:
            lat, lon, location_name = self._geocode(topic)
            content = self._build_report(topic, lat, lon, location_name)
        except FetcherError:
            raise
        except Exception as exc:
            raise FetcherError(f"WeatherFetcher failed for {topic!r}: {exc}") from exc

        topic_slug = topic.lower().replace(" ", "_").replace("/", "_")[:50]
        filename = f"weather_{topic_slug}.txt"
        path = self._write_doc(filename, content)
        return [path]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _geocode(self, topic: str) -> tuple[float, float, str]:
        """Return (latitude, longitude, resolved_name) for *topic*."""
        try:
            resp = requests.get(
                _GEOCODING_URL,
                params={"name": topic, "count": 1, "language": "en", "format": "json"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise FetcherError(f"WeatherFetcher failed for {topic!r}: {exc}") from exc

        results = data.get("results") or []
        if results:
            r = results[0]
            return float(r["latitude"]), float(r["longitude"]), str(r["name"])

        return _DEFAULT_LAT, _DEFAULT_LON, _DEFAULT_NAME

    def _build_report(
        self, topic: str, lat: float, lon: float, location_name: str
    ) -> str:
        """Fetch forecast data and format it as a plain-text report."""
        try:
            resp = requests.get(
                _FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                    "timezone": "auto",
                    "forecast_days": 7,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise FetcherError(f"WeatherFetcher failed for {topic!r}: {exc}") from exc

        current = data.get("current", {})
        current_units = data.get("current_units", {})
        daily = data.get("daily", {})
        daily_units = data.get("daily_units", {})

        lines: list[str] = []

        # Header
        lines.append(f"Weather Report — {location_name}")
        lines.append(f"Query topic  : {topic}")
        lines.append(f"Coordinates  : lat={lat}, lon={lon}")
        lines.append(f"Timezone     : {data.get('timezone', 'unknown')}")
        lines.append("")

        # Current conditions
        lines.append("=== Current Conditions ===")
        temp_unit = current_units.get("temperature_2m", "°C")
        hum_unit = current_units.get("relative_humidity_2m", "%")
        wind_unit = current_units.get("wind_speed_10m", "km/h")
        precip_unit = current_units.get("precipitation", "mm")

        lines.append(f"Temperature  : {current.get('temperature_2m', 'N/A')} {temp_unit}")
        lines.append(f"Humidity     : {current.get('relative_humidity_2m', 'N/A')} {hum_unit}")
        lines.append(f"Wind speed   : {current.get('wind_speed_10m', 'N/A')} {wind_unit}")
        lines.append(f"Precipitation: {current.get('precipitation', 'N/A')} {precip_unit}")
        lines.append("")

        # 7-day daily summary
        lines.append("=== 7-Day Forecast ===")
        d_temp_max_unit = daily_units.get("temperature_2m_max", "°C")
        d_temp_min_unit = daily_units.get("temperature_2m_min", "°C")
        d_precip_unit = daily_units.get("precipitation_sum", "mm")

        dates = daily.get("time", [])
        temp_max_list = daily.get("temperature_2m_max", [])
        temp_min_list = daily.get("temperature_2m_min", [])
        precip_list = daily.get("precipitation_sum", [])

        for i, day in enumerate(dates):
            t_max = temp_max_list[i] if i < len(temp_max_list) else "N/A"
            t_min = temp_min_list[i] if i < len(temp_min_list) else "N/A"
            precip = precip_list[i] if i < len(precip_list) else "N/A"
            lines.append(
                f"  {day}  max={t_max}{d_temp_max_unit}"
                f"  min={t_min}{d_temp_min_unit}"
                f"  precip={precip}{d_precip_unit}"
            )

        lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from datetime import date

    topic = sys.argv[1] if len(sys.argv) > 1 else "London"
    run_id = f"standalone_{date.today()}"
    paths = WeatherFetcher(run_id).fetch(topic)
    for p in paths:
        print(p)
