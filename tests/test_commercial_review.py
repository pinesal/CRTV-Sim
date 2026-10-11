import shutil
import unittest
import uuid
from pathlib import Path

from retrotv.commercial_review import CommercialReviewService
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now


class CommercialReviewTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        self.root = project / ".test-tmp" / f"commercial-review-{uuid.uuid4().hex}"
        self.media = self.root / "media"
        self.path = self.media / "fox" / "night" / "commercials" / "long.mp4"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"commercial one")
        self.config = AppConfig(
            self.root / "test.toml", project,
            {"application": {"database_path": str(self.root / "db.sqlite3")},
             "storage_roots": {"media": str(self.media)},
             "library": {"long_commercial_minutes": 3},
             "seasonal": {"holidays": {}},
             "scheduling": {"dayparts": [{"slug": "night"}]}}
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        root_id = ensure_storage_root(self.connection, "media")
        now = utc_now()
        self.connection.execute(
            "INSERT INTO channels(slug,display_name,enabled) VALUES ('fox','Fox',1)"
        )
        self.connection.execute(
            """INSERT INTO assets(id,source_root_id,source_relative_path,
                 original_filename,fingerprint,file_size,modified_ns,
                 media_type,display_title,
                 channel_slug,daypart_slug,metadata_status,integrity_status,
                 review_status,enabled_for_scheduling,first_seen_at,last_seen_at)
               VALUES (1,?,'fox/night/commercials/long.mp4','long.mp4','hash-one',14,1,
                 'commercial','Long','fox','night','ready','ready','ready',1,?,?)""",
            (root_id, now, now),
        )
        self.connection.execute(
            "INSERT INTO asset_media_info(asset_id,duration_seconds) VALUES (1,181)"
        )
        self.connection.execute(
            """INSERT INTO asset_eligibility(asset_id,channel_slug,daypart_slug,
                 eligibility_source) VALUES (1,'fox','night','channel_daypart_folder')"""
        )
        self.connection.commit()
        self.service = CommercialReviewService(self.connection, self.config)

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_threshold_review_approval_and_content_change(self):
        self.service.reconcile()
        row = self.connection.execute(
            "SELECT review_status,enabled_for_scheduling FROM assets WHERE id=1"
        ).fetchone()
        self.assertEqual(tuple(row), ("open", 0))
        self.service.decide(1, "approved")
        self.assertEqual(
            self.connection.execute("SELECT enabled_for_scheduling FROM assets WHERE id=1").fetchone()[0], 1
        )
        self.connection.execute(
            "UPDATE assets SET source_relative_path='fox/night/commercials/renamed.mp4' WHERE id=1"
        )
        self.connection.commit()
        self.service.reconcile()
        self.assertEqual(
            self.connection.execute("SELECT decision FROM commercial_duration_reviews WHERE asset_id=1").fetchone()[0],
            "approved",
        )
        self.connection.execute("UPDATE assets SET fingerprint='hash-two' WHERE id=1")
        self.connection.commit()
        self.service.reconcile()
        self.assertEqual(
            self.connection.execute("SELECT decision FROM commercial_duration_reviews WHERE asset_id=1").fetchone()[0],
            "pending",
        )

    def test_exact_threshold_is_not_flagged(self):
        self.connection.execute("UPDATE asset_media_info SET duration_seconds=180 WHERE asset_id=1")
        self.connection.commit()
        self.service.reconcile()
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM commercial_duration_reviews WHERE asset_id=1"
        ).fetchone())

    def test_move_reclassifies_without_changing_asset_id(self):
        self.service.reconcile()
        moved = self.service.move_to_category(1, "promos")
        self.assertEqual(moved, "fox/night/promos/long.mp4")
        self.assertTrue((self.media / moved).is_file())
        row = self.connection.execute(
            "SELECT media_type,source_relative_path,enabled_for_scheduling FROM assets WHERE id=1"
        ).fetchone()
        self.assertEqual(tuple(row), ("promo", moved, 1))
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM commercial_duration_reviews WHERE asset_id=1"
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
