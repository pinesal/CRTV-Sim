import shutil
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.normalization import AnalysisResult, NormalizationService
from retrotv.normalization_review import NormalizationReviewService
from retrotv.probe import ProbeResult
from retrotv.promotion import sha256_file


class NormalizationReviewTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        self.root = project / ".test-tmp" / ("rereview-" + uuid.uuid4().hex)
        self.media = self.root / "media"
        self.backups = self.root / "backup"
        self.config = AppConfig(self.root / "test.toml", project, {
            "application": {"database_path": str(self.root / "db.sqlite3")},
            "storage_roots": {"media": str(self.media), "originals_backup": str(self.backups),
                              "normalized": str(self.root / "normalized"), "previews": str(self.root / "previews")},
            "tools": {"ffprobe": "ffprobe", "ffmpeg": "ffmpeg"}})
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        media_id = ensure_storage_root(self.connection, "media")
        self.backup_id = ensure_storage_root(self.connection, "originals_backup")
        self.relative = "fox/evening/episodes/Test/Season 1/episode.mp4"
        self.source = self.media / self.relative
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"corrected")
        self.connection.execute("""INSERT INTO assets
            (id,source_root_id,source_relative_path,original_filename,file_size,modified_ns,
             media_type,channel_slug,daypart_slug,series_title,display_title,metadata_status,
             integrity_status,review_status,enabled_for_scheduling,first_seen_at,last_seen_at)
            VALUES (1,?,?,'episode.mp4',9,1,'episode','fox','evening','Test','Episode',
                    'ready','ready','ready',1,?,?)""", (media_id, self.relative, utc_now(), utc_now()))
        self.connection.execute("""INSERT INTO asset_media_info(asset_id,width,height,duration_seconds)
            VALUES (1,640,480,1400)""")
        self.connection.execute("""INSERT INTO normalization_profiles(asset_id,analysis_status,
            detected_mode,override_mode,proxy_status) VALUES (1,'analyzed','native_4_3','auto','promoted')""")
        self.connection.execute("""INSERT INTO asset_eligibility
            (asset_id,channel_slug,daypart_slug,eligibility_source) VALUES (1,'fox','evening','test')""")
        self.connection.commit()
        self.analysis = patch("retrotv.normalization.NormalizationAnalyzer.analyze", return_value=
            AnalysisResult("analyzed", "native_4_3", "native_4_3", 1, None, [], "Native frame"))
        self.analysis.start()

    def tearDown(self):
        self.analysis.stop()
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def backup(self):
        relative = "fox/evening/episodes/Test/episode.mkv"
        path = self.backups / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original")
        self.connection.execute("""INSERT INTO normalization_promotions
            (asset_id,original_source_relative_path,corrected_source_relative_path,backup_root_id,
             backup_relative_path,original_sha256,corrected_sha256,status,created_at)
            VALUES (1,?,?,?,?,?,?,'active',?)""",
            (relative, self.relative, self.backup_id, relative, sha256_file(path),
             sha256_file(self.source), utc_now()))
        self.connection.commit()
        return path

    def test_no_backup_preserves_only_copy_and_stays_in_review_after_reanalysis(self):
        result = NormalizationReviewService(self.connection, self.config).reopen(1)
        self.assertFalse(result["backup_restored"])
        self.assertEqual(self.source.read_bytes(), b"corrected")
        self.assertEqual(result["review_status"], "review")
        self.assertEqual(NormalizationService(self.connection, self.config).analyze_asset(1).status, "review")
        NormalizationService(self.connection, self.config).approve_review(1)
        self.assertEqual(self.connection.execute("SELECT review_requested FROM normalization_profiles").fetchone()[0], 0)

    @patch("retrotv.normalization_review.probe_media", return_value=ProbeResult(True,
           {"width": 1280, "height": 720, "duration_seconds": 1400}, "{}"))
    def test_verified_original_restored_at_current_location_preserving_identity(self, probe):
        backup = self.backup()
        self.connection.execute("INSERT INTO channels(id,slug,display_name) VALUES (1,'fox','Fox')")
        self.connection.execute("""INSERT INTO internet_sources(id,source_key,channel_id,source_type,
            url,show_title,source_relative_path,created_at)
            VALUES (1,'source',1,'direct_video','https://example.test/episode.mkv','Test','fox/source.json',?)""", (utc_now(),))
        self.connection.execute("""INSERT INTO remote_episodes(id,source_id,stable_key,remote_url,
            remote_filename,display_title,episode_date,local_path,retained,asset_id,created_at,updated_at)
            VALUES (20,1,'stable','https://example.test/episode.mkv','episode.mkv','Episode','1991-01-01',?,1,1,?,?)""",
            (str(self.source), utc_now(), utc_now()))
        self.connection.commit()
        result = NormalizationReviewService(self.connection, self.config).reopen(1)
        target = self.source.with_suffix(".mkv")
        self.assertTrue(result["backup_restored"])
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(self.source.exists())
        self.assertTrue(backup.exists())
        row = self.connection.execute("SELECT * FROM assets WHERE id=1").fetchone()
        self.assertEqual(row["series_title"], "Test")
        self.assertEqual(row["daypart_slug"], "evening")
        self.assertEqual(row["enabled_for_scheduling"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM asset_eligibility WHERE asset_id=1").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT width FROM asset_media_info WHERE asset_id=1").fetchone()[0], 1280)
        self.assertEqual(self.connection.execute("SELECT status FROM normalization_promotions").fetchone()[0], "restored")
        remote = self.connection.execute("SELECT * FROM remote_episodes WHERE id=20").fetchone()
        self.assertEqual(remote["local_path"], str(target))
        self.assertEqual(remote["asset_id"], 1)
        self.assertEqual(remote["stable_key"], "stable")
        self.assertEqual(remote["episode_date"], "1991-01-01")

    def test_invalid_backup_or_active_file_does_not_change_working_copy(self):
        backup = self.backup()
        backup.write_bytes(b"corrupted")
        service = NormalizationReviewService(self.connection, self.config)
        with self.assertRaisesRegex(ValueError, "verification failed"):
            service.reopen(1)
        with self.assertRaisesRegex(ValueError, "player must release"):
            service.reopen(1, active_path=str(self.source))
        self.assertEqual(self.source.read_bytes(), b"corrected")
        self.assertEqual(self.connection.execute("SELECT proxy_status FROM normalization_profiles").fetchone()[0], "promoted")

    @patch("retrotv.normalization_review.probe_media", return_value=ProbeResult(False, {}, error_detail="bad video"))
    def test_probe_failure_and_collision_preserve_current_file(self, probe):
        self.backup()
        service = NormalizationReviewService(self.connection, self.config)
        with self.assertRaisesRegex(ValueError, "probe failed"):
            service.reopen(1)
        self.source.with_suffix(".mkv").write_bytes(b"other")
        with self.assertRaises(FileExistsError):
            service.reopen(1)
        self.assertEqual(self.source.read_bytes(), b"corrected")

    @patch("retrotv.normalization_review.probe_media", return_value=ProbeResult(True, {"width":640,"height":480}, "{}"))
    def test_database_failure_rolls_back_file_installation(self, probe):
        self.backup()
        self.connection.execute("""CREATE TRIGGER fail_review BEFORE UPDATE ON assets
            BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        self.connection.commit()
        with self.assertRaisesRegex(Exception, "test failure"):
            NormalizationReviewService(self.connection, self.config).reopen(1)
        self.assertEqual(self.source.read_bytes(), b"corrected")
        self.assertFalse(self.source.with_suffix(".mkv").exists())

    def test_analysis_error_remains_error(self):
        with patch("retrotv.normalization.NormalizationAnalyzer.analyze", return_value=
                   AnalysisResult("error", "ambiguous", "preserve_frame", 0, None, [], "probe error")):
            self.assertEqual(NormalizationReviewService(self.connection, self.config).reopen(1)["review_status"], "error")

    def test_restart_recovers_interrupted_restore_without_losing_working_copy(self):
        service = NormalizationReviewService(self.connection, self.config)
        target = self.source.with_suffix(".mkv")
        rollback = self.source.with_name(".episode.review-rollback")
        original_hash = sha256_file(self.source)
        os.replace(self.source, rollback)
        target.write_bytes(b"original")
        service._journal_root().mkdir(parents=True)
        journal = service._journal_root() / "1.json"
        journal.write_text(json.dumps({"asset_id":1, "source":self.relative,
            "target":target.relative_to(self.media).as_posix(),
            "rollback":rollback.relative_to(self.media).as_posix(),
            "working_sha256":original_hash,"original_sha256":sha256_file(target)}))
        self.assertEqual(service.recover_pending(), 1)
        self.assertEqual(self.source.read_bytes(), b"corrected")
        self.assertFalse(target.exists())
        self.assertFalse(journal.exists())

    def test_requested_review_does_not_overwrite_duplicate_recipe_before_approval(self):
        from retrotv.duplicates import DuplicateService
        service = NormalizationReviewService(self.connection, self.config)
        service.reopen(1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM content_normalization_recipes").fetchone()[0], 0)
        self.assertFalse(DuplicateService(self.connection, self.config).apply_normalization_recipe(1))
        NormalizationService(self.connection, self.config).approve_review(1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM content_normalization_recipes").fetchone()[0], 1)
