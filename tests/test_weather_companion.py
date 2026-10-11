import shutil
import unittest
import uuid
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.weather_companion import WeatherCompanionManager


class FakeProcess:
    def __init__(self, pid=4321, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class WeatherCompanionTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        base = project / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temporary = base / f"weather-companion-{uuid.uuid4().hex}"
        self.home = self.temporary / "RetroWeather"
        (self.home / "config").mkdir(parents=True)
        (self.home / "config" / "settings.json").write_text("{}", encoding="utf-8")
        self.clock = [100.0]
        self.processes = []
        self.config = AppConfig(
            self.temporary / "test.toml",
            project,
            {
                "application": {"database_path": str(self.temporary / "db.sqlite3")},
                "storage_roots": {"media": str(self.temporary / "media")},
                "synthetic_channels": {
                    "weather": {
                        "enabled": True,
                        "manage_companion": True,
                        "companion_home": str(self.home),
                        "companion_host": "127.0.0.1",
                        "companion_port": 8775,
                        "companion_startup_timeout_seconds": 10,
                        "companion_restart_initial_seconds": 2,
                        "companion_restart_max_seconds": 8,
                    }
                },
            },
        )

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def manager(self, *, publication=None):
        def factory(*args, **kwargs):
            process = FakeProcess(pid=5000 + len(self.processes))
            self.processes.append((process, args, kwargs))
            return process

        return WeatherCompanionManager(
            self.config,
            publication_probe=(lambda: publication) if publication is not None else None,
            process_factory=factory,
            monotonic=lambda: self.clock[0],
        )

    @staticmethod
    def healthy_status(*, stale=False, published=True):
        return {
            "schema_version": 1,
            "version": "0.1.0",
            "weather_snapshot": True,
            "published_channel": published,
            "weather_status": {"snapshot_valid": True, "stale": stale},
            "scheduler": {"state": "sleeping"},
        }

    def test_existing_healthy_companion_is_not_duplicated_or_owned(self):
        manager = self.manager(publication={"ready": True, "stale": False})
        manager._get_json = lambda path: (
            self.healthy_status() if path == "/api/status" else {"state": "idle"}
        )
        manager.poll_once()
        self.assertEqual(self.processes, [])
        self.assertFalse(manager.status()["owned"])
        self.assertEqual(manager.status()["state"], "running")

    def test_missing_location_requires_setup_without_invalid_fetch_requests(self):
        manager = self.manager(publication={"ready": False, "stale": False})
        remote = self.healthy_status(published=False)
        remote["selected_location"] = None
        manager._get_json = lambda path: remote if path == "/api/status" else {"state": "idle"}
        posts = []
        manager._post_json = lambda path: posts.append(path)
        manager.poll_once()
        manager.poll_once()
        self.assertEqual(posts, [])
        self.assertEqual(manager.status()["state"], "setup_required")
        self.assertTrue(manager.status()["setup_required"])
        self.assertTrue(manager.status()["healthy"])
        remote["selected_location"] = {"location": {"display_name": "Selected city"}}
        manager.poll_once()
        self.assertFalse(manager.status()["setup_required"])
        self.assertEqual(posts, ["/api/channel/fetch-render"])

    def test_different_retroweather_home_on_port_is_not_duplicated(self):
        manager = self.manager()
        remote = self.healthy_status()
        remote["home"] = str(self.temporary / "DifferentWeather")
        manager._get_json = lambda path: remote
        manager.poll_once()
        self.assertEqual(self.processes, [])
        self.assertEqual(manager.status()["state"], "unavailable")
        self.assertIn("differentweather", manager.status()["error"].casefold())

    def test_missing_companion_launches_silently_then_becomes_healthy(self):
        manager = self.manager(publication={"ready": True, "stale": False})
        manager._get_json = lambda path: (_ for _ in ()).throw(OSError("offline"))
        manager.poll_once()
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(manager.status()["owned"])
        command = self.processes[0][1][0]
        self.assertIn("retroweather", command)
        self.assertEqual(command[-1], "8775")

        manager._get_json = lambda path: (
            self.healthy_status() if path == "/api/status" else {"state": "idle"}
        )
        manager.poll_once()
        self.assertTrue(manager.status()["healthy"])
        self.assertTrue(manager.status()["owned"])

    def test_stale_startup_requests_fetch_and_render_without_blocking_publication(self):
        manager = self.manager(publication={"ready": True, "stale": True})
        manager._get_json = lambda path: (
            self.healthy_status(stale=True) if path == "/api/status" else {"state": "idle"}
        )
        posts = []
        manager._post_json = lambda path: posts.append(path) or {"state": "running"}
        manager.poll_once()
        self.assertEqual(posts, ["/api/channel/fetch-render"])
        self.assertEqual(manager.status()["state"], "updating")
        self.assertTrue(manager.status()["healthy"])

    def test_stop_closes_only_an_owned_process(self):
        manager = self.manager()
        process = FakeProcess()
        manager.process = process
        manager.owned = True
        posts = []
        manager._post_json = lambda path: posts.append(path) or {"ok": True}
        manager.stop()
        self.assertEqual(posts, ["/api/shutdown"])
        self.assertEqual(process.returncode, 0)

        independent = self.manager()
        independent._post_json = lambda path: self.fail("Independent process was stopped")
        independent.stop()

    def test_crash_recovery_uses_bounded_backoff(self):
        manager = self.manager()
        manager.process = FakeProcess(returncode=7)
        manager.owned = True
        manager._get_json = lambda path: (_ for _ in ()).throw(OSError("offline"))
        manager.poll_once()
        self.assertEqual(manager.status()["restart_failures"], 1)
        self.assertEqual(len(self.processes), 0)
        manager.poll_once()
        self.assertEqual(len(self.processes), 0)
        self.clock[0] += 2
        manager.poll_once()
        self.assertEqual(len(self.processes), 1)


if __name__ == "__main__":
    unittest.main()
