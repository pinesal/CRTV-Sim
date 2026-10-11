import shutil
import os
import sqlite3
import unittest
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.database_backup import DatabaseBackupError, DatabaseBackupService
from retrotv.db import apply_migrations, connect_database


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        test_temp = project / ".test-tmp"
        test_temp.mkdir(exist_ok=True)
        self.root = test_temp / f"database-backup-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.config = AppConfig(
            profile_path=self.root / "test.toml",
            project_root=project,
            data={
                "application": {
                    "database_path": str(self.root / "retrotv.sqlite3"),
                    "timezone": "UTC",
                },
                "storage_roots": {"media": str(self.root / "media")},
                "database_backups": {
                    "enabled": True,
                    "directory": "backups",
                    "retention_count": 2,
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        self.connection.execute(
            "INSERT INTO channels(slug, display_name, enabled) VALUES ('fox', 'FOX', 1)"
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_daily_backup_is_verified_atomic_and_rotated(self):
        service = DatabaseBackupService(self.config)
        first = service.create(
            self.connection, now=datetime(2026, 8, 23, 12, tzinfo=UTC)
        )
        self.assertTrue(first.created)
        self.assertFalse(first.skipped)
        first_path = Path(str(first.path))
        self.assertTrue(first_path.is_file())
        self.assertFalse(first_path.with_name(first_path.name + ".partial").exists())
        with closing(sqlite3.connect(first_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT count(*) FROM channels").fetchone()[0], 1)

        skipped = service.create(
            self.connection, now=datetime(2026, 8, 23, 18, tzinfo=UTC)
        )
        self.assertTrue(skipped.skipped)
        self.assertEqual(skipped.reason, "Today's verified backup already exists")

        service.create(self.connection, now=datetime(2026, 8, 24, 12, tzinfo=UTC))
        third = service.create(
            self.connection, now=datetime(2026, 8, 25, 12, tzinfo=UTC)
        )
        self.assertEqual(third.removed_count, 1)
        self.assertEqual(
            [path.name for path in service._daily_backups(service.backup_directory)],
            ["retrotv-2026-08-24.sqlite3", "retrotv-2026-08-25.sqlite3"],
        )

    def test_forced_backup_refreshes_todays_snapshot(self):
        service = DatabaseBackupService(self.config)
        now = datetime(2026, 8, 25, 12, tzinfo=UTC)
        first = service.create(self.connection, now=now)
        self.connection.execute(
            "INSERT INTO channels(slug, display_name, enabled) VALUES ('mtv', 'MTV', 1)"
        )
        self.connection.commit()

        refreshed = service.create(self.connection, force=True, now=now)
        self.assertTrue(refreshed.created)
        with closing(sqlite3.connect(Path(str(first.path)))) as backup:
            self.assertEqual(backup.execute("SELECT count(*) FROM channels").fetchone()[0], 2)

    def test_invalid_snapshot_is_not_published(self):
        service = DatabaseBackupService(self.config)
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.connection.execute(
            "INSERT INTO normalization_profiles(asset_id) VALUES (999999)"
        )
        self.connection.commit()

        with self.assertRaisesRegex(DatabaseBackupError, "foreign-key"):
            service.create(
                self.connection, now=datetime(2026, 8, 25, 12, tzinfo=UTC)
            )
        self.assertFalse(
            (service.backup_directory / "retrotv-2026-08-25.sqlite3").exists()
        )
        self.assertFalse(
            (
                service.backup_directory
                / "retrotv-2026-08-25.sqlite3.partial"
            ).exists()
        )

    def test_shutdown_reuses_recent_verified_backup_across_midnight_without_scans(self):
        service = DatabaseBackupService(self.config)
        made = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)
        first = service.create(self.connection, now=made)
        path = Path(first.path)
        os.utime(path, (made.timestamp(), made.timestamp()))
        self.connection.execute(
            "INSERT INTO channels(slug, display_name, enabled) VALUES ('mtv', 'MTV', 1)"
        )
        self.connection.commit()
        with patch.object(service, "_check", side_effect=AssertionError("No full scan needed")):
            reused = service.create_for_shutdown(
                self.connection, now=made + timedelta(hours=1)
            )
        self.assertTrue(reused.skipped)
        self.assertFalse(reused.created)
        self.assertEqual(reused.path, first.path)
        self.assertEqual(path.stat().st_mtime, made.timestamp())
        with closing(sqlite3.connect(path)) as backup:
            self.assertEqual(backup.execute("SELECT count(*) FROM channels").fetchone()[0], 1)

    def test_shutdown_backup_expires_after_24_hours(self):
        service = DatabaseBackupService(self.config)
        made = datetime(2026, 10, 9, 12, tzinfo=UTC)
        first = service.create(self.connection, now=made)
        os.utime(first.path, (made.timestamp(), made.timestamp()))
        self.assertTrue(service.create_for_shutdown(
            self.connection, now=made + timedelta(hours=24)
        ).skipped)
        with patch.object(service, "create", return_value="fresh") as create:
            now = made + timedelta(hours=24, seconds=1)
            self.assertEqual(service.create_for_shutdown(self.connection, now=now), "fresh")
            create.assert_called_once_with(self.connection, force=True, now=now)

    def test_shutdown_ignores_partial_empty_and_future_backups(self):
        service = DatabaseBackupService(self.config)
        now = datetime(2026, 10, 10, 12, tzinfo=UTC)
        directory = service.backup_directory
        directory.mkdir()
        for name, contents, modified in (
            ("retrotv-2026-10-10.sqlite3.partial", b"unfinished", now),
            ("retrotv-2026-10-09.sqlite3", b"", now),
            ("retrotv-2026-10-11.sqlite3", b"future", now + timedelta(hours=1)),
        ):
            path = directory / name
            path.write_bytes(contents)
            os.utime(path, (modified.timestamp(), modified.timestamp()))
        with patch.object(service, "create", return_value="fresh") as create:
            self.assertEqual(service.create_for_shutdown(self.connection, now=now), "fresh")
            create.assert_called_once_with(self.connection, force=True, now=now)

    def test_shutdown_uses_modification_age_not_filename_order(self):
        service = DatabaseBackupService(self.config)
        now = datetime(2026, 10, 10, 12, tzinfo=UTC)
        first = service.create(self.connection, now=now - timedelta(days=1))
        second = service.create(self.connection, now=now)
        os.utime(first.path, (now.timestamp() - 30, now.timestamp() - 30))
        os.utime(second.path, (now.timestamp() - 60, now.timestamp() - 60))
        self.assertEqual(service.create_for_shutdown(self.connection, now=now).path, first.path)


if __name__ == "__main__":
    unittest.main()
