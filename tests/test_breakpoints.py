import json
import shutil
import subprocess
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.web import make_handler


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

    def test_quiet_pause_is_recorded_before_midpoint(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        output = "silence_start:239.6\nsilence_end:240.4"
        with patch.object(service, "_run_detection", return_value=output):
            result = service.analyze_asset(1)
        self.assertEqual((result.method, result.offset_ms), ("quiet_pause", 690000))
        row = self.connection.execute(
            "SELECT signals_json FROM asset_break_candidates WHERE selected=1"
        ).fetchone()
        signals = json.loads(row[0])
        self.assertTrue(signals["fallback"])
        self.assertEqual(signals["silence_duration_ms"], 800)
        self.assertEqual(signals["silence_start_ms"], 689600)
        self.assertEqual(signals["silence_threshold_db"], -35)
        self.assertEqual(service.selected_offsets(1), [690000])

    def test_quiet_pause_requires_complete_unclipped_half_second(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        for output in ("silence_start:239.8\nsilence_end:240.299",
                       "silence_start:0\nsilence_end:2",
                       "silence_start:479\nsilence_end:480",
                       "silence_start:479\nsilence_end:479.999",
                       "silence_start:0.001\nsilence_end:2",
                       "silence_start:240", "silence_end:240",
                       "silence_start:-1\nsilence_end:2"):
            with self.subTest(output=output), patch.object(service, "_run_detection", return_value=output):
                self.assertEqual(service.analyze_asset(1).method, "midpoint")
        with patch.object(service, "_run_detection", return_value="silence_start:239.75\nsilence_end:240.25"):
            self.assertEqual(service.analyze_asset(1).method, "quiet_pause")

    def test_natural_cue_takes_precedence_over_closer_quiet_pause(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        for cue in ("black_start:200 black_end:201 black_duration:1",
                    "[showinfo] pts_time:240.0"):
            output = cue + "\nsilence_start:239.6\nsilence_end:240.4"
            with self.subTest(cue=cue), patch.object(service, "_run_detection", return_value=output):
                self.assertIn(service.analyze_asset(1).method, {"fade", "scene"})

    def test_quiet_pause_ties_choose_longer_then_earlier(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        for output, expected in (
            ("silence_start:220\nsilence_end:230\nsilence_start:249\nsilence_end:261", 705000),
            ("silence_start:250\nsilence_end:260\nsilence_start:220\nsilence_end:230", 675000),
        ):
            with self.subTest(output=output), patch.object(service, "_run_detection", return_value=output):
                self.assertEqual(service.analyze_asset(1).offset_ms, expected)

    def test_no_fallback_policy_excludes_quiet_pause(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        output = "silence_start:239.6\nsilence_end:240.4"
        with patch.object(service, "_run_detection", return_value=output):
            service.analyze_asset(1)
            self.config.data["scheduling"]["midroll_fallback"] = "none"
            self.assertEqual(service.selected_offsets(1), [])
            self.assertEqual(service.analyze_asset(1).method, "none")

    def test_later_targets_get_quiet_pauses_and_preserve_manual_choices(self):
        self.connection.execute("UPDATE asset_media_info SET duration_seconds=2640 WHERE asset_id=1")
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value="silence_start:239.6\nsilence_end:240.4"):
            service.analyze_asset(1)
            self.assertEqual(service.selected_offsets(1), [660000, 1500000, 2070000])
            service.set_manual(1, 1350000, target_index=2)
            self.connection.execute("UPDATE asset_break_analyses SET source_signature='5:10:breaks-v5'")
            self.connection.commit()
            service.analyze_asset(1)
            self.assertEqual(service.selected_offsets(1), [660000, 1350000, 2070000])
            service.reset_manual(1, 2)
            self.assertEqual(service.selected_offsets(1), [660000, 1500000, 2070000])

    def test_spacing_is_checked_before_choosing_a_quiet_pause(self):
        self.connection.execute("UPDATE asset_media_info SET duration_seconds=2640 WHERE asset_id=1")
        self.connection.commit()
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value=""):
            service.set_manual(1, 1100000, target_index=1)
        # The first pause in target 2 is less than five minutes after the manual
        # break; select the later valid pause rather than dropping the break.
        output = "silence_start:80\nsilence_end:81\nsilence_start:239\nsilence_end:241"
        with patch.object(service, "_run_detection", return_value=output):
            service.analyze_asset(1)
        self.assertEqual(service.selected_offsets(1)[1], 1500000)

    def test_version_five_cuts_work_while_waiting_for_reanalysis(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        with patch.object(service, "_run_detection", return_value=""):
            service.analyze_asset(1)
        self.connection.execute("UPDATE asset_break_analyses SET source_signature='5:10:breaks-v5'")
        self.connection.commit()
        self.assertEqual(service.selected_offsets(1), [690000])
        self.assertEqual([row['id'] for row in service.pending_assets()], [1])
        with patch.object(service, "_run_detection", return_value="silence_start:238\nsilence_end:239"):
            service.analyze_asset(1)
        self.assertEqual(service.selected_offsets(1), [688500])
        self.assertEqual(service.pending_assets(), [])
        self.connection.execute("UPDATE assets SET modified_ns=11")
        self.connection.commit()
        self.assertEqual(service.selected_offsets(1), [])

    def test_breakpoint_api_filters_evidence_and_full_video_range(self):
        service = BreakpointAnalyzer(self.connection, self.config)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            def get(status):
                with urlopen(base + '/api/breakpoints?status=' + status) as response:
                    return json.load(response)['items']
            with patch.object(service, '_run_detection', return_value='silence_start:239.6\nsilence_end:240.4'):
                service.analyze_asset(1)
            self.assertEqual(get('fallback')[0]['series_title'], 'Show')
            self.assertEqual(get('quiet_pause')[0]['targets'][0]['selected']['method'], 'quiet_pause')
            self.assertEqual(get('midpoint'), [])
            self.assertEqual(get('pending'), [])
            self.connection.execute("UPDATE asset_break_analyses SET source_signature='5:10:breaks-v5'")
            self.connection.commit()
            self.assertTrue(get('pending')[0]['needs_reanalysis'])
            with patch.object(service, '_run_detection', return_value=''):
                service.analyze_asset(1)
            self.assertEqual(len(get('midpoint')), 1)
            self.assertEqual(len(get('fallback')), 1)
            self.assertEqual(get('quiet_pause'), [])
            with urlopen(Request(base + '/api/assets/1/play', headers={'Range':'bytes=1-3'})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b'ide')
            (self.media / 'fox/all-day/episodes/Show/episode.mp4').unlink()
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/assets/1/play')
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_detector_selects_quiet_pause_without_visual_transition(self):
        source = self.media / 'fox/all-day/episodes/Show/episode.mp4'
        # A tiny, low-frame-rate 23-minute file keeps the actual target and seek
        # timestamps realistic. Modulated tones approximate voiced audio.
        audio = r'aevalsrc=if(between(t\,689.6\,690.4)\,0\,0.2*(sin(2*PI*330*t)+0.2*sin(2*PI*550*t))):s=16000:d=1380'
        subprocess.run([self.config.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'color=c=blue:s=160x120:r=1:d=1380', '-f', 'lavfi', '-i', audio,
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-shortest', str(source)], check=True, timeout=120,
                       capture_output=True)
        result = BreakpointAnalyzer(self.connection, self.config).analyze_asset(1)
        self.assertEqual(result.method, 'quiet_pause')
        self.assertGreaterEqual(result.offset_ms, 689600)
        self.assertLessEqual(result.offset_ms, 690400)


if __name__ == "__main__":
    unittest.main()
