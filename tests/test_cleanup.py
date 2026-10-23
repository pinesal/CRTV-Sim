import hashlib
import os
import shutil
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from retrotv.cleanup import GeneratedAssetCleanup
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root


class GeneratedAssetCleanupTests(unittest.TestCase):
    def setUp(self):
        source_project = Path(__file__).resolve().parents[1]
        temporary_root = source_project / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"cleanup-{uuid.uuid4().hex}"
        self.root.mkdir()
        generated = self.root / "data" / "generated"
        self.announcements = generated / "announcements"
        self.graphics = generated / "graphics"
        self.media = self.root / "media"
        self.previews = self.root / "previews"
        self.backup = self.root / "backup"
        self.announcements.mkdir(parents=True)
        self.graphics.mkdir()
        self.media.mkdir()
        self.previews.mkdir()
        self.backup.mkdir()
        self.config = AppConfig(
            self.root / "test.toml",
            self.root,
            {
                "application": {
                    "database_path": str(self.root / "data" / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {
                    "media": str(self.root / "media"),
                    "state": str(self.root / "data"),
                    "normalized": str(self.root / "normalized"),
                    "previews": str(self.root / "previews"),
                    "originals_backup": str(self.root / "backup"),
                },
                "guide": {"output": str(generated / "guide.mp4")},
                "announcements": {"output_dir": str(self.announcements)},
                "graphics": {"output_dir": str(self.graphics)},
                "generated_cleanup": {
                    "minimum_interval_hours": 24,
                    "silent_retention_days": 7,
                    "narrated_retention_days": 30,
                    "graphics_retention_days": 14,
                    "aspect_preview_retention_days": 7,
                    "delete_normalization_backups": True,
                    "normalization_backup_retention_days": 30,
                    "temporary_retention_hours": 1,
                    "obsolete_continuity_retention_days": 1,
                    "guide_versions_to_keep": 3,
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, source_project / "migrations")
        self.connection.execute(
            "INSERT INTO channels(id,slug,display_name,enabled) VALUES (1,'fox','FOX',1)"
        )
        self.media_root_id = ensure_storage_root(self.connection, "media")
        self.backup_root_id = ensure_storage_root(
            self.connection, "originals_backup", storage_type="archive"
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _age(path: Path, now: float, days: float) -> None:
        path.write_bytes(b"generated")
        stamp = now - days * 86400
        os.utime(path, (stamp, stamp))

    def _opportunity(self, generation_id: int, path: Path, status: str) -> None:
        self.connection.execute(
            """
            INSERT INTO schedule_generations(
                id,channel_id,schedule_date,seed,configuration_fingerprint,
                status,validation_status,generated_at,activated_at
            ) VALUES (?,1,'2033-05-18',?,?,?,'valid',?,?)
            """,
            (
                generation_id,
                f"seed-{generation_id}",
                f"fingerprint-{generation_id}",
                status,
                "2033-05-18T00:00:00+00:00",
                "2033-05-18T00:00:00+00:00",
            ),
        )
        entry_id = generation_id
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                id,generation_id,channel_id,starts_at,ends_at,entry_type,title,
                daypart_slug,sequence_position,is_program
            ) VALUES (?,?,1,'2033-05-18T12:00:00-07:00',
                      '2033-05-18T12:00:30-07:00','announcement_opportunity',
                      'Up Next','afternoon',0,0)
            """,
            (entry_id, generation_id),
        )
        self.connection.execute(
            """
            INSERT INTO announcement_opportunities(
                schedule_entry_id,generation_id,channel_id,starts_at,ends_at,
                facts_json,cache_key,fallback_path,render_status,created_at,updated_at
            ) VALUES (?,?,1,'2033-05-18T12:00:00-07:00',
                      '2033-05-18T12:00:30-07:00','{}',?,?,
                      'ready','2033-05-18T00:00:00+00:00',
                      '2033-05-18T00:00:00+00:00')
            """,
            (entry_id, generation_id, f"cache-{generation_id}", str(path)),
        )
        self.connection.commit()

    def test_loading_cover_cache_expires_without_removing_recent_frames(self):
        now=time.time()
        root=self.config.storage_root('state')/'generated/iptv-covers'
        root.mkdir(parents=True)
        old=root/'old.bgra';old.write_bytes(b'cached')
        partial=root/'abandoned.partial.bgra';partial.write_bytes(b'partial')
        recent=root/'recent.bgra';recent.write_bytes(b'recent')
        for path in [old,partial]:os.utime(path,(now-7200,now-7200))
        GeneratedAssetCleanup(self.connection,self.config).run(force=True,now=now)
        self.assertFalse(old.exists());self.assertFalse(partial.exists());self.assertTrue(recent.exists())

    def test_cleanup_prunes_expired_cache_and_protects_active_outputs(self):
        now = time.time()
        active = self.announcements / "opportunity-1-active-silent.mp4"
        stale = self.announcements / "opportunity-2-stale-silent.mp4"
        recent_narrated = self.announcements / "manual-narrated.mp4"
        expired_narrated = self.announcements / "old-narrated.mp4"
        old_graphic = self.graphics / "graphic-1-old.mp4"
        old_partial = self.graphics / "graphic-2.partial.mp4"
        announcer_tests = self.announcements / "tests"
        announcer_tests.mkdir()
        old_voice_test = announcer_tests / "announcer-test-old.wav"
        recent_voice_test = announcer_tests / "announcer-test-recent.wav"
        for path, days in (
            (active, 20), (stale, 20), (recent_narrated, 10),
            (expired_narrated, 31), (old_graphic, 15), (old_partial, 2),
            (old_voice_test, 2), (recent_voice_test, 1 / 48),
        ):
            self._age(path, now, days)
        self._opportunity(1, active, "active")
        self._opportunity(2, stale, "superseded")

        generated = self.root / "data" / "generated"
        runtime_guides = []
        for index in range(5):
            path = generated / f"guide.runtime-{index}.mp4"
            self._age(path, now, index / 24)
            runtime_guides.append(path)
        current_continuity = generated / "continuity-v2.mp4"
        old_continuity = generated / "continuity.mp4"
        self._age(current_continuity, now, 0)
        self._age(old_continuity, now, 2)
        abandoned_guide = generated / ".guide-abandoned-final.mp4"
        self._age(abandoned_guide, now, 2)

        summary = GeneratedAssetCleanup(self.connection, self.config).run(
            force=True, now=now
        )

        self.assertFalse(summary.errors)
        self.assertTrue(active.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(recent_narrated.exists())
        self.assertFalse(expired_narrated.exists())
        self.assertFalse(old_graphic.exists())
        self.assertFalse(old_partial.exists())
        self.assertFalse(old_voice_test.exists())
        self.assertTrue(recent_voice_test.exists())
        self.assertEqual(sum(path.exists() for path in runtime_guides), 3)
        self.assertTrue(current_continuity.exists())
        self.assertFalse(old_continuity.exists())
        self.assertFalse(abandoned_guide.exists())
        stale_reference = self.connection.execute(
            "SELECT fallback_path FROM announcement_opportunities WHERE generation_id=2"
        ).fetchone()
        self.assertIsNone(stale_reference["fallback_path"])

        skipped = GeneratedAssetCleanup(self.connection, self.config).run(
            force=False, now=now + 60
        )
        self.assertTrue(skipped.skipped)
        self.assertIn("not due", skipped.reason)

    def test_schedule_specific_screens_expire_after_24_hours(self):
        now = time.time()
        expired = self.announcements / "opportunity-2-old-v6-narrated.mp4"
        recent = self.announcements / "opportunity-3-new-v6-narrated.mp4"
        voice = self.announcements / "generic" / "station.wav"
        voice.parent.mkdir()
        self._age(expired, now, 1.1)
        self._age(recent, now, 0.9)
        self._age(voice, now, 2)
        self._opportunity(2, expired, "superseded")
        self.connection.execute(
            "INSERT INTO announcement_jobs(opportunity_id,cache_key,provider,status,output_path,requested_at) "
            "SELECT id,cache_key,'openai','ready',?,? FROM announcement_opportunities WHERE generation_id=2",
            (str(expired), datetime.fromtimestamp(now, UTC).isoformat()),
        )
        self.connection.commit()
        GeneratedAssetCleanup(self.connection, self.config).run(force=True, now=now)
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(voice.exists())
        self.assertIsNone(self.connection.execute(
            "SELECT output_path FROM announcement_jobs LIMIT 1"
        ).fetchone()["output_path"])

    def test_generic_recordings_never_age_out_even_without_database_references(self):
        now = time.time()
        generic = self.announcements / "generic"
        generic.mkdir()
        paths = [generic / name for name in ("channel.wav", "channel-night.wav", "channel-weekly-special.wav", "old-unused.wav")]
        for path in paths:
            self._age(path, now, 400)
        GeneratedAssetCleanup(self.connection, self.config).run(force=True, now=now)
        self.assertTrue(all(path.exists() for path in paths))

    def _asset(self, relative: str, contents: bytes) -> int:
        path = self.media / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        cursor = self.connection.execute(
            """
            INSERT INTO assets(
                source_root_id, source_relative_path, original_filename,
                file_size, modified_ns, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.media_root_id,
                Path(relative).as_posix(),
                path.name,
                path.stat().st_size,
                path.stat().st_mtime_ns,
                "2033-01-01T00:00:00+00:00",
                "2033-01-01T00:00:00+00:00",
            ),
        )
        return int(cursor.lastrowid)

    def test_cleanup_removes_expired_approved_previews_and_clears_references(self):
        now = time.time()
        asset_id = self._asset("fox/all-day/episodes/show.mp4", b"working")
        source = self.previews / f"{asset_id}-source.jpg"
        corrected = self.previews / f"{asset_id}-normalized.jpg"
        self._age(source, now, 8)
        self._age(corrected, now, 8)
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(
                asset_id, analysis_status, source_preview_relative_path,
                normalized_preview_relative_path
            ) VALUES (?, 'analyzed', ?, ?)
            """,
            (asset_id, source.name, corrected.name),
        )
        self.connection.commit()

        summary = GeneratedAssetCleanup(self.connection, self.config).run(
            force=True, now=now
        )

        self.assertEqual(summary.previews_removed, 2)
        self.assertFalse(source.exists())
        self.assertFalse(corrected.exists())
        profile = self.connection.execute(
            "SELECT * FROM normalization_profiles WHERE asset_id=?", (asset_id,)
        ).fetchone()
        self.assertIsNone(profile["source_preview_relative_path"])
        self.assertIsNone(profile["normalized_preview_relative_path"])

    def test_cleanup_keeps_previews_while_aspect_review_is_open(self):
        now = time.time()
        asset_id = self._asset("fox/all-day/episodes/review.mp4", b"working")
        source = self.previews / f"{asset_id}-source.jpg"
        corrected = self.previews / f"{asset_id}-normalized.jpg"
        recipe = self.previews / f"{asset_id}-normalized.preview.json"
        for candidate in (source, corrected, recipe):
            self._age(candidate, now, 30)
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(
                asset_id, analysis_status, source_preview_relative_path,
                normalized_preview_relative_path
            ) VALUES (?, 'review', ?, ?)
            """,
            (asset_id, source.name, corrected.name),
        )
        self.connection.commit()

        summary = GeneratedAssetCleanup(self.connection, self.config).run(
            force=True, now=now
        )

        self.assertEqual(summary.previews_removed, 0)
        self.assertTrue(source.exists())
        self.assertTrue(corrected.exists())
        self.assertTrue(recipe.exists())

    def test_cleanup_deletes_only_expired_backups_with_verified_working_copy(self):
        now = time.time()
        old_created = datetime.fromtimestamp(now - 31 * 86400, UTC).isoformat()
        current_contents = b"verified normalized media"
        current_hash = hashlib.sha256(current_contents).hexdigest()
        safe_id = self._asset("fox/all-day/episodes/safe.mp4", current_contents)
        unsafe_id = self._asset("fox/all-day/episodes/unsafe.mp4", b"changed later")
        safe_backup_rel = Path("fox/all-day/episodes/safe-original.mkv")
        unsafe_backup_rel = Path("fox/all-day/episodes/unsafe-original.mkv")
        safe_backup = self.backup / safe_backup_rel
        unsafe_backup = self.backup / unsafe_backup_rel
        safe_backup.parent.mkdir(parents=True, exist_ok=True)
        safe_backup.write_bytes(b"safe original")
        unsafe_backup.write_bytes(b"unsafe original")
        for asset_id, relative, corrected_hash in (
            (safe_id, safe_backup_rel, current_hash),
            (unsafe_id, unsafe_backup_rel, "not-the-current-hash"),
        ):
            self.connection.execute(
                """
                INSERT INTO normalization_promotions(
                    asset_id, original_source_relative_path,
                    corrected_source_relative_path, backup_root_id,
                    backup_relative_path, original_sha256, corrected_sha256,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    asset_id,
                    relative.as_posix(),
                    self.connection.execute(
                        "SELECT source_relative_path FROM assets WHERE id=?",
                        (asset_id,),
                    ).fetchone()["source_relative_path"],
                    self.backup_root_id,
                    relative.as_posix(),
                    "original-hash",
                    corrected_hash,
                    old_created,
                ),
            )
        self.connection.commit()

        summary = GeneratedAssetCleanup(self.connection, self.config).run(
            force=True, now=now
        )

        self.assertEqual(summary.backups_removed, 1)
        self.assertGreaterEqual(summary.backups_retained, 1)
        self.assertFalse(safe_backup.exists())
        self.assertTrue(unsafe_backup.exists())
        safe_record = self.connection.execute(
            "SELECT backup_deleted_at FROM normalization_promotions WHERE asset_id=?",
            (safe_id,),
        ).fetchone()
        unsafe_record = self.connection.execute(
            "SELECT backup_deleted_at FROM normalization_promotions WHERE asset_id=?",
            (unsafe_id,),
        ).fetchone()
        self.assertIsNotNone(safe_record["backup_deleted_at"])
        self.assertIsNone(unsafe_record["backup_deleted_at"])

    def test_newer_record_extends_retention_for_shared_backup_path(self):
        now = time.time()
        old_created = datetime.fromtimestamp(now - 60 * 86400, UTC).isoformat()
        recent_created = datetime.fromtimestamp(now - 2 * 86400, UTC).isoformat()
        current_contents = b"current verified media"
        current_hash = hashlib.sha256(current_contents).hexdigest()
        asset_id = self._asset("fox/all-day/episodes/retried.mp4", current_contents)
        backup_relative = Path("fox/all-day/episodes/retried-original.mkv")
        backup = self.backup / backup_relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(b"original")
        for created_at in (old_created, recent_created):
            self.connection.execute(
                """
                INSERT INTO normalization_promotions(
                    asset_id, original_source_relative_path,
                    corrected_source_relative_path, backup_root_id,
                    backup_relative_path, original_sha256, corrected_sha256,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    asset_id,
                    backup_relative.as_posix(),
                    "fox/all-day/episodes/retried.mp4",
                    self.backup_root_id,
                    backup_relative.as_posix(),
                    "original-hash",
                    current_hash,
                    created_at,
                ),
            )
        self.connection.commit()

        summary = GeneratedAssetCleanup(self.connection, self.config).run(
            force=True, now=now
        )

        self.assertTrue(backup.exists())
        self.assertEqual(summary.backups_removed, 0)
        records = self.connection.execute(
            "SELECT backup_deleted_at FROM normalization_promotions WHERE asset_id=?",
            (asset_id,),
        ).fetchall()
        self.assertTrue(all(row["backup_deleted_at"] is None for row in records))


if __name__ == "__main__":
    unittest.main()
