import shutil
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root
from retrotv.maintenance_window import MaintenanceWindowSchedule, WindowOccurrence
from retrotv.runtime import ApplicationRuntime


class MaintenanceWindowTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        self.root = project / ".test-tmp" / f"maintenance-window-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        media = self.root / "media"
        media.mkdir()
        self.config = AppConfig(
            self.root / "test.toml", project,
            {
                "application": {"database_path": str(self.root / "retrotv.sqlite3"), "timezone": "America/Los_Angeles"},
                "storage_roots": {
                    "media": str(media), "state": str(self.root / "state"),
                    "normalized": str(self.root / "normalized"),
                    "previews": str(self.root / "previews"),
                    "originals_backup": str(self.root / "originals"),
                },
                "database_backups": {"directory": str(self.root / "backups"), "retention_count": 2},
                "maintenance_window": {"enabled": True, "start": "23:00", "end": "05:00"},
                "application_layer": {"startup_channel": "fox", "start_tv_on_launch": True},
                "guide": {"channel_number": 2, "channel_numbers": {"fox": 5}},
                "player": {"mpv": "mpv"},
                "synthetic_channels": {"weather": {"enabled": False}},
            },
        )
        with connect_database(self.config.database_path) as connection:
            apply_migrations(connection, project / "migrations")
            connection.execute("INSERT INTO channels(slug,display_name,channel_number) VALUES ('fox','FOX',5)")
            ensure_storage_root(connection, "media")
            connection.commit()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cross_midnight_window_uses_starting_day_as_occurrence(self):
        schedule = MaintenanceWindowSchedule(self.config)
        zone = ZoneInfo("America/Los_Angeles")
        before_midnight = schedule.active(datetime(2033, 5, 18, 23, 30, tzinfo=zone))
        after_midnight = schedule.active(datetime(2033, 5, 19, 2, 0, tzinfo=zone))
        self.assertEqual(before_midnight.occurrence_date, "2033-05-18")
        self.assertEqual(after_midnight.occurrence_date, "2033-05-18")
        self.assertEqual(after_midnight.ends_at.hour, 5)

    def test_completed_early_occurrence_resumes_once_and_does_not_reopen(self):
        runtime = ApplicationRuntime(self.config)
        zone = ZoneInfo("America/Los_Angeles")
        occurrence = WindowOccurrence(
            "2033-05-18", datetime(2033, 5, 18, 3, tzinfo=zone),
            datetime(2033, 5, 18, 5, tzinfo=zone),
        )
        runtime.maintenance_window_state = {
            "occurrence_date": occurrence.occurrence_date,
            "state": "active", "pipeline_started": True,
            "auto_resume": True, "resume_channel": "fox",
        }
        runtime.readiness = {"state": "ready", "stage": None, "error": None}
        runtime.readiness_thread = type("Finished", (), {"is_alive": lambda self: False})()
        with patch.object(runtime.maintenance_window_schedule, "active", return_value=occurrence), patch.object(
            runtime, "start_tv"
        ) as start:
            self.assertTrue(runtime._check_maintenance_window())
            self.assertEqual(runtime.maintenance_window_state["state"], "completed_early")
            start.assert_called_once_with("fox", qualifying_activity=False)
            runtime._check_maintenance_window()
            start.assert_called_once()

    def test_manual_start_marks_current_window_skipped(self):
        runtime = ApplicationRuntime(self.config)
        zone = ZoneInfo("America/Los_Angeles")
        occurrence = WindowOccurrence(
            "2033-05-18", datetime(2033, 5, 18, 3, tzinfo=zone),
            datetime(2033, 5, 18, 5, tzinfo=zone),
        )
        runtime.maintenance_window_state = {
            "occurrence_date": occurrence.occurrence_date,
            "state": "active", "auto_resume": True, "resume_channel": "fox",
        }
        with patch.object(runtime.maintenance_window_schedule, "active", return_value=occurrence):
            self.assertTrue(runtime.skip_current_maintenance_window(start_tv=False))
        self.assertEqual(runtime.maintenance_window_state["state"], "skipped")
        self.assertFalse(runtime.maintenance_window_state["auto_resume"])


if __name__ == "__main__":
    unittest.main()
