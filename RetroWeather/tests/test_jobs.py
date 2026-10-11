from __future__ import annotations

import json
import threading
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, initialize
from retroweather.models import NormalizedWeatherSnapshot
from retroweather.rendering import RenderResult
from retroweather.web.jobs import ChannelJobManager, JobBusyError


class ChannelJobTests(WorkspaceTestCase):
    def test_render_runs_off_request_thread_and_persists_success(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        entered = threading.Event()
        release = threading.Event()

        def render(app_paths, progress):
            progress("Rendering fixture", 50)
            entered.set()
            release.wait(3)
            version = app_paths.versions / "job-generation"
            return RenderResult(
                "job-generation",
                version,
                version / "weather-channel.mp4",
                version / "channel.json",
                120.0,
            )

        manager = ChannelJobManager(
            paths,
            weather_client=object(),
            render_function=render,
        )

        started = manager.start("render")
        self.assertEqual(started["state"], "running")
        self.assertTrue(entered.wait(1))
        self.assertEqual(manager.status()["progress"], 50)
        with self.assertRaises(JobBusyError):
            manager.start("render")
        release.set()
        completed = manager.wait()

        self.assertEqual(completed["state"], "success")
        self.assertEqual(completed["generation_id"], "job-generation")
        self.assertEqual(completed["progress"], 100)
        self.assertIn("Passed manifest", completed["validation_result"])
        self.assertTrue(paths.render_status.is_file())

        restored = ChannelJobManager(paths, weather_client=object())
        self.assertEqual(restored.status()["state"], "success")
        self.assertEqual(restored.status()["generation_id"], "job-generation")
        self.assertIn("Passed manifest", restored.status()["validation_result"])

    def test_fetch_and_render_runs_in_order_and_reports_failure(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        events: list[str] = []

        def fetch(app_paths, client):
            events.append("fetch")

        def render(app_paths, progress):
            events.append("render")
            raise RuntimeError("fixture render failure")

        manager = ChannelJobManager(
            paths,
            weather_client=object(),
            render_function=render,
            fetch_function=fetch,
        )

        manager.start("fetch-render")
        completed = manager.wait()

        self.assertEqual(events, ["fetch", "render"])
        self.assertEqual(completed["state"], "failed")
        self.assertIn("fixture render failure", completed["error"])
        self.assertIsNotNone(completed["finished_at"])

    def test_fetch_reports_only_material_display_changes(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        fixture = Path(__file__).parent / "fixtures" / "normalized-weather.json"
        original = json.loads(fixture.read_text(encoding="utf-8"))
        NormalizedWeatherSnapshot.from_dict(original).save(paths.latest_snapshot)

        changed = json.loads(json.dumps(original))
        changed["current"]["temperature"] = 82.0

        def fetch(app_paths, client):
            NormalizedWeatherSnapshot.from_dict(changed).save(app_paths.latest_snapshot)

        manager = ChannelJobManager(
            paths,
            weather_client=object(),
            fetch_function=fetch,
        )
        manager.start("fetch")
        first = manager.wait()
        manager.start("fetch")
        second = manager.wait()

        self.assertTrue(first["weather_changed"])
        self.assertFalse(second["weather_changed"])
