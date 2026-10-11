from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, initialize
from retroweather.locations import save_selected_location
from retroweather.providers.open_meteo import LocationSearchResult, ProviderRequestError
from retroweather.providers.open_meteo_weather import OpenMeteoForecastClient
from retroweather.weather import (
    build_weather_status,
    fetch_and_store_weather,
    load_fetch_state,
)


FIXTURES = Path(__file__).parent / "fixtures"
FORECAST_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-forecast.json").read_text(encoding="utf-8")
)
LOCATION_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-get.json").read_text(encoding="utf-8")
)
FETCHED_AT = datetime(2026, 8, 6, 21, 15, 3, tzinfo=timezone.utc)


class FailingForecastClient:
    def __init__(self, message: str = "fixture network outage") -> None:
        self.message = message
        self.last_raw_response = {"error": True, "reason": message}

    def fetch(self, location, units, expires_minutes=15):
        raise ProviderRequestError(self.message)


class WeatherCacheTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = AppPaths.discover(self.temporary)
        initialize(self.paths)
        selection = LocationSearchResult.from_api(LOCATION_PAYLOAD).to_selection()
        save_selected_location(self.paths, selection)
        self.client = OpenMeteoForecastClient(
            transport=lambda url, timeout: FORECAST_PAYLOAD,
            clock=lambda: FETCHED_AT,
        )

    def test_success_archives_raw_response_and_fetch_health(self) -> None:
        snapshot = fetch_and_store_weather(
            self.paths, self.client, clock=lambda: FETCHED_AT
        )
        state = load_fetch_state(self.paths)
        raw = json.loads(self.paths.latest_raw_forecast.read_text(encoding="utf-8"))
        status = build_weather_status(self.paths, now=FETCHED_AT)

        self.assertEqual(raw, FORECAST_PAYLOAD)
        self.assertEqual(state.state, "success")
        self.assertEqual(state.last_success_at, snapshot.metadata.fetched_at)
        self.assertTrue(status.available)
        self.assertTrue(status.snapshot_valid)
        self.assertFalse(status.refresh_due)
        self.assertFalse(status.stale)
        self.assertEqual(status.age_seconds, 0)

    def test_failure_preserves_snapshot_and_reports_fallback(self) -> None:
        fetch_and_store_weather(self.paths, self.client, clock=lambda: FETCHED_AT)
        original = self.paths.latest_snapshot.read_bytes()
        failed_at = FETCHED_AT + timedelta(hours=2)

        with self.assertRaisesRegex(ProviderRequestError, "network outage"):
            fetch_and_store_weather(
                self.paths,
                FailingForecastClient(),
                clock=lambda: failed_at,
            )

        self.assertEqual(self.paths.latest_snapshot.read_bytes(), original)
        state = load_fetch_state(self.paths)
        status = build_weather_status(self.paths, now=failed_at)
        raw = json.loads(self.paths.latest_raw_forecast.read_text(encoding="utf-8"))
        self.assertEqual(state.state, "failed")
        self.assertEqual(state.last_success_at, FETCHED_AT)
        self.assertIn("network outage", state.error)
        self.assertTrue(status.available)
        self.assertTrue(status.refresh_due)
        self.assertTrue(status.stale)
        self.assertEqual(status.age_seconds, 7200)
        self.assertEqual(raw["reason"], "fixture network outage")

    def test_invalid_snapshot_is_not_advertised_as_available(self) -> None:
        self.paths.latest_snapshot.write_text("{broken", encoding="utf-8")
        status = build_weather_status(self.paths, now=FETCHED_AT)

        self.assertFalse(status.available)
        self.assertFalse(status.snapshot_valid)
        self.assertIn("invalid JSON", status.snapshot_error)
