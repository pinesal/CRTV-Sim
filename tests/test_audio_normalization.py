import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from retrotv.audio_normalization import (
    AudioNormalizationService,
    _select_backup_path,
    _stream_duration,
)
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.promotion import sha256_file


class AudioNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        root = self.project / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temporary = root / f"audio-{uuid.uuid4().hex}"
        self.media = self.temporary / "media"
        self.media.mkdir(parents=True)
        self.config = AppConfig(
            profile_path=self.temporary / "test.toml",
            project_root=self.project,
            data={
                "application": {"database_path": str(self.temporary / "db.sqlite3")},
                "storage_roots": {
                    "media": str(self.media),
                    "normalized": str(self.temporary / "normalized"),
                    "originals_backup": str(self.temporary / "backup"),
                },
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                "audio_normalization": {
                    "target_lufs": -18.0,
                    "true_peak_dbtp": -1.5,
                    "loudness_tolerance_lu": 1.5,
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        root_id = ensure_storage_root(self.connection, "media")
        relative = Path("fox/evening/episodes/Test/test.mp4")
        source = self.media / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"test-media")
        stat = source.stat()
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO assets(
                source_root_id, source_relative_path, original_filename,
                file_size, modified_ns, media_type, metadata_status,
                integrity_status, review_status, enabled_for_scheduling,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, 'test.mp4', ?, ?, 'episode', 'ready', 'ready',
                      'ready', 1, ?, ?)
            """,
            (root_id, relative.as_posix(), stat.st_size, stat.st_mtime_ns, now, now),
        )
        self.asset_id = int(cursor.lastrowid)
        self.connection.execute(
            """
            INSERT INTO asset_media_info(
                asset_id, duration_seconds, video_codec, audio_codec, width, height
            ) VALUES (?, 1200, 'h264', 'aac', 640, 480)
            """,
            (self.asset_id,),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_analysis_auto_approves_material_loudness_difference(self):
        stderr = """
        [Parsed_loudnorm_0] {
          "input_i" : "-24.10",
          "input_tp" : "-3.20",
          "input_lra" : "5.10",
          "input_thresh" : "-34.20",
          "output_i" : "-17.90",
          "output_tp" : "-1.50",
          "output_lra" : "5.00",
          "output_thresh" : "-28.00",
          "normalization_type" : "dynamic",
          "target_offset" : "-0.10"
        }
        """
        completed = subprocess.CompletedProcess([], 0, "", stderr)
        with patch("retrotv.audio_normalization._run", return_value=completed):
            result = AudioNormalizationService(
                self.connection, self.config
            ).analyze_asset(self.asset_id)
        self.assertEqual(result.status, "analyzed")
        self.assertTrue(result.needs_correction)
        profile = self.connection.execute(
            "SELECT * FROM audio_normalization_profiles WHERE asset_id=?",
            (self.asset_id,),
        ).fetchone()
        self.assertEqual(profile["approval_status"], "approved")
        self.assertEqual(profile["correction_status"], "pending")

    def test_analysis_skips_audio_already_within_tolerance(self):
        stderr = """{
          "input_i":"-18.4", "input_tp":"-2.0", "input_lra":"4.0",
          "input_thresh":"-28.0", "target_offset":"0.0"
        }"""
        completed = subprocess.CompletedProcess([], 0, "", stderr)
        with patch("retrotv.audio_normalization._run", return_value=completed):
            result = AudioNormalizationService(
                self.connection, self.config
            ).analyze_asset(self.asset_id)
        self.assertFalse(result.needs_correction)
        profile = self.connection.execute(
            "SELECT * FROM audio_normalization_profiles WHERE asset_id=?",
            (self.asset_id,),
        ).fetchone()
        self.assertEqual(profile["approval_status"], "skipped")
        self.assertEqual(profile["correction_status"], "not_needed")

    def test_changed_source_returns_to_pending_analysis(self):
        service = AudioNormalizationService(self.connection, self.config)
        service._store_analysis(
            self.asset_id,
            "old-signature",
            status="analyzed",
            detail="old",
            measured_i=-18.0,
            measured_tp=-2.0,
            measured_lra=4.0,
            measured_thresh=-28.0,
            target_offset=0.0,
        )
        self.assertEqual(
            [row["id"] for row in service.pending_assets()], [self.asset_id]
        )

    def test_missing_media_is_not_an_audio_correction_candidate(self):
        service = AudioNormalizationService(self.connection, self.config)
        signature = self.connection.execute(
            """
            SELECT CAST(file_size AS TEXT) || ':' || CAST(modified_ns AS TEXT)
            FROM assets WHERE id=?
            """,
            (self.asset_id,),
        ).fetchone()[0]
        service._store_analysis(
            self.asset_id,
            signature,
            status="analyzed",
            detail="Needs correction",
            measured_i=-25.0,
            measured_tp=-3.0,
            measured_lra=5.0,
            measured_thresh=-35.0,
            target_offset=0.0,
        )
        self.connection.execute(
            "UPDATE assets SET integrity_status='missing' WHERE id=?",
            (self.asset_id,),
        )
        self.connection.commit()
        self.assertEqual(service.correction_candidates(), [])

    def test_webm_uses_opus_and_mp4_uses_aac(self):
        service = AudioNormalizationService(self.connection, self.config)
        self.assertIn("libopus", service._audio_codec_args(".webm"))
        self.assertIn("aac", service._audio_codec_args(".mp4"))
        self.assertIn("+faststart", service._audio_codec_args(".mp4"))
        self.assertIn("libmp3lame", service._fallback_audio_codec_args(".mp4"))

    def test_stream_duration_uses_video_instead_of_longer_container_audio(self):
        raw_probe = """{
          "streams": [
            {"codec_type":"video", "duration":"3250.880000"},
            {"codec_type":"audio", "duration":"3258.802000"}
          ],
          "format": {"duration":"3258.802000"}
        }"""
        self.assertEqual(_stream_duration(raw_probe, "video"), 3250.88)
        self.assertEqual(_stream_duration(raw_probe, "audio"), 3258.802)
        self.assertIsNone(_stream_duration("not json", "video"))

    def test_conflicting_audio_backup_gets_versioned_without_overwrite(self):
        backup = self.temporary / "backup/show.mp4"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"older source")
        old_bytes = backup.read_bytes()
        current = self.temporary / "current.mp4"
        current.write_bytes(b"current source")
        current_hash = sha256_file(current)

        selected, selected_relative = _select_backup_path(
            backup, Path("show.mp4"), current_hash
        )

        self.assertEqual(backup.read_bytes(), old_bytes)
        self.assertEqual(
            selected.name, f"show.source-{current_hash[:12]}.mp4"
        )
        self.assertEqual(selected_relative.name, selected.name)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg integration tools are unavailable",
)
class AudioNormalizationIntegrationTests(unittest.TestCase):
    def test_normalization_preserves_video_and_creates_verified_backup(self):
        project = Path(__file__).resolve().parents[1]
        base = project / ".test-tmp"
        base.mkdir(exist_ok=True)
        temporary = base / f"audio-integration-{uuid.uuid4().hex}"
        source = temporary / "media/fox/evening/episodes/Test/quiet.mp4"
        source.parent.mkdir(parents=True)
        try:
            made = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=red:s=320x240:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-filter:a", "volume=0.02", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(source),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                check=False,
            )
            self.assertEqual(made.returncode, 0, made.stderr)

            def frame_md5(path):
                result = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-map", "0:v:0", "-f", "framemd5", "-",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            original_video = frame_md5(source)
            original_bytes = source.read_bytes()
            config = AppConfig(
                profile_path=temporary / "test.toml",
                project_root=project,
                data={
                    "application": {"database_path": str(temporary / "db.sqlite3")},
                    "storage_roots": {
                        "media": str(temporary / "media"),
                        "normalized": str(temporary / "normalized"),
                        "originals_backup": str(temporary / "backup"),
                    },
                    "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                    "audio_normalization": {"target_lufs": -18.0},
                },
            )
            connection = connect_database(config.database_path)
            try:
                apply_migrations(connection, project / "migrations")
                root_id = ensure_storage_root(connection, "media")
                stat = source.stat()
                now = utc_now()
                cursor = connection.execute(
                    """
                    INSERT INTO assets(
                        source_root_id, source_relative_path, original_filename,
                        file_size, modified_ns, media_type, metadata_status,
                        integrity_status, review_status, enabled_for_scheduling,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, 'quiet.mp4', ?, ?, 'episode', 'ready', 'ready',
                              'ready', 1, ?, ?)
                    """,
                    (
                        root_id, "fox/evening/episodes/Test/quiet.mp4",
                        stat.st_size, stat.st_mtime_ns, now, now,
                    ),
                )
                asset_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO asset_media_info(
                        asset_id, duration_seconds, video_codec, audio_codec,
                        width, height
                    ) VALUES (?, 4.0, 'h264', 'aac', 320, 240)
                    """,
                    (asset_id,),
                )
                connection.commit()
                service = AudioNormalizationService(connection, config)
                analysis = service.analyze_asset(asset_id)
                self.assertEqual(analysis.status, "analyzed")
                self.assertTrue(analysis.needs_correction)
                normalized = service.normalize_asset(asset_id)
                self.assertTrue(normalized.source_path.is_file())
                self.assertEqual(normalized.backup_path.read_bytes(), original_bytes)
                self.assertEqual(frame_md5(normalized.source_path), original_video)
                profile = connection.execute(
                    "SELECT * FROM audio_normalization_profiles WHERE asset_id=?",
                    (asset_id,),
                ).fetchone()
                self.assertEqual(profile["correction_status"], "promoted")
            finally:
                connection.close()
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
