import hashlib
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.duplicates import DuplicateService
from retrotv.probe import ProbeResult


class DuplicateServiceTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        self.root = project / ".test-tmp" / f"duplicates-{uuid.uuid4().hex}"
        self.media = self.root / "media"
        self.normalized = self.root / "normalized"
        self.media.mkdir(parents=True)
        self.config = AppConfig(
            self.root / "test.toml",
            project,
            {
                "application": {"database_path": str(self.root / "state.sqlite3")},
                "storage_roots": {
                    "media": str(self.media),
                    "normalized": str(self.normalized),
                },
                "tools": {"ffprobe": "ffprobe", "ffmpeg": "ffmpeg"},
                "normalization": {"canvas_width": 640, "canvas_height": 480},
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        root_id = ensure_storage_root(self.connection, "media")
        now = utc_now()
        self.paths = []
        for asset_id, relative in (
            (1, "fox/evening/episodes/Show/one.mkv"),
            (2, "fox/specials/weekly/show/episodes/Show/one.mkv"),
        ):
            path = self.media / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"identical-original-video")
            stat = path.stat()
            self.paths.append(path)
            self.connection.execute(
                """
                INSERT INTO assets(
                    id, source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, channel_slug, media_type,
                    metadata_status, integrity_status, review_status,
                    enabled_for_scheduling, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'fox', 'episode',
                    'ready', 'ready', 'ready', 1, ?, ?)
                """,
                (asset_id, root_id, relative, path.name, stat.st_size, stat.st_mtime_ns, now, now),
            )
            self.connection.execute(
                """
                INSERT INTO asset_media_info(asset_id, duration_seconds, width, height)
                VALUES (?, 1200, 720, 480)
                """,
                (asset_id,),
            )
            self.connection.execute(
                "INSERT INTO normalization_profiles(asset_id) VALUES (?)",
                (asset_id,),
            )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_exact_groups_review_and_normalization_reuse(self):
        service = DuplicateService(self.connection, self.config)
        service.record_asset(1)
        service.record_asset(2)
        summary = service.groups()
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["groups"][0]["member_count"], 2)

        service.review(summary["groups"][0]["sha256"], "intentional")
        self.assertEqual(service.groups()["intentional"], 1)

        self.connection.execute(
            """
            UPDATE normalization_profiles SET analysis_status='analyzed',
                detected_mode='stretched_4_3', proposed_mode='stretched_4_3',
                override_mode='auto', confidence=0.95 WHERE asset_id=1
            """
        )
        self.connection.commit()
        self.assertTrue(service.store_normalization_recipe(1))
        self.assertTrue(service.apply_normalization_recipe(2))
        copied = self.connection.execute(
            "SELECT * FROM normalization_profiles WHERE asset_id=2"
        ).fetchone()
        self.assertEqual(copied["analysis_status"], "analyzed")
        self.assertEqual(copied["detected_mode"], "stretched_4_3")

    def test_corrected_duplicate_is_staged_without_encoding(self):
        service = DuplicateService(self.connection, self.config)
        service.record_asset(1)
        service.record_asset(2)
        self.connection.execute(
            """
            UPDATE normalization_profiles SET analysis_status='analyzed',
                detected_mode='stretched_4_3', proposed_mode='stretched_4_3'
            WHERE asset_id=1
            """
        )
        self.connection.commit()
        service.store_normalization_recipe(1)
        service.apply_normalization_recipe(2)

        corrected = b"already-corrected-video"
        self.paths[0].write_bytes(corrected)
        corrected_hash = hashlib.sha256(corrected).hexdigest()
        original_hash = hashlib.sha256(b"identical-original-video").hexdigest()
        self.connection.execute(
            """
            INSERT INTO normalization_promotions(
                asset_id, original_source_relative_path,
                corrected_source_relative_path, backup_root_id,
                backup_relative_path, original_sha256, corrected_sha256,
                status, created_at
            ) VALUES (1, 'old.mkv', 'one.mkv', 1, 'old.mkv', ?, ?, 'active', ?)
            """,
            (original_hash, corrected_hash, utc_now()),
        )
        self.connection.commit()
        probe = ProbeResult(True, {"duration_seconds": 1200.0}, raw_json="{}")
        with patch("retrotv.duplicates.probe_media", return_value=probe):
            donor = service.prepare_reusable_proxy(2)
        self.assertEqual(donor, 1)
        self.assertEqual((self.normalized / "fox" / "2.mp4").read_bytes(), corrected)
        profile = self.connection.execute(
            "SELECT proxy_status FROM normalization_profiles WHERE asset_id=2"
        ).fetchone()
        self.assertEqual(profile["proxy_status"], "ready")

    def test_recipe_with_crop_outside_current_frame_is_not_reused(self):
        service = DuplicateService(self.connection, self.config)
        service.record_asset(1)
        service.record_asset(2)
        self.connection.execute(
            "UPDATE asset_media_info SET width=1280,height=720 WHERE asset_id=1"
        )
        self.connection.execute(
            """
            UPDATE normalization_profiles SET analysis_status='analyzed',
                detected_mode='pillarboxed_4_3', proposed_mode='pillarboxed_4_3',
                crop_x=172,crop_y=0,crop_width=936,crop_height=720
            WHERE asset_id=1
            """
        )
        self.connection.commit()
        self.assertTrue(service.store_normalization_recipe(1))

        self.assertFalse(service.apply_normalization_recipe(2))
        target = self.connection.execute(
            "SELECT analysis_status,crop_width FROM normalization_profiles WHERE asset_id=2"
        ).fetchone()
        self.assertEqual(target["analysis_status"], "pending")
        self.assertIsNone(target["crop_width"])


if __name__ == "__main__":
    unittest.main()
