import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.normalization import Crop, NormalizationAnalyzer, NormalizationService


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        test_temp = self.project / ".test-tmp"
        test_temp.mkdir(exist_ok=True)
        self.temporary = test_temp / f"normalization-{uuid.uuid4().hex}"
        self.media = self.temporary / "media"
        self.media.mkdir(parents=True)
        self.config = AppConfig(
            profile_path=self.temporary / "test.toml",
            project_root=self.project,
            data={
                "application": {
                    "database_path": str(self.temporary / "retrotv.sqlite3")
                },
                "storage_roots": {
                    "media": str(self.media),
                    "normalized": str(self.temporary / "normalized"),
                    "previews": str(self.temporary / "previews"),
                },
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                "normalization": {"sample_count": 5},
            },
        )

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    @staticmethod
    def media_info(width, height, dar, duration=1200.0):
        return {
            "width": width,
            "height": height,
            "display_aspect_ratio": dar,
            "sample_aspect_ratio": "1:1",
            "duration_seconds": duration,
        }

    def test_native_4_3_is_automatic(self):
        analyzer = NormalizationAnalyzer(self.config)
        result = analyzer.analyze(
            Path("unused"),
            self.media_info(640, 480, "4:3"),
            crop_provider=lambda path, offset: Crop(640, 480, 0, 0),
        )
        self.assertEqual(result.status, "analyzed")
        self.assertEqual(result.detected_mode, "native_4_3")
        self.assertGreater(result.confidence, 0.9)

    def test_pillarboxed_4_3_uses_consistent_crop(self):
        analyzer = NormalizationAnalyzer(self.config)
        result = analyzer.analyze(
            Path("unused"),
            self.media_info(1920, 1080, "16:9"),
            crop_provider=lambda path, offset: Crop(1440, 1080, 240, 0),
        )
        self.assertEqual(result.status, "analyzed")
        self.assertEqual(result.detected_mode, "pillarboxed_4_3")
        self.assertEqual(result.crop, Crop(1440, 1080, 240, 0))

    def test_pillarboxed_stretched_mode_crops_before_stretching(self):
        service = NormalizationService(None, self.config)
        profile = {
            "analysis_status": "analyzed",
            "override_mode": "pillarboxed_stretched_4_3",
            "crop_width": 478,
            "crop_height": 482,
            "crop_x": 80,
            "crop_y": 0,
        }
        self.assertEqual(
            service.video_filter(profile),
            "crop=478:482:80:0,scale=640:480,setsar=1",
        )

    def test_full_frame_16_9_is_flagged_as_possible_stretch(self):
        analyzer = NormalizationAnalyzer(self.config)
        result = analyzer.analyze(
            Path("unused"),
            self.media_info(1920, 1080, "16:9"),
            crop_provider=lambda path, offset: Crop(1920, 1080, 0, 0),
        )
        self.assertEqual(result.status, "review")
        self.assertEqual(result.detected_mode, "ambiguous")
        self.assertEqual(result.proposed_mode, "stretched_4_3")

    def test_doubled_width_640_by_240_is_corrected_as_stretched_4_3(self):
        analyzer = NormalizationAnalyzer(self.config)
        samples = iter(
            [
                Crop(640, 240, 0, 0),
                Crop(640, 240, 0, 0),
                Crop(540, 228, 58, 0),
                Crop(640, 240, 0, 0),
                Crop(640, 240, 0, 0),
            ]
        )
        result = analyzer.analyze(
            Path("unused"),
            self.media_info(640, 240, "8:3", duration=30.0),
            crop_provider=lambda path, offset: next(samples),
        )
        self.assertEqual(result.status, "analyzed")
        self.assertEqual(result.detected_mode, "stretched_4_3")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_override_changes_filter_and_marks_existing_proxy_stale(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, first_seen_at, last_seen_at
                ) VALUES (?, 'fox/night/episodes/test.mp4', 'test.mp4',
                          1, 1, ?, ?)
                """,
                (root_id, now, now),
            )
            asset_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO normalization_profiles(
                    asset_id, analysis_status, detected_mode, proposed_mode,
                    override_mode, proxy_status
                ) VALUES (?, 'review', 'ambiguous', 'stretched_4_3',
                          'auto', 'ready')
                """,
                (asset_id,),
            )
            connection.commit()
            service = NormalizationService(connection, self.config)
            initial = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            with self.assertRaises(ValueError):
                service.video_filter(initial)
            self.assertEqual(
                service.video_filter(initial, allow_proposed=True),
                "scale=640:480,setsar=1",
            )
            service.set_override(asset_id, "stretched_4_3")
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            self.assertEqual(profile["proxy_status"], "stale")
            self.assertEqual(
                service.video_filter(profile),
                "scale=640:480,setsar=1",
            )
        finally:
            connection.close()

    def test_folder_review_applies_to_exact_folder_and_approves_all_targets(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            paths = [
                "mtv/night/episodes/Show/Season 1/episode1.mp4",
                "mtv/night/episodes/Show/Season 1/episode2.mkv",
                "mtv/night/episodes/Show/Season 2/episode3.mp4",
            ]
            asset_ids = []
            for relative in paths:
                cursor = connection.execute(
                    """
                    INSERT INTO assets(
                        source_root_id, source_relative_path, original_filename,
                        file_size, modified_ns, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, 1, 1, ?, ?)
                    """,
                    (root_id, relative, Path(relative).name, now, now),
                )
                target_id = int(cursor.lastrowid)
                asset_ids.append(target_id)
                connection.execute(
                    """
                    INSERT INTO asset_media_info(
                        asset_id, duration_seconds, width, height,
                        sample_aspect_ratio, display_aspect_ratio
                    ) VALUES (?, 1200, 1920, 1080, '1:1', '16:9')
                    """,
                    (target_id,),
                )
                connection.execute(
                    """
                    INSERT INTO normalization_profiles(
                        asset_id, analysis_status, detected_mode,
                        proposed_mode, override_mode
                    ) VALUES (?, 'review', 'ambiguous', 'stretched_4_3', 'auto')
                    """,
                    (target_id,),
                )
            connection.commit()

            result = NormalizationService(
                connection, self.config
            ).approve_folder_review(asset_ids[0], "stretched_4_3")

            self.assertEqual(result["updated_count"], 2)
            self.assertEqual(result["asset_ids"], asset_ids[:2])
            profiles = connection.execute(
                """
                SELECT asset_id, analysis_status, override_mode
                FROM normalization_profiles ORDER BY asset_id
                """
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in profiles],
                [
                    (asset_ids[0], "analyzed", "stretched_4_3"),
                    (asset_ids[1], "analyzed", "stretched_4_3"),
                    (asset_ids[2], "review", "auto"),
                ],
            )
        finally:
            connection.close()

    def test_folder_crop_is_atomic_when_one_video_has_incompatible_geometry(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            asset_ids = []
            for index, width in enumerate((1920, 640), start=1):
                relative = f"fox/evening/episodes/Show/episode{index}.mp4"
                cursor = connection.execute(
                    """
                    INSERT INTO assets(
                        source_root_id, source_relative_path, original_filename,
                        file_size, modified_ns, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, 1, 1, ?, ?)
                    """,
                    (root_id, relative, Path(relative).name, now, now),
                )
                target_id = int(cursor.lastrowid)
                asset_ids.append(target_id)
                connection.execute(
                    """
                    INSERT INTO asset_media_info(
                        asset_id, duration_seconds, width, height,
                        sample_aspect_ratio, display_aspect_ratio
                    ) VALUES (?, 1200, ?, 1080, '1:1', '16:9')
                    """,
                    (target_id, width),
                )
                connection.execute(
                    """
                    INSERT INTO normalization_profiles(
                        asset_id, analysis_status, detected_mode,
                        proposed_mode, override_mode
                    ) VALUES (?, 'review', 'ambiguous', 'pillarboxed_4_3', 'auto')
                    """,
                    (target_id,),
                )
            connection.commit()

            service = NormalizationService(connection, self.config)
            with self.assertRaisesRegex(ValueError, "episode2.mp4"):
                service.approve_folder_review(
                    asset_ids[0], "pillarboxed_4_3",
                    crop=Crop(1440, 1080, 240, 0),
                )
            rows = connection.execute(
                """
                SELECT analysis_status, override_mode
                FROM normalization_profiles ORDER BY asset_id
                """
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [("review", "auto"), ("review", "auto")],
            )
        finally:
            connection.close()

    def test_reanalyzing_restored_original_marks_promotion_stale(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            relative = "fox/morning/blocks/restored.mp4"
            source = self.media / Path(relative)
            source.parent.mkdir(parents=True)
            source.write_bytes(b"restored")
            cursor = connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'restored.mp4', ?, 1, ?, ?)
                """,
                (root_id, relative, source.stat().st_size, now, now),
            )
            asset_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO asset_media_info(
                    asset_id, duration_seconds, width, height,
                    display_aspect_ratio
                ) VALUES (?, 30, 850, 480, '85:48')
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO normalization_profiles(
                    asset_id, analysis_status, detected_mode, proposed_mode,
                    override_mode, proxy_status
                ) VALUES (?, 'analyzed', 'native_4_3', 'native_4_3',
                          'auto', 'promoted')
                """,
                (asset_id,),
            )
            connection.commit()

            result = NormalizationService(
                connection, self.config
            ).analyze_asset(
                asset_id,
                crop_provider=lambda path, offset: Crop(640, 480, 105, 0),
            )
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()

            self.assertEqual(result.detected_mode, "pillarboxed_4_3")
            self.assertEqual(profile["proxy_status"], "stale")
        finally:
            connection.close()

    def test_pillarbox_override_requires_and_validates_crop(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, first_seen_at, last_seen_at
                ) VALUES (?, 'shared/all-day/filler/windowbox.mp4',
                          'windowbox.mp4', 1, 1, ?, ?)
                """,
                (root_id, now, now),
            )
            asset_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO asset_media_info(
                    asset_id, duration_seconds, width, height,
                    display_aspect_ratio
                ) VALUES (?, 30, 480, 360, '4:3')
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO normalization_profiles(
                    asset_id, analysis_status, detected_mode, proposed_mode
                ) VALUES (?, 'review', 'ambiguous', 'preserve_frame')
                """,
                (asset_id,),
            )
            connection.commit()
            service = NormalizationService(connection, self.config)
            with self.assertRaisesRegex(ValueError, "requires a crop rectangle"):
                service.set_override(asset_id, "pillarboxed_4_3")
            with self.assertRaisesRegex(ValueError, "outside"):
                service.set_override(
                    asset_id,
                    "custom",
                    crop=Crop(480, 360, 60, 45),
                )
            service.set_override(
                asset_id,
                "custom",
                crop=Crop(358, 270, 60, 45),
            )
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            self.assertEqual(
                service.video_filter(profile),
                "crop=358:270:60:45,"
                "scale=640:480:force_original_aspect_ratio=decrease,"
                "pad=640:480:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
            )
            service.set_override(asset_id, "native_4_3")
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            self.assertEqual(profile["override_mode"], "native_4_3")
            self.assertIsNone(profile["crop_x"])
            self.assertIsNone(profile["crop_y"])
            self.assertIsNone(profile["crop_width"])
            self.assertIsNone(profile["crop_height"])
            self.assertEqual(profile["proxy_status"], "stale")
        finally:
            connection.close()

    def test_approving_aspect_review_freezes_proposed_mode(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, first_seen_at, last_seen_at
                ) VALUES (?, 'shared/all-day/filler/stretched.mp4',
                          'stretched.mp4', 1, 1, ?, ?)
                """,
                (root_id, now, now),
            )
            asset_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO asset_media_info(
                    asset_id, duration_seconds, width, height,
                    display_aspect_ratio
                ) VALUES (?, 30, 640, 360, '16:9')
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO normalization_profiles(
                    asset_id, analysis_status, detected_mode, proposed_mode,
                    source_preview_relative_path, normalized_preview_relative_path
                ) VALUES (?, 'review', 'ambiguous', 'stretched_4_3', ?, ?)
                """,
                (asset_id, f"{asset_id}-source.jpg", f"{asset_id}-normalized.jpg"),
            )
            connection.commit()

            preview_root = self.config.storage_root("previews")
            preview_root.mkdir(parents=True, exist_ok=True)
            source_preview = preview_root / f"{asset_id}-source.jpg"
            normalized_preview = preview_root / f"{asset_id}-normalized.jpg"
            source_preview.write_bytes(b"source preview")
            normalized_preview.write_bytes(b"normalized preview")

            service = NormalizationService(connection, self.config)
            self.assertEqual(service.approve_review(asset_id), "stretched_4_3")
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            self.assertEqual(profile["analysis_status"], "analyzed")
            self.assertEqual(profile["override_mode"], "stretched_4_3")
            self.assertIsNone(profile["source_preview_relative_path"])
            self.assertIsNone(profile["normalized_preview_relative_path"])
            self.assertFalse(source_preview.exists())
            self.assertFalse(normalized_preview.exists())
        finally:
            connection.close()

    def test_corrected_preview_is_a_draft_until_exact_result_is_approved(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, metadata_status, integrity_status,
                    first_seen_at, last_seen_at
                ) VALUES (?, 'fox/all-day/episodes/draft.mp4', 'draft.mp4',
                          100, 123, 'ready', 'ready', ?, ?)
                """,
                (root_id, now, now),
            )
            asset_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO asset_media_info(
                    asset_id, duration_seconds, width, height,
                    sample_aspect_ratio, display_aspect_ratio
                ) VALUES (?, 1200, 1920, 1080, '1:1', '16:9')
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO normalization_profiles(
                    asset_id, analysis_status, detected_mode,
                    proposed_mode, override_mode
                ) VALUES (?, 'review', 'ambiguous', 'stretched_4_3', 'auto')
                """,
                (asset_id,),
            )
            connection.commit()
            service = NormalizationService(connection, self.config)

            def render(row, target, *, video_filter=None, position=0.35):
                target.write_bytes(
                    f"{position}:{video_filter or 'source'}".encode("utf-8")
                )

            with patch.object(service, "_render_preview_frame", side_effect=render):
                preview = service.create_corrected_preview(
                    asset_id, "stretched_4_3", frame_count=3
                )

            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id=?", (asset_id,)
            ).fetchone()
            self.assertEqual(profile["analysis_status"], "review")
            self.assertEqual(profile["override_mode"], "auto")
            self.assertEqual(service.preview_recipe(asset_id)["token"], preview["token"])
            self.assertEqual(preview["frame_count"], 3)
            self.assertEqual(len(preview["frames"]), 3)

            approved = service.approve_preview(asset_id, preview["token"])
            self.assertEqual(approved, "stretched_4_3")
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id=?", (asset_id,)
            ).fetchone()
            self.assertEqual(profile["analysis_status"], "analyzed")
            self.assertEqual(profile["override_mode"], "stretched_4_3")
            self.assertFalse(Path(preview["source"]).exists())
            self.assertFalse(Path(preview["normalized"]).exists())
            self.assertFalse(service._preview_recipe_path(asset_id).exists())
            self.assertFalse(
                (self.config.storage_root("previews") / f"{asset_id}-source-1.jpg").exists()
            )
        finally:
            connection.close()

    def test_changed_source_invalidates_corrected_preview_approval(self):
        connection = connect_database(self.config.database_path)
        try:
            apply_migrations(connection, self.project / "migrations")
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, metadata_status, integrity_status,
                    first_seen_at, last_seen_at
                ) VALUES (?, 'fox/all-day/episodes/changed.mp4', 'changed.mp4',
                          100, 123, 'ready', 'ready', ?, ?)
                """,
                (root_id, now, now),
            )
            asset_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO asset_media_info(
                    asset_id, duration_seconds, width, height,
                    sample_aspect_ratio, display_aspect_ratio
                ) VALUES (?, 1200, 1920, 1080, '1:1', '16:9')
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO normalization_profiles(
                    asset_id, analysis_status, detected_mode,
                    proposed_mode, override_mode
                ) VALUES (?, 'review', 'ambiguous', 'preserve_frame', 'auto')
                """,
                (asset_id,),
            )
            connection.commit()
            service = NormalizationService(connection, self.config)

            def render(row, target, *, video_filter=None):
                target.write_bytes(b"preview")

            with patch.object(service, "_render_preview_frame", side_effect=render):
                preview = service.create_corrected_preview(
                    asset_id, "preserve_frame"
                )
            connection.execute(
                "UPDATE assets SET modified_ns=124 WHERE id=?", (asset_id,)
            )
            connection.commit()

            self.assertIsNone(service.preview_recipe(asset_id))
            with self.assertRaisesRegex(ValueError, "missing or stale"):
                service.approve_preview(asset_id, preview["token"])
            profile = connection.execute(
                "SELECT * FROM normalization_profiles WHERE asset_id=?", (asset_id,)
            ).fetchone()
            self.assertEqual(profile["analysis_status"], "review")
            self.assertEqual(profile["override_mode"], "auto")
        finally:
            connection.close()

    def test_crop_suggestion_fits_four_by_three_inside_detected_borders(self):
        crop = NormalizationService.suggest_crop(
            {
                "width": 480,
                "height": 360,
                "crop_width": None,
                "crop_height": None,
                "crop_x": None,
                "crop_y": None,
                "sample_results_json": json.dumps(
                    [
                        {"crop": {"width": 358, "height": 360, "x": 60, "y": 0}},
                        {"crop": {"width": 356, "height": 360, "x": 60, "y": 0}},
                        {"crop": {"width": 358, "height": 360, "x": 60, "y": 0}},
                        {"crop": {"width": 480, "height": 360, "x": 0, "y": 0}},
                    ]
                ),
            }
        )
        self.assertEqual(crop, Crop(358, 270, 60, 45))

    def test_output_canvas_does_not_upscale_small_media(self):
        service = NormalizationService(None, self.config)
        native = {
            "analysis_status": "analyzed",
            "detected_mode": "native_4_3",
            "override_mode": "auto",
        }
        self.assertEqual(
            service.output_canvas({"width": 320, "height": 240}, native),
            (320, 240),
        )
        stretched = {
            "analysis_status": "analyzed",
            "detected_mode": "ambiguous",
            "override_mode": "stretched_4_3",
        }
        self.assertEqual(
            service.output_canvas({"width": 640, "height": 240}, stretched),
            (320, 240),
        )
        self.assertEqual(
            service.output_canvas({"width": 1920, "height": 1080}, stretched),
            (640, 480),
        )

    def test_output_canvas_wraps_small_crop_without_enlarging_picture(self):
        service = NormalizationService(None, self.config)
        profile = {
            "analysis_status": "analyzed",
            "detected_mode": "ambiguous",
            "override_mode": "custom",
            "crop_width": 358,
            "crop_height": 270,
        }
        self.assertEqual(
            service.output_canvas({"width": 480, "height": 360}, profile),
            (360, 270),
        )

    def test_encoding_rate_limits_follow_source_file_bitrate(self):
        service = NormalizationService(None, self.config)
        audio_kbps, video_kbps = service.encoding_rate_limits(
            {
                "file_size": 690_032_374,
                "duration_seconds": 14_290.024,
            }
        )
        self.assertEqual(audio_kbps, 117)
        self.assertGreaterEqual(video_kbps, 260)
        self.assertLessEqual(video_kbps, 270)

    def test_retry_rate_limits_follow_measured_overshoot(self):
        service = NormalizationService(None, self.config)
        audio_kbps, video_kbps = service.retry_rate_limits(
            128,
            500,
            source_size=100,
            output_size=150,
        )
        self.assertEqual(audio_kbps, 84)
        self.assertEqual(video_kbps, 330)

    def test_oversized_proxy_retries_with_constrained_average_bitrate(self):
        connection = Mock()
        service = NormalizationService(connection, self.config)
        row = {
            "id": 44,
            "channel_slug": "fox",
            "source_relative_path": "fox/night/episodes/Test/oversized.mp4",
            "file_size": 100,
            "duration_seconds": 30.0,
            "width": 640,
            "height": 360,
        }
        profile = {
            "analysis_status": "analyzed",
            "detected_mode": "stretched_4_3",
            "override_mode": "auto",
        }
        service._asset = Mock(return_value=row)
        service._profile = Mock(return_value=profile)
        service._validate_proxy = Mock(return_value=None)
        encoded_sizes = iter((120, 100))
        commands = []

        def fake_run(args, timeout):
            commands.append(args)
            Path(args[-1]).write_bytes(b"x" * next(encoded_sizes))
            return Mock(returncode=0, stderr="")

        probe = Mock(ok=True, metadata={"duration_seconds": 30.0})
        with (
            patch("retrotv.normalization._run", side_effect=fake_run),
            patch("retrotv.normalization.probe_media", return_value=probe),
            patch("retrotv.normalization.ensure_storage_root", return_value=2),
        ):
            result = service.create_proxy(44)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(commands), 2)
        self.assertIn("-crf", commands[0])
        self.assertNotIn("-b:v", commands[0])
        self.assertIn("-b:v", commands[1])
        self.assertNotIn("-crf", commands[1])
        self.assertIsNone(result.warning)

    def test_valid_correction_is_kept_when_size_retries_are_exhausted(self):
        connection = Mock()
        service = NormalizationService(connection, self.config)
        service.size_retry_attempts = 1
        row = {
            "id": 45,
            "channel_slug": "fox",
            "source_relative_path": "fox/night/episodes/Test/still-large.mp4",
            "file_size": 100,
            "duration_seconds": 30.0,
            "width": 640,
            "height": 360,
        }
        profile = {
            "analysis_status": "analyzed",
            "detected_mode": "stretched_4_3",
            "override_mode": "auto",
        }
        service._asset = Mock(return_value=row)
        service._profile = Mock(return_value=profile)
        service._validate_proxy = Mock(return_value=None)
        encoded_sizes = iter((120, 115))

        def fake_run(args, timeout):
            Path(args[-1]).write_bytes(b"x" * next(encoded_sizes))
            return Mock(returncode=0, stderr="")

        probe = Mock(ok=True, metadata={"duration_seconds": 30.0})
        with (
            patch("retrotv.normalization._run", side_effect=fake_run),
            patch("retrotv.normalization.probe_media", return_value=probe),
            patch("retrotv.normalization.ensure_storage_root", return_value=2),
        ):
            result = service.create_proxy(45)

        self.assertTrue(result.ok, result.error)
        self.assertIn("Accepted corrected output", result.warning)
        self.assertFalse(service._validate_proxy.call_args.kwargs["enforce_size"])

    def test_proxy_larger_than_configured_source_ratio_is_rejected_early(self):
        service = NormalizationService(None, self.config)
        proxy = self.temporary / "oversized.mp4"
        proxy.write_bytes(b"x" * 111)
        error = service._validate_proxy(
            proxy,
            10.0,
            (640, 480),
            source_size=100,
        )
        self.assertIn("1.10x source-size limit", error)


if __name__ == "__main__":
    unittest.main()
