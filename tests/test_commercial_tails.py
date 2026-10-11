import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

from retrotv.commercial_tails import CommercialTailService
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.probe import fingerprint_file, probe_media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg is required for media integration tests")
class CommercialTailTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        self.root = project / ".test-tmp" / f"commercial-tails-{uuid.uuid4().hex}"
        self.media = self.root / "media"
        self.media.mkdir(parents=True)
        self.config = AppConfig(self.root / "config.toml", project, {
            "application": {"database_path": str(self.root / "db.sqlite3")},
            "storage_roots": {"media": str(self.media),
                              "originals_backup": str(self.root / "backup")},
            "tools": {"ffmpeg": shutil.which("ffmpeg"),
                      "ffprobe": shutil.which("ffprobe")},
        })
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        self.service = CommercialTailService(self.connection, self.config)

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def make_spot(self, *, endcard: bool = False, media_type: str = "commercial"):
        source = self.media / "fox/all-day/commercials/spot.mp4"
        source.parent.mkdir(parents=True)
        end = "color=c=black:s=320x240:d=4:r=24"
        if endcard:
            end += ",drawbox=x=130:y=100:w=60:h=40:color=white:t=fill"
        command = [str(self.config.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "color=c=red:s=320x240:d=5:r=24",
                   "-f", "lavfi", "-i", end,
                   "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                   "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   str(source)]
        subprocess.run(command, check=True, capture_output=True)
        probe = probe_media(source, ffprobe=self.config.ffprobe, timeout_seconds=30.0)
        self.assertTrue(probe.ok)
        fingerprint, algorithm = fingerprint_file(source, mode="quick")
        now = utc_now()
        root_id = ensure_storage_root(self.connection, "media")
        self.connection.execute("""
            INSERT INTO assets(id,source_root_id,source_relative_path,original_filename,
                fingerprint,fingerprint_algorithm,file_size,modified_ns,media_type,
                metadata_status,integrity_status,review_status,enabled_for_scheduling,
                first_seen_at,last_seen_at)
            VALUES (1,?,?,?,?,?,?,?,?, 'ready','ready','ready',1,?,?)
        """, (root_id, "fox/all-day/commercials/spot.mp4", "spot.mp4",
              fingerprint, algorithm, source.stat().st_size, source.stat().st_mtime_ns,
              media_type, now, now))
        self.connection.execute("""
            INSERT INTO asset_media_info(asset_id,duration_seconds) VALUES (1,?)
        """, (probe.metadata["duration_seconds"],))
        self.connection.commit()
        return source

    def test_empty_black_tail_is_trimmed_with_verified_backup(self):
        source = self.make_spot()
        self.assertEqual(len(self.service.pending_assets()), 1)
        self.assertEqual(self.service.analyze_asset(1), "trimmed")
        row = self.connection.execute("""
            SELECT status,backup_relative_path,original_duration_seconds,
                   corrected_duration_seconds FROM commercial_tail_analyses WHERE asset_id=1
        """).fetchone()
        self.assertEqual(row["status"], "trimmed")
        self.assertGreater(row["original_duration_seconds"] - row["corrected_duration_seconds"], 3)
        self.assertTrue((self.config.storage_root("originals_backup") /
                         row["backup_relative_path"]).is_file())
        self.assertTrue(source.is_file())
        self.assertEqual(self.service.pending_assets(), [])

    def test_black_endcard_and_non_commercial_are_untouched(self):
        source = self.make_spot(endcard=True)
        size = source.stat().st_size
        self.assertEqual(self.service.analyze_asset(1), "skipped")
        self.assertEqual(source.stat().st_size, size)
        self.connection.execute("UPDATE assets SET media_type='promo' WHERE id=1")
        self.connection.commit()
        self.assertEqual(self.service.pending_assets(), [])
        with self.assertRaisesRegex(ValueError, "commercials"):
            self.service.analyze_asset(1)


if __name__ == "__main__":
    unittest.main()
