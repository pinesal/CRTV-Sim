import shutil
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.database_maintenance import DatabaseCompactionService, DatabaseHousekeepingService
from retrotv.db import apply_migrations, connect_database, ensure_storage_root
from retrotv.scheduling import ScheduleBuilder


class DatabaseHousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / ".test-tmp" / f"database-maintenance-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        for name in ("media", "state", "normalized", "previews", "originals", "backups"):
            (self.root / name).mkdir()
        self.config = AppConfig(
            self.root / "test.toml", self.project,
            {
                "application": {"database_path": str(self.root / "retrotv.sqlite3"), "timezone": "UTC"},
                "storage_roots": {name: str(self.root / name) for name in ("media", "state", "normalized", "previews")}
                    | {"originals_backup": str(self.root / "originals")},
                "database_backups": {"enabled": True, "directory": str(self.root / "backups"), "retention_count": 3},
                "database_housekeeping": {
                    "enabled": True, "schedule_retention_days": 30,
                    "operation_retention_days": 30, "missing_asset_retention_days": 90,
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        self.connection.execute("INSERT INTO channels(id,slug,display_name) VALUES (1,'fox','FOX')")
        self.root_id = ensure_storage_root(self.connection, "media")
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _asset(self, name, seen, status="ready"):
        cursor = self.connection.execute(
            """INSERT INTO assets(source_root_id,source_relative_path,original_filename,
                   file_size,modified_ns,integrity_status,review_status,first_seen_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (self.root_id, name, name, 1, 1, status, "ignored" if status == "missing" else "ready", seen, seen),
        )
        return int(cursor.lastrowid)

    def test_purge_checkpoints_schedule_and_respects_missing_retention(self):
        now = datetime(2033, 5, 18, 12, tzinfo=UTC)
        old = (now - timedelta(days=120)).isoformat()
        recent = (now - timedelta(days=10)).isoformat()
        scheduled = self._asset("scheduled.mp4", old)
        old_missing = self._asset("old-missing.mp4", old, "missing")
        recent_missing = self._asset("recent-missing.mp4", recent, "missing")
        self.connection.execute(
            """INSERT INTO schedule_generations(
                   id,channel_id,schedule_date,seed,configuration_fingerprint,status,
                   validation_status,generated_at,locked)
               VALUES (1,1,'2033-03-01','seed','fingerprint','active','valid',?,0)""",
            (old,),
        )
        self.connection.execute(
            """INSERT INTO recurring_lineup_snapshots(
                   generation_id,channel_id,schedule_date,state_json,state_hash,
                   input_state_hash,updated_at
               ) VALUES (1,1,'2033-03-01','{"version":1,"queues":{"morning":[]}}',
                         'state-hash','input-hash',?)""",
            (old,),
        )
        self.connection.execute(
            """INSERT INTO schedule_entries(
                   generation_id,channel_id,asset_id,starts_at,ends_at,media_start_ms,
                   media_end_ms,entry_type,title,daypart_slug,sequence_position,is_program)
               VALUES (1,1,?,'2033-03-01T12:00:00+00:00','2033-03-01T12:30:00+00:00',0,
                       1800000,'episode','A Show','all-day',0,1)""",
            (scheduled,),
        )
        self.connection.commit()

        result = DatabaseHousekeepingService(self.connection, self.config).run(
            force=True, now=now
        )

        self.assertEqual(result.state, "complete", result.error)
        self.assertEqual(result.rows_removed["schedule_generations"], 1)
        checkpoint = self.connection.execute(
            "SELECT context_key,asset_id FROM schedule_history_checkpoints"
        ).fetchone()
        self.assertEqual(checkpoint["context_key"], "scheduled.mp4")
        self.assertEqual(checkpoint["asset_id"], scheduled)
        recurring_checkpoint = self.connection.execute(
            "SELECT checkpoint_date,state_json FROM recurring_lineup_checkpoints WHERE channel_id=1"
        ).fetchone()
        self.assertEqual(recurring_checkpoint["checkpoint_date"], "2033-03-01")
        self.assertIn('"queues":{"morning":[]}', recurring_checkpoint["state_json"])
        self.assertIsNone(self.connection.execute("SELECT id FROM assets WHERE id=?", (old_missing,)).fetchone())
        self.assertIsNotNone(self.connection.execute("SELECT id FROM assets WHERE id=?", (recent_missing,)).fetchone())
        self.assertTrue(Path(result.backup["path"]).is_file())
        scheduler = ScheduleBuilder.__new__(ScheduleBuilder)
        scheduler.connection = self.connection
        self.assertEqual(
            scheduler._previous_programs(1, now).get("scheduled.mp4"), scheduled
        )

    def test_compaction_creates_backup_and_preserves_integrity(self):
        self.connection.execute("CREATE TABLE bulky(value TEXT)")
        self.connection.executemany(
            "INSERT INTO bulky(value) VALUES (?)", (("x" * 1000,) for _ in range(500))
        )
        self.connection.commit()
        self.connection.execute("DELETE FROM bulky")
        self.connection.commit()
        self.connection.close()
        service = DatabaseCompactionService(self.config)
        self.assertGreaterEqual(service.preview()["reclaimable_bytes"], 0)
        result = service.run()
        self.assertEqual(result["state"], "complete")
        self.assertTrue(Path(result["backup"]["path"]).is_file())
        self.connection = connect_database(self.config.database_path)
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
