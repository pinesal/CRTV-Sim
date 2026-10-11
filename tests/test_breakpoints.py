import shutil
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now


class BreakpointTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        root = self.project / ".test-tmp" / f"breakpoints-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        self.root = root
        self.media = root / "media"
        source = self.media / "fox/all-day/episodes/Show/episode.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        self.config = AppConfig(
            root / "test.toml", self.project,
            {
                "application": {"database_path": str(root / "db.sqlite3")},
                "storage_roots": {
                    "media": str(self.media), "normalized": str(root / "normalized"),
                    "previews": str(root / "previews"),
                },
                "scheduling": {
                    "midroll_min_media_minutes": 15,
                    "midroll_search_window_minutes": 4,
                    "midroll_min_segment_minutes": 5,
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        root_id = ensure_storage_root(self.connection, "media")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO assets(
                id,source_root_id,source_relative_path,original_filename,
                file_size,modified_ns,media_type,display_title,series_title,
                channel_slug,daypart_slug,metadata_status,integrity_status,
                review_status,enabled_for_scheduling,first_seen_at,last_seen_at
            ) VALUES (1,?,'fox/all-day/episodes/Show/episode.mp4','episode.mp4',
                      5,10,'episode','Episode','Show','fox','all-day',
                      'ready','ready','ready',1,?,?)
            """,
            (root_id, now, now),
        )
        self.connection.execute(
            "INSERT INTO asset_media_info(asset_id,duration_seconds) VALUES (1,1380)"
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_black_and_silent_fade_is_preferred_near_midpoint(self):
        output = "\n".join(
            [
                "[blackdetect] black_start:238.0 black_end:240.0 black_duration:2.0",
                "[silencedetect] silence_start:237.8",
                "[silencedetect] silence_end:240.2 | silence_duration:2.4",
                "[showinfo] n:1 pts:1 pts_time:100.0",
            ]
        )
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value=output):
            result = service.analyze_asset(1)
        # The scan starts four minutes before the 11:30 midpoint, so 239s is 11:29.
        self.assertEqual(result.method, "fade")
        self.assertEqual(result.offset_ms, 689000)
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(service.selected_offset(1), 689000)

    def test_pending_analysis_prioritizes_active_upcoming_schedule(self):
        now = datetime.now(UTC)
        self.connection.execute(
            "INSERT INTO channels(id,slug,display_name) VALUES (1,'fox','Fox')"
        )
        for asset_id in (2, 3, 4):
            self.connection.execute(
                """INSERT INTO assets(
                    id,source_root_id,source_relative_path,original_filename,
                    file_size,modified_ns,media_type,display_title,series_title,
                    channel_slug,daypart_slug,metadata_status,integrity_status,
                    review_status,enabled_for_scheduling,first_seen_at,last_seen_at)
                    SELECT ?,source_root_id,source_relative_path || ?,original_filename,
                           file_size,modified_ns,media_type,display_title,series_title,
                           channel_slug,daypart_slug,metadata_status,integrity_status,
                           review_status,enabled_for_scheduling,first_seen_at,last_seen_at
                    FROM assets WHERE id=1""",
                (asset_id, f"-{asset_id}"),
            )
            self.connection.execute(
                "INSERT INTO asset_media_info(asset_id,duration_seconds) VALUES (?,1380)",
                (asset_id,),
            )
        for generation_id, status in ((10, "active"), (11, "superseded")):
            self.connection.execute(
                """INSERT INTO schedule_generations(
                    id,channel_id,schedule_date,seed,configuration_fingerprint,
                    status,validation_status,generated_at)
                    VALUES (?,1,?,?,? ,?,'valid',?)""",
                (generation_id, now.date().isoformat(), f"seed-{generation_id}",
                 f"fingerprint-{generation_id}", status, utc_now()),
            )
        for asset_id, generation_id, start in (
            (2, 10, now + timedelta(hours=2)),
            (3, 10, now + timedelta(hours=1)),
            (4, 11, now + timedelta(minutes=10)),
            (1, 10, now - timedelta(hours=2)),
        ):
            self.connection.execute(
                """INSERT INTO schedule_entries(
                    generation_id,channel_id,asset_id,starts_at,ends_at,
                    media_start_ms,media_end_ms,entry_type,title,daypart_slug,
                    sequence_position,is_program)
                    VALUES (?,1,?,?,?,?,?,'episode','Episode','all-day',?,1)""",
                (generation_id, asset_id, start.isoformat(timespec="milliseconds"),
                 (start + timedelta(minutes=23)).isoformat(timespec="milliseconds"),
                 0, 1380000, asset_id),
            )
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        self.assertEqual([row["id"] for row in service.pending_assets(limit=2)], [3, 2])
        self.assertEqual([row["id"] for row in service.pending_assets()], [3, 2, 1, 4])

    def test_chapter_bonus_requires_nearby_natural_transition(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        candidates = [
            {"offset_ms": 675000, "method": "fade", "score": 0.70,
             "signals": {"black_duration": 0.5, "silence": True}},
            {"offset_ms": 690000, "method": "scene", "score": 0.62,
             "signals": {"scene_change": True}},
        ]
        service._apply_chapter_hints(candidates, [674000, 690000, 930000], 450000, 930000)
        self.assertAlmostEqual(candidates[0]["score"], 0.82)
        self.assertTrue(candidates[0]["signals"]["chapter_supported"])
        self.assertEqual(candidates[0]["offset_ms"], 675000)
        self.assertTrue(candidates[1]["signals"]["chapter_supported"])
        self.assertAlmostEqual(candidates[1]["score"], 0.68)

    def test_analyzed_break_records_chapter_evidence(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        output = "[blackdetect] black_start:238.0 black_end:240.0 black_duration:2.0"
        with patch.object(service, "_run_detection", return_value=output), \
             patch.object(service, "_chapter_starts", return_value=[689500]):
            service.analyze_asset(1)
        row = self.connection.execute(
            "SELECT signals_json FROM asset_break_candidates WHERE asset_id=1 AND selected=1"
        ).fetchone()
        self.assertIn('"chapter_supported": true', row["signals_json"])

    def test_detector_failure_does_not_invent_midpoint_fallback(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", side_effect=RuntimeError("decode failed")):
            result = service.analyze_asset(1)
        self.assertEqual(result.method, "none")
        self.assertEqual(result.status, "error")
        self.assertEqual(service.selected_offsets(1), [])
        row = self.connection.execute(
            "SELECT status,detail FROM asset_break_analyses WHERE asset_id=1"
        ).fetchone()
        self.assertEqual(row["status"], "error")
        self.assertIn("decode failed", row["detail"])

    def test_long_episode_scans_each_twelve_minute_act_target(self):
        self.connection.execute(
            "UPDATE asset_media_info SET duration_seconds=3600 WHERE asset_id=1"
        )
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value="") as detector:
            result = service.analyze_asset(1)
        self.assertEqual(result.method, "midpoint")
        self.assertEqual(service.selected_offsets(1), [720000, 1440000, 2160000, 2880000])
        self.assertEqual(detector.call_count, 4)

    def test_break_targets_scale_with_full_program_duration(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        self.assertEqual(len(service.target_offsets(23 * 60_000)), 1)
        self.assertEqual(service.target_offsets(44 * 60_000), [660000, 1500000, 2070000])
        self.assertEqual(len(service.target_offsets(70 * 60_000)), 5)
        self.assertEqual(len(service.target_offsets(90 * 60_000)), 7)
        self.assertEqual(len(service.target_offsets(120 * 60_000)), 9)

    def test_prior_version_manual_break_survives_reanalysis_version_change(self):
        self.connection.execute(
            """INSERT INTO asset_break_analyses(
                   asset_id,source_signature,status,selected_offset_ms,
                   selected_method,confidence,analyzed_at
               ) VALUES (1,'5:10:breaks-v2','manual',600000,'manual',1.0,?)""",
            (utc_now(),),
        )
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        self.assertEqual(service.selected_offsets(1), [])
        self.assertEqual([row["id"] for row in service.pending_assets()], [1])

    def test_prior_automatic_analysis_refreshes_for_chapter_hints(self):
        self.connection.execute(
            """INSERT INTO asset_break_analyses(
                   asset_id,source_signature,status,selected_offset_ms,
                   selected_method,confidence,analyzed_at
               ) VALUES (1,'5:10:breaks-v2','analyzed',690000,'midpoint',0.25,?)""",
            (utc_now(),),
        )
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        self.assertEqual(service.selected_offsets(1), [])
        self.assertEqual([row["id"] for row in service.pending_assets()], [1])

        self.connection.execute(
            "UPDATE asset_media_info SET duration_seconds=2640 WHERE asset_id=1"
        )
        self.connection.commit()
        self.assertEqual(service.selected_offsets(1), [])
        self.assertEqual([row["id"] for row in service.pending_assets()], [1])

    def test_manual_break_must_leave_five_minutes_on_each_side(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        with self.assertRaises(ValueError):
            service.set_manual(1, 120000)
        result = service.set_manual(1, 600000)
        self.assertEqual(result.method, "manual")
        self.assertEqual(service.selected_offset(1), 600000)

    def test_manual_target_keeps_other_analyzed_breaks_and_can_reset(self):
        self.connection.execute("UPDATE asset_media_info SET duration_seconds=2640 WHERE asset_id=1")
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value=""):
            service.analyze_asset(1)
            service.set_manual(1, 1350000, target_index=2)
            self.assertEqual(service.selected_offsets(1), [660000, 1350000, 2070000])
            service.reset_manual(1, 2)
        self.assertEqual(service.selected_offsets(1), [660000, 1500000, 2070000])

    def test_scene_without_silence_or_chapter_is_not_natural_break(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value="[showinfo] pts_time:240.0"):
            service.analyze_asset(1)
        row = self.connection.execute(
            "SELECT method,signals_json FROM asset_break_candidates WHERE asset_id=1 AND selected=1"
        ).fetchone()
        self.assertEqual(row["method"], "midpoint")
        self.assertIn("no natural cue", row["signals_json"])

    def test_long_programs_analyze_later_windows(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        for minutes, expected in ((70, 5), (90, 7), (120, 9)):
            with self.subTest(minutes=minutes):
                self.connection.execute(
                    "UPDATE asset_media_info SET duration_seconds=? WHERE asset_id=1",
                    (minutes * 60,),
                )
                self.connection.commit()
                with patch.object(service, "_run_detection", return_value="") as detector:
                    service.analyze_asset(1)
                self.assertEqual(detector.call_count, expected)
                self.assertEqual(len(service.selected_offsets(1)), expected)

    def test_scene_with_nearby_silence_is_selected(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        output = "[silencedetect] silence_start:239.7\n[silencedetect] silence_end:240.2\n[showinfo] pts_time:240.0"
        with patch.object(service, "_run_detection", return_value=output):
            service.analyze_asset(1)
        row = self.connection.execute(
            "SELECT method,signals_json FROM asset_break_candidates WHERE asset_id=1 AND selected=1"
        ).fetchone()
        self.assertEqual(row["method"], "scene")
        self.assertIn('"silence": true', row["signals_json"])

    def test_disabling_fallback_excludes_previously_recorded_midpoint(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value=""):
            service.analyze_asset(1)
        self.assertEqual(len(service.selected_offsets(1)), 1)
        self.config.data["scheduling"]["midroll_fallback"] = "none"
        self.assertEqual(service.selected_offsets(1), [])


if __name__ == "__main__":
    unittest.main()
