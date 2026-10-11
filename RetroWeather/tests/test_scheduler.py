from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, initialize, load_settings
from retroweather.locations import save_selected_location
from retroweather.models import NormalizedWeatherSnapshot
from retroweather.providers.open_meteo import LocationSearchResult
from retroweather.web.scheduler import RefreshScheduler, cleanup_storage


FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_PAYLOAD = json.loads(
    (FIXTURES / "normalized-weather.json").read_text(encoding="utf-8")
)
LOCATION_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-get.json").read_text(encoding="utf-8")
)


class FakeJobs:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.busy = False
        self.result = {
            "state": "idle",
            "started_at": None,
            "finished_at": None,
            "weather_changed": None,
            "error": None,
        }

    def start(self, action: str):
        self.actions.append(action)
        self.busy = True
        started = f"job-{len(self.actions)}"
        self.result = {
            "state": "running",
            "action": action,
            "started_at": started,
            "finished_at": None,
            "weather_changed": None,
            "error": None,
        }
        return dict(self.result)

    def is_busy(self):
        return self.busy

    def status(self):
        return dict(self.result)

    def complete(self, *, changed: bool | None = None, error: str | None = None):
        self.busy = False
        self.result.update(
            {
                "state": "failed" if error else "success",
                "finished_at": "complete",
                "weather_changed": changed,
                "error": error,
            }
        )


class RefreshSchedulerTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = AppPaths.discover(self.temporary)
        initialize(self.paths)
        selection = LocationSearchResult.from_api(LOCATION_PAYLOAD).to_selection()
        save_selected_location(self.paths, selection)
        self.now = datetime.fromisoformat("2026-08-06T15:00:00-07:00")

    def test_missing_weather_and_publication_start_one_combined_job(self):
        jobs = FakeJobs()
        scheduler = RefreshScheduler(self.paths, jobs, clock=lambda: self.now)

        status = scheduler.run_once()

        self.assertEqual(jobs.actions, ["fetch-render"])
        self.assertEqual(status["state"], "job_running")
        self.assertEqual(status["pending_action"], "fetch-render")
        self.assertTrue(self.paths.scheduler_status.is_file())

    def test_material_fetch_change_triggers_render_before_interval(self):
        old_snapshot = NormalizedWeatherSnapshot.from_dict(SNAPSHOT_PAYLOAD)
        old_snapshot.save(self.paths.latest_snapshot)
        self._publish("fresh-channel", self.now - timedelta(minutes=10))
        jobs = FakeJobs()
        scheduler = RefreshScheduler(self.paths, jobs, clock=lambda: self.now)

        scheduler.run_once()
        self.assertEqual(jobs.actions, ["fetch"])

        updated = json.loads(json.dumps(SNAPSHOT_PAYLOAD))
        updated["current"]["temperature"] = 81.0
        updated["metadata"]["fetched_at"] = self.now.isoformat()
        updated["metadata"]["expires_at"] = (self.now + timedelta(minutes=15)).isoformat()
        NormalizedWeatherSnapshot.from_dict(updated).save(self.paths.latest_snapshot)
        jobs.complete(changed=True)

        status = scheduler.run_once(self.now + timedelta(seconds=1))

        self.assertEqual(jobs.actions, ["fetch", "render"])
        self.assertEqual(status["pending_action"], "render")

    def test_failures_use_bounded_retry_without_duplicate_jobs(self):
        jobs = FakeJobs()
        scheduler = RefreshScheduler(self.paths, jobs, clock=lambda: self.now)
        scheduler.run_once()
        jobs.complete(error="fixture outage")

        backing_off = scheduler.run_once(self.now + timedelta(seconds=1))
        scheduler.run_once(self.now + timedelta(seconds=30))

        self.assertEqual(jobs.actions, ["fetch-render"])
        self.assertEqual(backing_off["state"], "backing_off")
        self.assertEqual(backing_off["consecutive_failures"], 1)
        self.assertEqual(
            backing_off["next_retry_at"],
            (self.now + timedelta(seconds=61)).astimezone(timezone.utc).isoformat(),
        )

        scheduler.run_once(self.now + timedelta(seconds=61))
        self.assertEqual(jobs.actions, ["fetch-render", "fetch-render"])

    def test_disabled_scheduler_and_missing_location_do_not_start_jobs(self):
        settings = load_settings(self.paths)
        settings["refresh"]["automatic"] = False
        self.paths.settings.write_text(json.dumps(settings), encoding="utf-8")
        jobs = FakeJobs()
        scheduler = RefreshScheduler(self.paths, jobs, clock=lambda: self.now)
        self.assertEqual(scheduler.run_once()["state"], "disabled")
        self.assertEqual(jobs.actions, [])

        settings["refresh"]["automatic"] = True
        settings["location"] = None
        self.paths.settings.write_text(json.dumps(settings), encoding="utf-8")
        self.assertEqual(scheduler.run_once()["state"], "waiting_for_location")
        self.assertEqual(jobs.actions, [])

    def test_cleanup_retains_active_and_newest_versions(self):
        for name in ("v1", "v2", "v3", "v4", "v5"):
            (self.paths.versions / name).mkdir()
        self._publish("v1", self.now)
        settings = load_settings(self.paths)
        settings["retention"]["valid_versions"] = 3

        removed = cleanup_storage(self.paths, settings, now=self.now)

        self.assertEqual(removed["versions"], 1)
        self.assertEqual(
            {item.name for item in self.paths.versions.iterdir()},
            {"v1", "v3", "v4", "v5"},
        )

    def _publish(self, generation: str, generated_at: datetime) -> None:
        version = self.paths.versions / generation
        version.mkdir(exist_ok=True)
        manifest = {
            "schema_version": 1,
            "generation_id": generation,
            "generated_at": generated_at.isoformat(),
        }
        (version / "channel.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.paths.output / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": generation,
                    "manifest": f"versions/{generation}/channel.json",
                }
            ),
            encoding="utf-8",
        )
