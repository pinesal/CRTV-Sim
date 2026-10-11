import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.normalization import NormalizationService
from retrotv.promotion import PromotionService
from retrotv.probe import probe_media


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg integration tools are unavailable",
)
class NormalizationIntegrationTests(unittest.TestCase):
    def test_pillarbox_analysis_proxy_and_validation(self):
        project = Path(__file__).resolve().parents[1]
        base = project / ".test-tmp"
        base.mkdir(exist_ok=True)
        temporary = base / f"normalization-integration-{uuid.uuid4().hex}"
        source = temporary / "media/fox/night/episodes/Test/pillarbox.mp4"
        source.parent.mkdir(parents=True)
        try:
            completed = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi",
                    "-i",
                    "color=black:s=850x480:d=4,"
                    "drawbox=x=105:y=0:w=640:h=480:color=red:t=fill",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            config = AppConfig(
                profile_path=temporary / "test.toml",
                project_root=project,
                data={
                    "application": {
                        "database_path": str(temporary / "retrotv.sqlite3")
                    },
                    "storage_roots": {
                        "media": str(temporary / "media"),
                        "normalized": str(temporary / "normalized"),
                        "previews": str(temporary / "previews"),
                        "originals_backup": str(temporary / "originals-backup"),
                    },
                    "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                    "normalization": {
                        "sample_count": 3,
                        "video_preset": "ultrafast",
                    },
                },
            )
            connection = connect_database(config.database_path)
            try:
                apply_migrations(connection, project / "migrations")
                root_id = ensure_storage_root(connection, "media")
                now = utc_now()
                cursor = connection.execute(
                    """
                    INSERT INTO assets(
                        source_root_id, source_relative_path, original_filename,
                        file_size, modified_ns, channel_slug, metadata_status,
                        integrity_status, review_status, first_seen_at, last_seen_at
                    ) VALUES (?, ?, 'pillarbox.mp4', ?, ?, 'fox', 'ready',
                              'ready', 'ready', ?, ?)
                    """,
                    (
                        root_id,
                        "fox/night/episodes/Test/pillarbox.mp4",
                        source.stat().st_size,
                        source.stat().st_mtime_ns,
                        now,
                        now,
                    ),
                )
                asset_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO asset_media_info(
                        asset_id, duration_seconds, width, height,
                        sample_aspect_ratio, display_aspect_ratio
                    ) VALUES (?, 4.0, 850, 480, '1:1', '85:48')
                    """,
                    (asset_id,),
                )
                connection.commit()
                service = NormalizationService(connection, config)
                analysis = service.analyze_asset(asset_id)
                self.assertEqual(analysis.detected_mode, "pillarboxed_4_3")
                source_preview, normalized_preview = service.create_preview(asset_id)
                self.assertTrue(source_preview.is_file())
                self.assertTrue(normalized_preview.is_file())
                proxy = service.create_proxy(asset_id)
                self.assertTrue(proxy.ok, proxy.error)
                self.assertTrue(proxy.path.is_file())
                profile = connection.execute(
                    "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                self.assertEqual(profile["proxy_status"], "ready")
                self.assertEqual(
                    Path(profile["proxy_relative_path"]),
                    Path("fox") / f"{asset_id}.mp4",
                )
                promotion = PromotionService(
                    connection, config
                ).replace_with_backup(asset_id)
                self.assertTrue(promotion.source_path.is_file())
                self.assertTrue(promotion.backup_path.is_file())
                self.assertNotEqual(
                    promotion.source_path.read_bytes(),
                    promotion.backup_path.read_bytes(),
                )
                corrected_probe = probe_media(
                    promotion.source_path,
                    ffprobe="ffprobe",
                    timeout_seconds=20,
                )
                original_probe = probe_media(
                    promotion.backup_path,
                    ffprobe="ffprobe",
                    timeout_seconds=20,
                )
                self.assertEqual(
                    (
                        corrected_probe.metadata["width"],
                        corrected_probe.metadata["height"],
                    ),
                    (640, 480),
                )
                self.assertEqual(
                    (
                        original_probe.metadata["width"],
                        original_probe.metadata["height"],
                    ),
                    (850, 480),
                )
                asset = connection.execute(
                    "SELECT * FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()
                self.assertEqual(
                    asset["source_relative_path"],
                    "fox/night/episodes/Test/pillarbox.mp4",
                )
                promoted = connection.execute(
                    "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                self.assertEqual(promoted["proxy_status"], "promoted")
            finally:
                connection.close()
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
