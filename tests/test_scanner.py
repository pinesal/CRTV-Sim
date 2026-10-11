import hashlib
import shutil
import unittest
import uuid
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.probe import DecodeResult, ProbeResult
from retrotv.scanner import LibraryScanner


class FakeInspector:
    def probe(self, path: Path) -> ProbeResult:
        duration = 7200.0 if "movies" in path.parts else 1200.0
        return ProbeResult(
            True,
            {
                "container_format": "matroska",
                "duration_seconds": duration,
                "bit_rate": 1_000_000,
                "video_codec": "h264",
                "audio_codec": "aac",
                "width": 640,
                "height": 480,
                "frame_rate": "30000/1001",
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "4:3",
                "pixel_format": "yuv420p",
                "field_order": "progressive",
                "audio_sample_rate": 48000,
                "audio_channels": 2,
            },
            raw_json="{}",
        )

    def sample(self, path: Path, start_seconds: float) -> DecodeResult:
        return DecodeResult(True)

    def fingerprint(self, path: Path) -> tuple[str, str]:
        return hashlib.sha256(path.read_bytes()).hexdigest(), "test-sha256"


class ScannerTests(unittest.TestCase):
    def test_shared_holiday_episodes_recover_prior_restriction_and_moved_identity(self):
        project = Path(__file__).resolve().parents[1]
        temporary = project / ".test-tmp" / f"shared-holiday-{uuid.uuid4().hex}"
        media = temporary / "media"
        path = media / "shared/specials/holidays/halloween/evening/episodes/Haunted Show/Halloween.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"episode")
        config = AppConfig(temporary / "test.toml", project, {
            "application": {"database_path":str(temporary / "state.sqlite3")},
            "storage_roots": {"media":str(media)},
            "scanner": {"video_extensions":[".mp4"]},
            "library": {"channels":["fox"],"holidays":["halloween"]}})
        connection = connect_database(config.database_path)
        try:
            apply_migrations(connection, project / "migrations")
            scanner = LibraryScanner(connection, config, inspector=FakeInspector())
            scanner.scan()
            asset_id = connection.execute("SELECT id FROM assets").fetchone()[0]
            connection.execute("DELETE FROM asset_eligibility WHERE asset_id=?", (asset_id,))
            connection.execute("UPDATE assets SET review_status='open',enabled_for_scheduling=0 WHERE id=?", (asset_id,))
            connection.execute("""INSERT INTO review_items(asset_id,reason_code,detail,status,created_at)
                VALUES (?,'shared_program_requires_channels','Old restriction','open',?)""", (asset_id, utc_now()))
            connection.commit()
            scanner.scan()
            row = connection.execute("SELECT review_status,enabled_for_scheduling FROM assets WHERE id=?", (asset_id,)).fetchone()
            self.assertEqual(tuple(row), ("ready",1))
            self.assertEqual(connection.execute("SELECT channel_slug FROM asset_eligibility WHERE asset_id=?", (asset_id,)).fetchone()[0], "*")
            moved = path.parent / "Season 1" / path.name
            moved.parent.mkdir()
            path.rename(moved)
            scanner.scan()
            rows = connection.execute("SELECT id,source_relative_path FROM assets").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], asset_id)
            self.assertEqual(rows[0]["source_relative_path"], moved.relative_to(media).as_posix())
        finally:
            connection.close()
            shutil.rmtree(temporary, ignore_errors=True)

    def test_holiday_layout_reclassifies_unchanged_files(self):
        project = Path(__file__).resolve().parents[1]
        temporary = project / ".test-tmp" / f"scanner-holiday-{uuid.uuid4().hex}"
        media = temporary / "media"
        episode = (media / "nickelodeon/specials/holidays/halloween/daytime"
                   / "episodes/Count Duckula/1x01.mp4")
        weekly = (media / "nickelodeon/specials/holidays/halloween/specials"
                  / "weekly/snick-main/blocks/SNICK or Treat.mp4")
        for path in (episode, weekly):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.name.encode())
        graphic = weekly.parent.parent / "up-next" / "background.mp4"
        graphic.parent.mkdir(parents=True)
        graphic.write_bytes(b"presentation only")
        config = AppConfig(
            temporary / "test.toml", project,
            {"application": {"database_path": str(temporary / "state.sqlite3")},
             "storage_roots": {"media": str(media)},
             "scanner": {"video_extensions": [".mp4"]},
             "library": {"channels": ["nickelodeon"]}},
        )
        connection = connect_database(config.database_path)
        try:
            apply_migrations(connection, project / "migrations")
            scanner = LibraryScanner(connection, config, inspector=FakeInspector())
            scanner.scan()
            rows = connection.execute(
                "SELECT media_type,daypart_slug,holiday_weekly_slug FROM assets "
                "ORDER BY source_relative_path"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["daypart_slug"], "daytime")
            self.assertEqual(rows[1]["holiday_weekly_slug"], "snick-main")
            connection.execute(
                "UPDATE assets SET media_type=NULL,daypart_slug=NULL,holiday_weekly_slug=NULL"
            )
            connection.commit()
            scanner.scan()
            rows = connection.execute(
                "SELECT media_type,daypart_slug,holiday_weekly_slug FROM assets "
                "ORDER BY source_relative_path"
            ).fetchall()
            self.assertEqual(rows[0]["media_type"], "episode")
            self.assertEqual(rows[0]["daypart_slug"], "daytime")
            self.assertEqual(rows[1]["media_type"], "complete_block")
            self.assertEqual(rows[1]["holiday_weekly_slug"], "snick-main")
        finally:
            connection.close()
            shutil.rmtree(temporary, ignore_errors=True)

    def test_first_scan_audits_long_commercial_once(self):
        project = Path(__file__).resolve().parents[1]
        temporary = project / ".test-tmp" / f"scanner-long-ad-{uuid.uuid4().hex}"
        media = temporary / "media"
        path = media / "fox/night/commercials/long.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"long-ad")
        config = AppConfig(
            temporary / "test.toml", project,
            {"application": {"database_path": str(temporary / "state.sqlite3")},
             "storage_roots": {"media": str(media)},
             "scanner": {"video_extensions": [".mp4"]},
             "library": {"channels": ["fox"], "long_commercial_minutes": 3}},
        )
        connection = connect_database(config.database_path)
        try:
            apply_migrations(connection, project / "migrations")
            scanner = LibraryScanner(connection, config, inspector=FakeInspector())
            scanner.scan()
            row = connection.execute(
                """SELECT a.review_status,a.enabled_for_scheduling,r.decision
                   FROM assets a JOIN commercial_duration_reviews r ON r.asset_id=a.id"""
            ).fetchone()
            self.assertEqual(tuple(row), ("open", 0, "pending"))
            scanner.scan()
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM review_items WHERE reason_code='long_commercial'
                   AND status='open'"""
            ).fetchone()[0], 1)
        finally:
            connection.close()
            shutil.rmtree(temporary, ignore_errors=True)

    def test_scan_restores_profile_for_video_then_audio_corrected_working_copy(self):
        project = Path(__file__).resolve().parents[1]
        temporary = project / ".test-tmp" / f"scanner-promotion-{uuid.uuid4().hex}"
        media = temporary / "media"
        path = media / "fox/night/episodes/Show/corrected.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"audio-corrected-working-copy")
        config = AppConfig(
            temporary / "test.toml",
            project,
            {
                "application": {"database_path": str(temporary / "state.sqlite3")},
                "storage_roots": {"media": str(media)},
                "scanner": {"video_extensions": [".mp4"]},
                "library": {"channels": ["fox"]},
            },
        )
        connection = connect_database(config.database_path)
        try:
            apply_migrations(connection, project / "migrations")
            scanner = LibraryScanner(connection, config, inspector=FakeInspector())
            scanner.scan()
            asset_id = int(connection.execute("SELECT id FROM assets").fetchone()["id"])
            root_id = ensure_storage_root(connection, "media")
            current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            video_hash = hashlib.sha256(b"video-corrected").hexdigest()
            now = utc_now()
            connection.execute(
                """
                INSERT INTO normalization_promotions(
                    asset_id,original_source_relative_path,corrected_source_relative_path,
                    backup_root_id,backup_relative_path,original_sha256,corrected_sha256,
                    status,created_at
                ) VALUES (?,?,?,?,?,?,?,'active',?)
                """,
                (asset_id, "original.mp4", "corrected.mp4", root_id, "original.mp4",
                 hashlib.sha256(b"original").hexdigest(), video_hash, now),
            )
            connection.execute(
                """
                INSERT INTO audio_normalization_promotions(
                    asset_id,source_relative_path,backup_root_id,backup_relative_path,
                    original_sha256,corrected_sha256,status,created_at
                ) VALUES (?,?,?,?,?,?,'active',?)
                """,
                (asset_id, "corrected.mp4", root_id, "audio.mp4",
                 video_hash, current_hash, now),
            )
            connection.execute(
                """
                UPDATE normalization_profiles SET analysis_status='analyzed',
                    detected_mode='pillarboxed_4_3',proposed_mode='pillarboxed_4_3',
                    crop_x=172,crop_y=0,crop_width=936,crop_height=720,
                    proxy_status='failed',proxy_error='invalid crop'
                WHERE asset_id=?
                """,
                (asset_id,),
            )
            connection.commit()

            scanner.scan()
            profile = connection.execute(
                """SELECT analysis_status,detected_mode,crop_width,proxy_status,proxy_error
                   FROM normalization_profiles WHERE asset_id=?""",
                (asset_id,),
            ).fetchone()
            self.assertEqual(profile["analysis_status"], "analyzed")
            self.assertEqual(profile["detected_mode"], "native_4_3")
            self.assertIsNone(profile["crop_width"])
            self.assertEqual(profile["proxy_status"], "promoted")
            self.assertIsNone(profile["proxy_error"])

            connection.execute(
                "DELETE FROM audio_normalization_promotions WHERE asset_id=?", (asset_id,)
            )
            connection.execute(
                "DELETE FROM normalization_promotions WHERE asset_id=?", (asset_id,)
            )
            connection.execute(
                """
                UPDATE normalization_profiles SET analysis_status='analyzed',
                    detected_mode='pillarboxed_4_3', proposed_mode='pillarboxed_4_3',
                    crop_x=172,crop_y=0,crop_width=936,crop_height=720,
                    proxy_status='failed',proxy_error='invalid crop'
                WHERE asset_id=?
                """,
                (asset_id,),
            )
            connection.commit()
            self.assertEqual(scanner._repair_invalid_normalization_geometry(), 1)
            invalid = connection.execute(
                "SELECT analysis_status,crop_width,proxy_status FROM normalization_profiles WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            self.assertEqual(invalid["analysis_status"], "pending")
            self.assertIsNone(invalid["crop_width"])
            self.assertEqual(invalid["proxy_status"], "not_created")
        finally:
            connection.close()
            shutil.rmtree(temporary, ignore_errors=True)

    def test_scanner_persists_relative_paths_and_review_state(self):
        project = Path(__file__).resolve().parents[1]
        test_temp = project / ".test-tmp"
        test_temp.mkdir(exist_ok=True)
        temporary = test_temp / f"scanner-{uuid.uuid4().hex}"
        temporary.mkdir()
        try:
            media = temporary / "media"
            files = [
                media / "fox/night/episodes/The Simpsons/episode.mkv",
                media / "shared/night/commercials/late-ad.mp4",
                media / "nickelodeon/daytime/bumpers/orange-splat.mp4",
                media
                / "shared/specials/holidays/halloween/movies/monster-movie.mp4",
                media
                / "shared/specials/holidays/halloween/blocks/channel-block.mp4",
            ]
            for index, path in enumerate(files):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"fake-video-{index}".encode())
            template_media = (
                media
                / "_channel_template/morning/episodes/_series_name/ignored.mp4"
            )
            template_media.parent.mkdir(parents=True, exist_ok=True)
            template_media.write_bytes(b"must-not-be-indexed")
            audio_partial = (
                media
                / "fox/night/episodes/The Simpsons/.episode.retrotv-audio.partial.mp4"
            )
            audio_partial.write_bytes(b"must-not-be-indexed")
            weekly_graphic = (
                media
                / "nickelodeon/specials/weekly/snick/up-next/background.mp4"
            )
            weekly_graphic.parent.mkdir(parents=True, exist_ok=True)
            weekly_graphic.write_bytes(b"must-not-be-library-media")

            config = AppConfig(
                profile_path=temporary / "test.toml",
                project_root=project,
                data={
                    "profile": "test",
                    "application": {
                        "database_path": str(temporary / "retrotv.sqlite3")
                    },
                    "storage_roots": {"media": str(media)},
                    "scanner": {
                        "video_extensions": [".mp4", ".mkv"],
                        "sample_seconds": 2.0,
                        "sample_end_margin_seconds": 3.0,
                    },
                    "library": {
                        "channels": ["fox", "nickelodeon"],
                        "holidays": ["halloween", "christmas"],
                    },
                    "guide": {
                        "channel_number": 2,
                        "channel_numbers": {"hbo": 55},
                    },
                },
            )
            connection = connect_database(config.database_path)
            try:
                apply_migrations(connection, project / "migrations")
                scanner = LibraryScanner(
                    connection,
                    config,
                    inspector=FakeInspector(),
                )
                summary = scanner.scan()
                self.assertEqual(summary.discovered, 5)
                self.assertEqual(summary.ready, 4)
                self.assertEqual(summary.review, 1)
                self.assertEqual(summary.channels_created, 2)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) count FROM normalization_profiles"
                    ).fetchone()["count"],
                    5,
                )
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT COUNT(*) count FROM normalization_profiles
                        WHERE analysis_status = 'pending'
                        """
                    ).fetchone()["count"],
                    5,
                )
                channels = connection.execute(
                    """
                    SELECT slug, channel_number, enabled
                    FROM channels ORDER BY channel_number
                    """
                ).fetchall()
                self.assertEqual(
                    [dict(row) for row in channels],
                    [
                        {"slug": "fox", "channel_number": 10, "enabled": 1},
                        {
                            "slug": "nickelodeon",
                            "channel_number": 11,
                            "enabled": 1,
                        },
                    ],
                )

                rows = connection.execute(
                    """
                    SELECT source_relative_path, media_type, duration_class,
                           review_status, enabled_for_scheduling
                    FROM assets ORDER BY source_relative_path
                    """
                ).fetchall()
                self.assertEqual(len(rows), 5)
                self.assertFalse(
                    any(
                        row["source_relative_path"].startswith("_channel_template/")
                        for row in rows
                    )
                )
                self.assertTrue(
                    all(not Path(row["source_relative_path"]).is_absolute() for row in rows)
                )
                movie = next(row for row in rows if row["media_type"] == "movie")
                self.assertEqual(movie["duration_class"], "long_duration")
                block = next(
                    row
                    for row in rows
                    if row["source_relative_path"].endswith("channel-block.mp4")
                )
                self.assertEqual(block["review_status"], "open")
                self.assertEqual(block["enabled_for_scheduling"], 0)

                night_ad = connection.execute(
                    """
                    SELECT e.channel_slug, e.daypart_slug
                    FROM asset_eligibility e
                    JOIN assets a ON a.id = e.asset_id
                    WHERE a.source_relative_path LIKE '%late-ad.mp4'
                    """
                ).fetchone()
                self.assertEqual(night_ad["channel_slug"], "*")
                self.assertEqual(night_ad["daypart_slug"], "night")

                daytime_bumper = connection.execute(
                    """
                    SELECT e.daypart_slug
                    FROM asset_eligibility e
                    JOIN assets a ON a.id = e.asset_id
                    WHERE a.source_relative_path LIKE '%orange-splat.mp4'
                    ORDER BY e.daypart_slug
                    """
                ).fetchall()
                self.assertEqual(
                    {row["daypart_slug"] for row in daytime_bumper},
                    {"morning", "afternoon", "evening"},
                )
                self.assertNotIn(
                    "night",
                    {row["daypart_slug"] for row in daytime_bumper},
                )

                hbo = media / "HBO"
                hbo.mkdir()
                second = scanner.scan()
                self.assertEqual(second.unchanged, 5)
                self.assertEqual(second.channels_created, 1)
                hbo_channel = connection.execute(
                    "SELECT display_name, channel_number FROM channels WHERE slug = 'hbo'"
                ).fetchone()
                self.assertEqual(dict(hbo_channel), {"display_name": "HBO", "channel_number": 55})

                # Eligibility is derived from folder placement. If a restored
                # database is missing those rows, an ordinary unchanged-file
                # scan must rebuild them without resetting approved analysis.
                fox_asset = connection.execute(
                    """
                    SELECT id FROM assets
                    WHERE source_relative_path LIKE '%episode.mkv'
                    """
                ).fetchone()["id"]
                connection.execute(
                    """
                    UPDATE normalization_profiles
                    SET analysis_status = 'analyzed', override_mode = 'native_4_3'
                    WHERE asset_id = ?
                    """,
                    (fox_asset,),
                )
                connection.execute(
                    "DELETE FROM asset_eligibility WHERE asset_id = ?", (fox_asset,)
                )
                connection.commit()

                repaired = scanner.scan()
                self.assertEqual(repaired.unchanged, 5)
                restored_eligibility = connection.execute(
                    """
                    SELECT channel_slug, daypart_slug FROM asset_eligibility
                    WHERE asset_id = ?
                    """,
                    (fox_asset,),
                ).fetchall()
                self.assertEqual(
                    {(row["channel_slug"], row["daypart_slug"]) for row in restored_eligibility},
                    {("fox", "night")},
                )
                preserved_profile = connection.execute(
                    """
                    SELECT analysis_status, override_mode FROM normalization_profiles
                    WHERE asset_id = ?
                    """,
                    (fox_asset,),
                ).fetchone()
                self.assertEqual(
                    dict(preserved_profile),
                    {"analysis_status": "analyzed", "override_mode": "native_4_3"},
                )

                files[0].unlink()
                shutil.rmtree(hbo)
                third = scanner.scan()
                self.assertEqual(third.missing, 1)
                self.assertEqual(third.channels_disabled, 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT enabled FROM channels WHERE slug = 'hbo'"
                    ).fetchone()["enabled"],
                    0,
                )
                missing = connection.execute(
                    """
                    SELECT integrity_status, review_status, enabled_for_scheduling
                    FROM assets
                    WHERE source_relative_path LIKE '%episode.mkv'
                    """
                ).fetchone()
                self.assertEqual(missing["integrity_status"], "missing")
                self.assertEqual(missing["review_status"], "ignored")
                self.assertEqual(missing["enabled_for_scheduling"], 0)
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM review_items
                        WHERE asset_id = (
                            SELECT id FROM assets
                            WHERE source_relative_path LIKE '%episode.mkv'
                        ) AND status = 'open'
                        """
                    ).fetchone()["count"],
                    0,
                )

                # A retired row is not reported as a fresh deletion forever.
                fourth = scanner.scan()
                self.assertEqual(fourth.missing, 0)
            finally:
                connection.close()
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
