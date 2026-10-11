from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, initialize, load_settings
from retroweather.locations import load_selected_location
from retroweather.locations import save_selected_location
from retroweather.models import Location
from retroweather.providers.open_meteo import LocationSearchResult
from retroweather.providers.open_meteo import ProviderRequestError
from retroweather.providers.open_meteo_weather import OpenMeteoForecastClient
from retroweather.web.server import create_server
from retroweather.weather import fetch_and_store_weather


FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-search.json").read_text(encoding="utf-8")
)
GET_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-get.json").read_text(encoding="utf-8")
)
FORECAST_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-forecast.json").read_text(encoding="utf-8")
)


class FixtureGeocoder:
    def search(self, query: str, count: int = 10):
        return tuple(
            LocationSearchResult.from_api(item, f"fixture[{index}]")
            for index, item in enumerate(SEARCH_PAYLOAD["results"])
        )

    def get(self, provider_location_id: int):
        return LocationSearchResult.from_api(GET_PAYLOAD)


class FixtureWeatherClient:
    def fetch(self, location: Location, units: str, expires_minutes: int = 15):
        return OpenMeteoForecastClient(
            transport=lambda url, timeout: FORECAST_PAYLOAD,
        ).fetch(location, units, expires_minutes)

    def fetch_snapshot(self, location: Location, units: str, expires_minutes: int = 15):
        return self.fetch(location, units, expires_minutes).snapshot


class FailingWeatherClient:
    last_raw_response = {"error": True, "reason": "fixture outage"}

    def fetch(self, location: Location, units: str, expires_minutes: int = 15):
        raise ProviderRequestError("fixture outage")


class FixtureJobManager:
    def __init__(self):
        self.actions: list[str] = []

    def status(self):
        return {
            "schema_version": 1,
            "state": "idle",
            "action": None,
            "stage": "Ready",
            "progress": 0,
            "started_at": None,
            "finished_at": None,
            "generation_id": None,
            "validation_result": None,
            "error": None,
            "last_successful_publication": None,
            "last_successful_generation": None,
        }

    def start(self, action: str):
        self.actions.append(action)
        payload = self.status()
        payload.update({"state": "running", "action": action, "stage": "Queued"})
        return payload


class DashboardTests(WorkspaceTestCase):
    def test_shutdown_endpoint_stops_server_cleanly(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST"
        )
        with urlopen(request, timeout=3) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 202)
            self.assertTrue(payload["ok"])
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        server.server_close()

    def test_dashboard_and_status_are_served(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                dashboard = response.read().decode("utf-8")
            with urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=3
            ) as response:
                status = json.load(response)

            self.assertIn("RetroWeather", dashboard)
            self.assertIn("location-form", dashboard)
            self.assertIn("channel-render", dashboard)
            self.assertIn("asset-library", dashboard)
            self.assertIn("theme-inspector", dashboard)
            self.assertIn("music-volume", dashboard)
            self.assertIn("setup-build", dashboard)
            self.assertIn("general-settings-form", dashboard)
            self.assertIn("appearance-form", dashboard)
            self.assertIn("automation-form", dashboard)
            self.assertIn("section-nav", dashboard)
            self.assertTrue(status["initialized"])
            self.assertFalse(status["weather_snapshot"])
            self.assertFalse(status["published_channel"])
            self.assertIsNone(status["selected_location"])
            self.assertEqual(status["asset_summary"]["total"], 0)
            self.assertEqual(status["music_settings"]["volume"], 0.28)
            self.assertEqual(status["dashboard_settings"]["channel_name"], "RetroWeather")
            self.assertEqual(status["dashboard_settings"]["units"], "imperial")
            self.assertEqual(status["dashboard_settings"]["appearance"]["safe_margin"], 28)
            self.assertEqual(status["dashboard_settings"]["refresh"]["render_minutes"], 30.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dashboard_settings_can_be_saved_and_reset(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]

            def post(route: str, payload: dict):
                request = Request(
                    f"http://127.0.0.1:{port}{route}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    return json.load(response)

            general = post(
                "/api/dashboard/settings",
                {"section": "general", "channel_name": "Local Weather", "units": "metric"},
            )
            self.assertEqual(general["settings"]["channel_name"], "Local Weather")
            self.assertEqual(general["settings"]["units"], "metric")

            appearance = post(
                "/api/dashboard/settings",
                {
                    "section": "appearance",
                    "panel_opacity": 0.66,
                    "safe_margin": 32,
                    "text_scale": 1.05,
                    "palette": {
                        "header": "#123456",
                        "accent": "#abcdef",
                        "condition": "#fedcba",
                    },
                },
            )
            self.assertEqual(appearance["settings"]["appearance"]["panel_opacity"], 0.66)
            self.assertEqual(appearance["settings"]["appearance"]["safe_margin"], 32)
            self.assertEqual(
                appearance["settings"]["appearance"]["palette"]["accent"], "0xabcdef"
            )

            automation = post(
                "/api/dashboard/settings",
                {
                    "section": "automation",
                    "automatic": False,
                    "data_minutes": 20,
                    "render_minutes": 45,
                    "stale_after_minutes": 120,
                },
            )
            self.assertFalse(automation["settings"]["refresh"]["automatic"])
            self.assertEqual(automation["settings"]["refresh"]["render_minutes"], 45.0)

            persisted = load_settings(paths)
            self.assertEqual(persisted["channel"]["display_name"], "Local Weather")
            self.assertEqual(persisted["appearance"]["palette"]["header"], "0x123456")
            self.assertEqual(persisted["refresh"]["data_minutes"], 20.0)

            reset = post("/api/dashboard/settings/reset", {"section": "appearance"})
            self.assertEqual(reset["settings"]["appearance"]["safe_margin"], 28)
            self.assertEqual(
                reset["settings"]["appearance"]["palette"]["accent"], "0x59c7e8"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dashboard_settings_reject_invalid_values(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            request = Request(
                f"http://127.0.0.1:{port}/api/dashboard/settings",
                data=json.dumps(
                    {
                        "section": "appearance",
                        "panel_opacity": 2,
                        "safe_margin": 28,
                        "text_scale": 1,
                        "palette": {
                            "header": "#071827",
                            "accent": "not-a-color",
                            "condition": "#82edb0",
                        },
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(load_settings(paths)["appearance"]["panel_opacity"], 0.58)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_location_can_be_searched_and_selected_through_api(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(
            paths, host="127.0.0.1", port=0, geocoder=FixtureGeocoder()
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urlopen(
                f"http://127.0.0.1:{port}/api/locations/search?q=Los%20Angeles",
                timeout=3,
            ) as response:
                search = json.load(response)
            request = Request(
                f"http://127.0.0.1:{port}/api/location",
                data=json.dumps({"provider_location_id": 5368361}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                selected = json.load(response)

            self.assertEqual(len(search["results"]), 2)
            self.assertEqual(
                selected["selection"]["location"]["display_name"],
                "Los Angeles, California, United States",
            )
            persisted = load_selected_location(paths)
            self.assertEqual(persisted.provider_location_id, 5368361)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_weather_can_be_fetched_and_read_through_api(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        selection = LocationSearchResult.from_api(GET_PAYLOAD).to_selection()
        save_selected_location(paths, selection)
        server = create_server(
            paths,
            host="127.0.0.1",
            port=0,
            weather_client=FixtureWeatherClient(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            fetch = Request(
                f"http://127.0.0.1:{port}/api/weather/fetch",
                data=b"",
                method="POST",
            )
            with urlopen(fetch, timeout=3) as response:
                started = json.load(response)
                self.assertEqual(response.status, 202)
            completed = _wait_for_job(port)
            with urlopen(f"http://127.0.0.1:{port}/api/weather", timeout=3) as response:
                stored = json.load(response)
            with urlopen(f"http://127.0.0.1:{port}/api/plan", timeout=3) as response:
                plan = json.load(response)

            self.assertEqual(started["action"], "fetch")
            self.assertEqual(completed["state"], "success")
            self.assertEqual(stored["current"]["condition_text"], "Mainly clear")
            self.assertTrue(paths.latest_snapshot.is_file())
            self.assertEqual(plan["duration_seconds"], 120)
            self.assertEqual(plan["segments"][-1]["end_seconds"], 120)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_failed_web_refresh_keeps_last_snapshot_available(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        selection = LocationSearchResult.from_api(GET_PAYLOAD).to_selection()
        save_selected_location(paths, selection)
        fetch_and_store_weather(paths, FixtureWeatherClient())
        server = create_server(
            paths,
            host="127.0.0.1",
            port=0,
            weather_client=FailingWeatherClient(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            request = Request(
                f"http://127.0.0.1:{port}/api/weather/fetch",
                data=b"",
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 202)
            completed = _wait_for_job(port)
            with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
                status = json.load(response)

            self.assertEqual(completed["state"], "failed")
            self.assertIn("fixture outage", completed["error"])
            self.assertTrue(status["weather_status"]["available"])
            self.assertEqual(status["weather_status"]["state"], "failed")
            self.assertIn("fixture outage", status["weather_status"]["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_asset_api_detects_files_added_after_server_start(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/assets", timeout=3) as response:
                initial = json.load(response)
            added = paths.media / "graphics" / "conditions" / "sunny" / "day" / "new.png"
            added.write_bytes(b"not a real image")
            with urlopen(f"http://127.0.0.1:{port}/api/assets", timeout=3) as response:
                refreshed = json.load(response)

            self.assertEqual(initial["summary"]["total"], 0)
            self.assertEqual(refreshed["summary"]["total"], 1)
            self.assertEqual(refreshed["assets"][0]["relative_path"], "graphics/conditions/sunny/day/new.png")
            self.assertFalse(refreshed["assets"][0]["valid"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_music_volume_can_be_saved_through_dashboard_api(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            request = Request(
                f"http://127.0.0.1:{port}/api/music/settings",
                data=json.dumps({"enabled": True, "volume": 0.42}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                saved = json.load(response)
            with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
                status = json.load(response)

            self.assertEqual(saved["music"]["volume"], 0.42)
            self.assertEqual(status["music_settings"]["volume"], 0.42)
            self.assertEqual(load_settings(paths)["music"]["volume"], 0.42)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_published_channel_and_range_video_are_served(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        version = paths.versions / "fixture-version"
        version.mkdir(parents=True)
        (version / "weather-channel.mp4").write_bytes(b"0123456789" * 100)
        manifest = {
            "schema_version": 1,
            "channel": {"slug": "weather", "display_name": "RetroWeather"},
            "generation_id": "fixture-version",
            "generated_at": "2026-08-06T12:00:00-07:00",
            "valid_until": "2026-08-06T12:30:00-07:00",
            "timezone": "America/Los_Angeles",
            "video": "weather-channel.mp4",
            "duration_seconds": 240.0,
            "attribution": ["Fixture"],
            "segments": [],
        }
        (version / "channel.json").write_text(json.dumps(manifest), encoding="utf-8")
        (paths.output / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "fixture-version",
                    "manifest": "versions/fixture-version/channel.json",
                }
            ),
            encoding="utf-8",
        )
        server = create_server(paths, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/channel", timeout=3) as response:
                active = json.load(response)
            request = Request(
                f"http://127.0.0.1:{port}/channel/video",
                headers={"Range": "bytes=100-199"},
            )
            with urlopen(request, timeout=3) as response:
                video_slice = response.read()
                status = response.status
                content_range = response.headers["Content-Range"]

            self.assertEqual(active["generation_id"], "fixture-version")
            self.assertEqual(status, 206)
            self.assertEqual(len(video_slice), 100)
            self.assertEqual(content_range, "bytes 100-199/1000")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_manual_job_and_output_folder_endpoints(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        jobs = FixtureJobManager()
        opened: list[Path] = []
        server = create_server(
            paths,
            host="127.0.0.1",
            port=0,
            job_manager=jobs,
            output_opener=lambda path: opened.append(path),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for route, expected_action in (
                ("/api/channel/render", "render"),
                ("/api/channel/fetch-render", "fetch-render"),
                ("/api/weather/fetch", "fetch"),
            ):
                request = Request(
                    f"http://127.0.0.1:{port}{route}", data=b"", method="POST"
                )
                with urlopen(request, timeout=3) as response:
                    payload = json.load(response)
                    self.assertEqual(response.status, 202)
                    self.assertEqual(payload["action"], expected_action)
            with urlopen(f"http://127.0.0.1:{port}/api/channel/job", timeout=3) as response:
                status = json.load(response)
            open_request = Request(
                f"http://127.0.0.1:{port}/api/output/open", data=b"", method="POST"
            )
            with urlopen(open_request, timeout=3) as response:
                opened_payload = json.load(response)
            media_request = Request(
                f"http://127.0.0.1:{port}/api/media/open", data=b"", method="POST"
            )
            with urlopen(media_request, timeout=3) as response:
                media_payload = json.load(response)

            self.assertEqual(jobs.actions, ["render", "fetch-render", "fetch"])
            self.assertEqual(status["stage"], "Ready")
            self.assertEqual(opened, [paths.output, paths.media])
            self.assertEqual(opened_payload["path"], str(paths.output))
            self.assertEqual(media_payload["path"], str(paths.media))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def _wait_for_job(port: int, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with urlopen(f"http://127.0.0.1:{port}/api/channel/job", timeout=3) as response:
            payload = json.load(response)
        if payload["state"] != "running":
            return payload
        time.sleep(0.02)
    raise AssertionError("Channel job did not finish in time")


if __name__ == "__main__":
    import unittest

    unittest.main()
