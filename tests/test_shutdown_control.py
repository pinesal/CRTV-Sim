import shutil
import threading
import unittest
import uuid
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.runtime import ApplicationRuntime
from retrotv.shutdown_control import ShutdownControl


class ShutdownControlTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        root = project / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temporary = root / f"shutdown-control-{uuid.uuid4().hex}"
        self.temporary.mkdir()
        self.control = ShutdownControl(self.temporary / "state")

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_request_and_status_round_trip(self):
        self.control.begin_session(dashboard_url="http://127.0.0.1:8765")
        request_id = self.control.submit_request(requester_pid=42)
        request = self.control.read_request()
        self.assertEqual(request["request_id"], request_id)
        self.assertEqual(request["requester_pid"], 42)

        self.control.write_status(
            request_id,
            state="safe",
            message="done",
            shutdown={"safe_to_remove": True},
        )
        status = self.control.read_status(request_id)
        self.assertEqual(status["state"], "safe")
        self.assertTrue(status["shutdown"]["safe_to_remove"])

    def test_runtime_watcher_processes_drive_local_request(self):
        project = Path(__file__).resolve().parents[1]
        config = AppConfig(
            self.temporary / "test.toml",
            project,
            {
                "application": {
                    "database_path": str(self.temporary / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {
                    "media": str(self.temporary / "media"),
                    "state": str(self.temporary / "state"),
                    "normalized": str(self.temporary / "normalized"),
                    "previews": str(self.temporary / "previews"),
                    "originals_backup": str(self.temporary / "backup"),
                },
            },
        )
        runtime = ApplicationRuntime(config)
        runtime.shutdown_control.begin_session(
            dashboard_url="http://127.0.0.1:8765"
        )
        runtime.shutdown = lambda: {
            "state": "safe",
            "safe_to_remove": True,
            "error": None,
        }
        worker = threading.Thread(target=runtime._shutdown_request_worker)
        worker.start()
        request_id = runtime.shutdown_control.submit_request(requester_pid=42)
        worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        status = runtime.shutdown_control.read_status(request_id)
        self.assertEqual(status["state"], "safe")
        self.assertTrue(status["shutdown"]["safe_to_remove"])


if __name__ == "__main__":
    unittest.main()
