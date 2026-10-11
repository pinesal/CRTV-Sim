from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from _support import WorkspaceTestCase
from retroweather.cli import main
from retroweather.config import AppPaths, initialize
from retroweather.locations import save_selected_location
from retroweather.models import Location, NormalizedWeatherSnapshot
from retroweather.providers.open_meteo import LocationSearchResult, ProviderRequestError, ProviderResponseError
from retroweather.providers.open_meteo_weather import (
    CURRENT_FIELDS,
    DAILY_FIELDS,
    HOURLY_FIELDS,
    OpenMeteoForecastClient,
    condition_text,
)


FIXTURES = Path(__file__).parent / "fixtures"
FORECAST_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-forecast.json").read_text(encoding="utf-8")
)
LOCATION_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-get.json").read_text(encoding="utf-8")
)
FIXED_NOW = datetime(2026, 8, 6, 21, 15, 3, tzinfo=timezone.utc)


def fixture_snapshot(location: Location, units: str = "imperial") -> NormalizedWeatherSnapshot:
    return OpenMeteoForecastClient(
        transport=lambda url, timeout: FORECAST_PAYLOAD,
        clock=lambda: FIXED_NOW,
    ).fetch_snapshot(location, units)


class FixtureForecastClient:
    def __init__(self, timeout: float = 20.0, clock=None) -> None:
        self.timeout = timeout

    def fetch(self, location: Location, units: str, expires_minutes: int = 15):
        return OpenMeteoForecastClient(
            transport=lambda url, timeout: FORECAST_PAYLOAD,
            clock=lambda: FIXED_NOW,
        ).fetch(location, units, expires_minutes)

    def fetch_snapshot(self, location: Location, units: str, expires_minutes: int = 15):
        return self.fetch(location, units, expires_minutes).snapshot


class OpenMeteoForecastTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location_result = LocationSearchResult.from_api(LOCATION_PAYLOAD)
        self.location = self.location_result.to_location()

    def test_request_and_normalized_forecast(self) -> None:
        requests: list[tuple[str, float]] = []

        def transport(url: str, timeout: float):
            requests.append((url, timeout))
            return FORECAST_PAYLOAD

        snapshot = OpenMeteoForecastClient(
            timeout=9, transport=transport, clock=lambda: FIXED_NOW
        ).fetch_snapshot(self.location, "imperial")
        parameters = parse_qs(urlparse(requests[0][0]).query)

        self.assertEqual(parameters["current"], [",".join(CURRENT_FIELDS)])
        self.assertEqual(parameters["hourly"], [",".join(HOURLY_FIELDS)])
        self.assertEqual(parameters["daily"], [",".join(DAILY_FIELDS)])
        self.assertEqual(parameters["temperature_unit"], ["fahrenheit"])
        self.assertEqual(parameters["wind_speed_unit"], ["mph"])
        self.assertEqual(parameters["precipitation_unit"], ["inch"])
        self.assertEqual(parameters["timezone"], ["America/Los_Angeles"])
        self.assertEqual(requests[0][1], 9.0)
        self.assertEqual(snapshot.current.condition_text, "Mainly clear")
        self.assertAlmostEqual(snapshot.current.visibility, 10.0)
        self.assertEqual(len(snapshot.hourly), 3)
        self.assertEqual(snapshot.hourly[0].starts_at.hour, 14)
        self.assertEqual(len(snapshot.daily), 2)
        self.assertEqual(snapshot.daily[0].sunrise.hour, 6)
        self.assertEqual(snapshot.metadata.fetched_at, FIXED_NOW)
        self.assertEqual(snapshot.metadata.provider, "open-meteo")

    def test_metric_visibility_and_unknown_condition_fallback(self) -> None:
        payload = copy.deepcopy(FORECAST_PAYLOAD)
        payload["current"]["weather_code"] = 500
        snapshot = OpenMeteoForecastClient(
            transport=lambda url, timeout: payload, clock=lambda: FIXED_NOW
        ).fetch_snapshot(self.location, "metric")

        self.assertEqual(snapshot.current.condition_text, "Unknown conditions")
        self.assertAlmostEqual(snapshot.current.visibility, 16.09344)
        self.assertEqual(condition_text(None), "Unknown conditions")

    def test_mismatched_arrays_and_api_errors_are_rejected(self) -> None:
        malformed = copy.deepcopy(FORECAST_PAYLOAD)
        malformed["hourly"]["temperature_2m"].pop()
        with self.assertRaisesRegex(ProviderResponseError, "equal lengths"):
            OpenMeteoForecastClient(
                transport=lambda url, timeout: malformed, clock=lambda: FIXED_NOW
            ).fetch_snapshot(self.location, "imperial")

        with self.assertRaisesRegex(ProviderRequestError, "fixture failure"):
            OpenMeteoForecastClient(
                transport=lambda url, timeout: {"error": True, "reason": "fixture failure"}
            ).fetch_snapshot(self.location, "imperial")

    def test_repeated_daylight_saving_hour_is_preserved(self) -> None:
        payload = copy.deepcopy(FORECAST_PAYLOAD)
        payload["current"]["time"] = "2026-11-01T00:15"
        payload["hourly"]["time"] = [
            "2026-11-01T00:00",
            "2026-11-01T01:00",
            "2026-11-01T01:00",
            "2026-11-01T02:00",
        ]
        payload["daily"]["time"] = ["2026-11-01", "2026-11-02"]
        payload["daily"]["sunrise"] = [
            "2026-11-01T06:15",
            "2026-11-02T06:16",
        ]
        payload["daily"]["sunset"] = [
            "2026-11-01T16:59",
            "2026-11-02T16:58",
        ]

        snapshot = OpenMeteoForecastClient(
            transport=lambda url, timeout: payload,
            clock=lambda: FIXED_NOW,
        ).fetch_snapshot(self.location, "imperial")

        repeated = snapshot.hourly[1:3]
        self.assertEqual(repeated[0].starts_at.hour, 1)
        self.assertEqual(repeated[1].starts_at.hour, 1)
        self.assertNotEqual(repeated[0].starts_at.utcoffset(), repeated[1].starts_at.utcoffset())
        self.assertLess(repeated[0].starts_at.timestamp(), repeated[1].starts_at.timestamp())

    def test_cli_fetch_saves_valid_snapshot(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        save_selected_location(paths, self.location_result.to_selection())
        output = io.StringIO()

        with patch("retroweather.weather.OpenMeteoForecastClient", FixtureForecastClient):
            with redirect_stdout(output):
                result = main(["--home", str(self.temporary), "fetch"])

        self.assertEqual(result, 0)
        self.assertTrue(paths.latest_snapshot.is_file())
        saved = NormalizedWeatherSnapshot.load(paths.latest_snapshot)
        self.assertEqual(saved.location, self.location)
        self.assertIn("Current: 78.4° · Mainly clear", output.getvalue())
