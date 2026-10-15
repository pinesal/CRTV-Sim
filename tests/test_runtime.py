import shutil
import threading
import unittest
import uuid
import hashlib
import json
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.database_backup import DatabaseBackupService
from retrotv.db import (
    apply_migrations,
    connect_database,
    ensure_storage_root,
    utc_now,
)
from retrotv.runtime import ApplicationRuntime
from retrotv.web import make_handler


class ApplicationRuntimeTests(unittest.TestCase):
    def test_dashboard_re_review_api_forwards_selected_asset(self):
        runtime = ApplicationRuntime(self.config)
        server = ThreadingHTTPServer(("127.0.0.1",0), make_handler(self.config, runtime))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(runtime, "request_normalization_review", return_value=True) as request:
                response = urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/runtime/normalization-review",
                    data=json.dumps({"asset_id":12}).encode(), headers={"Content-Type":"application/json"}))
                with response:
                    self.assertTrue(json.load(response)["ok"])
                request.assert_called_once_with(12)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_re_review_waits_for_player_release_and_resumes_same_channel(self):
        runtime = ApplicationRuntime(self.config)
        stopped = []
        runtime.controller = SimpleNamespace(current=SimpleNamespace(asset_id=12),
                                             stop=lambda:stopped.append(True))
        runtime.requested_channel = "nickelodeon"
        resumed = []
        runtime.start_tv = lambda channel, **kwargs:resumed.append(channel)
        with patch("retrotv.normalization_review.NormalizationReviewService.reopen") as reopen:
            def check(asset_id):
                self.assertEqual(stopped, [True])
                return {"backup_restored":False, "review_status":"review"}
            reopen.side_effect = check
            self.assertTrue(runtime.request_normalization_review(12))
            runtime.normalization_review_thread.join(5)
        self.assertFalse(runtime.normalization_review_thread.is_alive())
        self.assertEqual(runtime.normalization_review["state"], "completed")
        self.assertEqual(resumed, ["nickelodeon"])
        self.assertIsNone(runtime.readiness_thread)

    def test_re_review_rejects_stale_asset_and_does_not_restore_while_player_alive(self):
        runtime = ApplicationRuntime(self.config)
        runtime.controller = SimpleNamespace(current=SimpleNamespace(asset_id=12))
        with self.assertRaisesRegex(ValueError, "playing media changed"):
            runtime.request_normalization_review(13)
        runtime.stop_tv = lambda **kwargs:None
        runtime.playback_thread = SimpleNamespace(is_alive=lambda:True)
        runtime._review_resume = False
        with patch("retrotv.normalization_review.NormalizationReviewService.reopen") as reopen:
            runtime._normalization_review_worker(12, "fox")
        reopen.assert_not_called()
        self.assertEqual(runtime.normalization_review["state"], "failed")

    def test_missing_media_reporting_queues_once_and_does_not_stop_tv(self):
        runtime = ApplicationRuntime(self.config)
        runtime.tv_state = "playing"
        missing = {"asset_id":12,"path":"R:/moved.mp4","channel":"fox","entry_id":7}
        runtime._report_missing_media(missing)
        self.assertEqual(runtime.playback_recovery["state"], "queued")
        runtime.playback_recovery["state"] = "missing"
        runtime._report_missing_media(missing)
        self.assertEqual(runtime.playback_recovery["state"], "missing")
        self.assertEqual(runtime.tv_state, "playing")

    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        base = project / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temporary = base / f"runtime-{uuid.uuid4().hex}"
        self.temporary.mkdir()
        self.config = AppConfig(
            self.temporary / "test.toml",
            project,
            {
                "application": {
                    "database_path": str(self.temporary / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {
                    "media": str(self.temporary / "media"),
                    "state": str(self.temporary / "state"),
                    "normalized": str(self.temporary / "normalized"),
                    "previews": str(self.temporary / "previews"),
                    "originals_backup": str(self.temporary / "backup"),
                },
                "scheduling": {"timezone": "America/Los_Angeles"},
                "guide": {
                    "channel_number": 2,
                    "channel_numbers": {"fox": 5, "nickelodeon": 33},
                },
            },
        )
        with connect_database(self.config.database_path) as connection:
            apply_migrations(connection, project / "migrations")
            connection.executemany(
                """
                INSERT INTO channels(slug, display_name, enabled)
                VALUES (?, ?, 1)
                """,
                (("fox", "FOX"), ("nickelodeon", "Nickelodeon")),
            )
            connection.commit()

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_channels_include_numeric_guide_and_configured_order(self):
        channels = ApplicationRuntime(self.config).channels()
        self.assertEqual(
            [(item["channel_number"], item["slug"]) for item in channels],
            [(2, "__guide__"), (5, "fox"), (33, "nickelodeon")],
        )

    def test_numeric_entry_resolves_to_channel_slug(self):
        runtime = ApplicationRuntime(self.config)
        tuned = []
        runtime.tune = tuned.append
        runtime.tune_number(33)
        self.assertEqual(tuned, ["nickelodeon"])
        with self.assertRaisesRegex(ValueError, "not configured"):
            runtime.tune_number(99)

    def test_channel_change_osd_uses_uppercase_station_label(self):
        runtime = ApplicationRuntime(self.config)
        messages = []
        runtime.controller = SimpleNamespace(
            player=SimpleNamespace(show_text=messages.append)
        )
        runtime.channels = lambda: [
            {
                "slug": "weather",
                "display_name": "RetroWeather",
                "channel_number": 42,
                "is_guide": False,
                "is_synthetic": True,
            }
        ]
        runtime._show_channel_osd("weather")
        self.assertEqual(messages, ["CH 42  RETROWEATHER"])

    def test_weather_tuning_requires_setup_and_publication_before_slug_or_number(self):
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True, "root": str(self.temporary / "weather")}
        }
        self.config.data["guide"]["channel_numbers"]["weather"] = 42
        runtime = ApplicationRuntime(self.config)
        self.assertNotIn("weather", [item["slug"] for item in runtime.channels()])
        for channel in ("weather", "42"):
            with self.assertRaisesRegex(ValueError, "choose your location"):
                runtime._resolve_channel(channel)
        self.assertTrue(runtime._synthetic_status()["weather"]["setup_required"])
        with patch.object(runtime.synthetic_readers["weather"], "refresh", return_value=SimpleNamespace(ready=True)):
            self.assertEqual(runtime._resolve_channel("weather"), "weather")
            self.assertIn("weather", [item["slug"] for item in runtime.channels()])
            tuned = []
            runtime.tune = tuned.append
            runtime.tune_number(42)
            self.assertEqual(tuned, ["weather"])

    def test_unavailable_previous_weather_channel_does_not_kill_remote_playback(self):
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True, "root": str(self.temporary / "weather")}
        }
        runtime = ApplicationRuntime(self.config)
        runtime.requested_channel = "__guide__"
        runtime.previous_channel = "weather"
        runtime.tv_state = "playing"
        with patch.object(runtime, "_show_text") as message:
            runtime._handle_player_action("last-channel")
        self.assertEqual(runtime.requested_channel, "__guide__")
        self.assertEqual(runtime.tv_state, "playing")
        self.assertIn("RetroWeather is not ready", message.call_args.args[0])

    def test_empty_new_install_channel_step_stays_on_guide_until_weather_is_ready(self):
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True, "root": str(self.temporary / "weather")}
        }
        with connect_database(self.config.database_path) as connection:
            connection.execute("UPDATE channels SET enabled=0")
            connection.commit()
        runtime = ApplicationRuntime(self.config)
        runtime.requested_channel = "__guide__"
        self.assertEqual([item["slug"] for item in runtime.channels()], ["__guide__"])
        tuned = []
        runtime.tune = tuned.append
        runtime._channel_step(1)
        runtime._channel_step(-1)
        self.assertEqual(tuned, ["__guide__", "__guide__"])

    def test_synthetic_weather_replaces_stale_database_channel_and_wraps_up(self):
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True}
        }
        self.config.data["guide"]["channel_numbers"]["weather"] = 42
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                """
                INSERT INTO channels(
                    slug, display_name, channel_number, enabled
                ) VALUES ('weather', 'Weather', 42, 1)
                """
            )
            connection.commit()

        runtime = ApplicationRuntime(self.config)
        with patch.object(runtime.synthetic_readers["weather"], "refresh", return_value=SimpleNamespace(ready=True)):
            channels = runtime.channels()
        self.assertEqual(
            [item["slug"] for item in channels].count("weather"),
            1,
        )
        tuned = []
        runtime.requested_channel = "weather"
        runtime.tune = tuned.append
        with patch.object(runtime.synthetic_readers["weather"], "refresh", return_value=SimpleNamespace(ready=True)):
            runtime._channel_step(1)
        self.assertEqual(tuned, ["__guide__"])

    def test_announcement_eligibility_thresholds_are_exact(self):
        now = datetime.fromisoformat("2026-08-03T12:00:00-07:00")
        reason = ApplicationRuntime.announcement_eligibility_reason
        self.assertEqual(
            reason(now, now - timedelta(seconds=60), now, 10 * 60),
            "eligible",
        )
        self.assertEqual(
            reason(now, now - timedelta(seconds=59.999), now, 10 * 60),
            "Channel dwell is under 60 seconds",
        )
        self.assertEqual(
            reason(now, now - timedelta(seconds=60), now - timedelta(minutes=120), 10 * 60),
            "eligible",
        )
        self.assertEqual(
            reason(now, now - timedelta(seconds=60), now, 4 * 60),
            "Opportunity is four minutes away or less",
        )
        self.assertEqual(
            reason(now, now - timedelta(seconds=60), now, 10 * 60 + 0.001),
            "Opportunity is more than ten minutes away",
        )
        self.assertEqual(
            reason(
                now, now, now, 16,
                local_tts=True,
            ),
            "eligible",
        )
        self.assertEqual(
            reason(
                now, now, now, 15,
                local_tts=True,
            ),
            "Opportunity is too close for safe local rendering",
        )
        self.assertEqual(
            reason(now, now, now, 120, local_tts=True),
            "eligible",
        )
        self.assertEqual(
            reason(now, now, now, 120.001, local_tts=True),
            "Local opportunity is more than two minutes away",
        )

    def test_completed_announcement_worker_releases_generation_slot(self):
        runtime = ApplicationRuntime(self.config)
        runtime.announcement_thread = threading.current_thread()
        runtime._release_announcement_worker_slot()
        self.assertIsNone(runtime.announcement_thread)

    def test_automation_polling_survives_one_announcement_check_failure(self):
        runtime = ApplicationRuntime(self.config)
        runtime.stop_event = SimpleNamespace(
            wait=unittest.mock.Mock(side_effect=[False, False, True])
        )
        checks = unittest.mock.Mock(side_effect=[RuntimeError("temporary failure"), None])
        runtime._check_announcement_demand = checks
        runtime.request_guide_refresh = unittest.mock.Mock()
        runtime.request_maintenance = unittest.mock.Mock()
        runtime._automation_worker()
        self.assertEqual(checks.call_count, 2)
        self.assertIn("retrying", runtime.announcement_state["eligibility_reason"])

    def test_readiness_pipeline_runs_media_work_in_safe_order(self):
        runtime = ApplicationRuntime(self.config)
        runtime.settings.update(
            {
                "auto_readiness_when_tv_idle": True,
                "auto_convert_approved_media": True,
                "auto_analyze_pending_audio": True,
                "auto_normalize_approved_audio": True,
            }
        )
        calls = []
        runtime._maintenance_worker = lambda: calls.append("scan")
        runtime._normalization_batch_worker = (
            lambda *, idle_only=False: calls.append(("video", idle_only))
        )
        runtime._audio_analysis_worker = (
            lambda *, idle_only=False: calls.append(("audio-analysis", idle_only))
        )
        runtime._audio_normalization_worker = (
            lambda *, idle_only=False: calls.append(("audio-normalization", idle_only))
        )

        runtime._readiness_worker()

        self.assertEqual(
            calls,
            [
                "scan",
                ("video", True),
                ("audio-analysis", True),
                ("audio-normalization", True),
            ],
        )
        self.assertEqual(runtime.readiness["state"], "ready")

    def test_readiness_failure_keeps_error_stage_and_logs_it(self):
        runtime = ApplicationRuntime(self.config)
        message = 'Cannot generate schedule'
        def fail_scan():
            runtime.maintenance.update(state='error', stage=None,
                                       failed_stage='schedules', error=message)
        runtime._maintenance_worker = fail_scan
        with patch('builtins.print') as log:
            runtime._readiness_worker()
        status = runtime.status()
        self.assertEqual(status['readiness']['state'], 'error')
        self.assertEqual(status['readiness']['error'], message)
        self.assertEqual(status['readiness']['failed_stage'], 'schedules')
        self.assertIn(message, log.call_args.args[0])

    def test_readiness_failure_after_scan_is_reported_in_status(self):
        runtime = ApplicationRuntime(self.config)
        runtime._maintenance_worker = unittest.mock.Mock()
        runtime.settings['auto_convert_approved_media'] = True
        runtime._normalization_batch_worker = unittest.mock.Mock(
            side_effect=RuntimeError('Conversion could not start'))
        with patch('builtins.print'):
            runtime._readiness_worker()
        self.assertEqual(runtime.readiness['failed_stage'], 'convert approved media')
        self.assertEqual(runtime.readiness['error'], 'Conversion could not start')
        self.assertIsNone(runtime.maintenance['error'])

    def test_successful_guide_refresh_preserves_maintenance_failure(self):
        runtime = ApplicationRuntime(self.config)
        runtime.maintenance.update(state='error', error='Schedule generation failed')
        with patch('retrotv.runtime.GuideRenderer') as renderer:
            renderer.return_value.render.return_value = {'duration_seconds': 30}
            self.assertTrue(runtime._refresh_guide_if_needed(force=True))
        self.assertTrue(runtime.maintenance['guide_rendered'])
        self.assertEqual(runtime.maintenance['error'], 'Schedule generation failed')

    def test_automatic_readiness_waits_until_tv_is_stopped(self):
        runtime = ApplicationRuntime(self.config)
        runtime.settings["auto_readiness_when_tv_idle"] = True
        runtime.controller = SimpleNamespace()
        runtime.tv_state = "playing"

        self.assertFalse(runtime.request_readiness_pipeline())
        self.assertIsNone(runtime.readiness_thread)

    def test_manual_media_job_does_not_overlap_readiness_pipeline(self):
        runtime = ApplicationRuntime(self.config)
        runtime.readiness_thread = SimpleNamespace(is_alive=lambda: True)

        self.assertFalse(runtime.request_maintenance())
        self.assertFalse(runtime.request_normalization_batch())
        self.assertFalse(runtime.request_audio_analysis())
        self.assertFalse(runtime.request_audio_normalization())

    def test_demand_check_accepts_existing_fallback_path(self):
        runtime = ApplicationRuntime(self.config)
        now = datetime.now(runtime.timezone)
        starts = now + timedelta(minutes=8)
        ends = starts + timedelta(seconds=30)
        fallback = self.temporary / "fallback.mp4"
        fallback.write_bytes(b"ready")
        with connect_database(self.config.database_path) as connection:
            channel = connection.execute(
                "SELECT id FROM channels WHERE slug='nickelodeon'"
            ).fetchone()
            cursor = connection.execute(
                """
                INSERT INTO schedule_generations(
                    channel_id,schedule_date,seed,configuration_fingerprint,
                    status,validation_status,generated_at,activated_at
                ) VALUES (?,?,?,'test','active','valid',?,?)
                """,
                (int(channel["id"]), starts.date().isoformat(), "seed", utc_now(), utc_now()),
            )
            generation_id = int(cursor.lastrowid)
            cursor = connection.execute(
                """
                INSERT INTO schedule_entries(
                    generation_id,channel_id,starts_at,ends_at,entry_type,title,
                    daypart_slug,sequence_position,is_program
                ) VALUES (?,?,?,?, 'announcement_opportunity','Up Next','evening',1,0)
                """,
                (generation_id, int(channel["id"]), starts.isoformat(), ends.isoformat()),
            )
            entry_id = int(cursor.lastrowid)
            facts = json.dumps({"channel": "nickelodeon", "station_identification": True, "listings": []})
            connection.execute(
                """
                INSERT INTO announcement_opportunities(
                    schedule_entry_id,generation_id,channel_id,starts_at,ends_at,
                    facts_json,cache_key,fallback_path,render_status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?, 'ready',?,?)
                """,
                (
                    entry_id, generation_id, int(channel["id"]), starts.isoformat(),
                    ends.isoformat(), facts, hashlib.sha256(facts.encode()).hexdigest(),
                    str(fallback), utc_now(), utc_now(),
                ),
            )
            connection.commit()
            connection.execute(
                "UPDATE announcement_opportunities SET fallback_mode='silent' WHERE schedule_entry_id=?", (entry_id,)
            )
            connection.commit()
        runtime.requested_channel = "nickelodeon"
        runtime.tuned_since = now - timedelta(minutes=2)
        runtime.last_qualifying_user_activity = now
        runtime.controller = SimpleNamespace()
        runtime.tv_state = "playing"
        with patch("retrotv.announcements.UpNextRenderer._validate", return_value=30):
            runtime._check_announcement_demand()
        self.assertEqual(runtime.announcement_state["next_opportunity"]["channel"], "nickelodeon")
        self.assertNotIn("not defined", runtime.announcement_state.get("error") or "")
        self.assertEqual(runtime.announcement_state["next_opportunity"]["audio_mode"], "silent")
        # A legacy cached fallback has no proven audio identity; prepare it again.
        with connect_database(self.config.database_path) as connection:
            connection.execute("UPDATE announcement_opportunities SET fallback_mode=NULL WHERE schedule_entry_id=?", (entry_id,))
        with patch("retrotv.runtime.threading.Thread") as thread:
            runtime._check_announcement_demand()
            thread.return_value.start.assert_called_once()
            self.assertEqual(runtime.announcement_state["generation_state"], "rendering-fallback")

    def test_instance_lock_rejects_second_application(self):
        first = ApplicationRuntime(self.config)
        second = ApplicationRuntime(self.config)
        first._acquire_instance_lock()
        try:
            with self.assertRaisesRegex(RuntimeError, "Another CRTV Sim"):
                second._acquire_instance_lock()
        finally:
            first._release_instance_lock()

    def test_safe_shutdown_waits_for_writer_checkpoints_and_backs_up_database(self):
        runtime = ApplicationRuntime(self.config)
        writer_finished = threading.Event()

        def active_writer():
            runtime.stop_event.wait()
            with connect_database(self.config.database_path) as connection:
                connection.execute(
                    "INSERT INTO channels(slug, display_name, enabled) VALUES ('late-write', 'Late Write', 1)"
                )
                connection.commit()
            writer_finished.set()

        writer = threading.Thread(target=active_writer)
        runtime.normalization_batch_thread = writer
        writer.start()
        runtime.weather_companion.stop = unittest.mock.Mock()

        result = runtime.shutdown()

        self.assertTrue(writer_finished.is_set())
        self.assertTrue(result["safe_to_remove"])
        self.assertEqual(result["state"], "safe")
        self.assertTrue(runtime.shutdown_complete_event.is_set())
        self.assertTrue(Path(result["database_backup"]["path"]).is_file())
        with connect_database(self.config.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM channels WHERE slug='late-write'"
                ).fetchone()[0],
                1,
            )
        self.assertFalse(runtime.request_maintenance())

    def test_safe_shutdown_reuses_backup_but_flushes_latest_writes(self):
        runtime = ApplicationRuntime(self.config)
        with connect_database(self.config.database_path) as connection:
            backup = DatabaseBackupService(self.config).create(connection)
        before = Path(backup.path).stat().st_mtime_ns
        writer_finished = threading.Event()

        def writer():
            runtime.stop_event.wait()
            with connect_database(self.config.database_path) as connection:
                connection.execute(
                    "INSERT INTO channels(slug, display_name, enabled) VALUES ('late-write', 'Late Write', 1)"
                )
                connection.commit()
            writer_finished.set()

        runtime.normalization_batch_thread = threading.Thread(target=writer)
        runtime.normalization_batch_thread.start()
        runtime.weather_companion.stop = unittest.mock.Mock()
        statements = []

        def traced_connection(path):
            connection = connect_database(path)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("retrotv.runtime.connect_database", side_effect=traced_connection), patch.object(
            DatabaseBackupService, "create", side_effect=AssertionError("Must reuse backup")
        ):
            result = runtime.shutdown()
        self.assertTrue(writer_finished.is_set())
        self.assertTrue(result["safe_to_remove"])
        self.assertTrue(result["database_backup"]["skipped"])
        self.assertEqual(Path(backup.path).stat().st_mtime_ns, before)
        self.assertIn("PRAGMA wal_checkpoint(TRUNCATE)", statements)
        self.assertFalse(any("quick_check" in sql or "integrity_check" in sql for sql in statements))
        with connect_database(self.config.database_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM channels WHERE slug='late-write'"
            ).fetchone()[0], 1)
        self.assertIn("checkpointing database", result["stage_timings"])
        snapshot = runtime.shutdown_status()
        snapshot["stage_timings"].clear()
        self.assertTrue(runtime.shutdown_status()["stage_timings"])

    def test_safe_shutdown_checkpoint_failure_is_not_reported_safe(self):
        runtime = ApplicationRuntime(self.config)
        runtime.weather_companion.stop = unittest.mock.Mock()
        connection = unittest.mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = (1, 10, 0)
        with patch("retrotv.runtime.connect_database", return_value=connection), patch.object(
            DatabaseBackupService, "create_for_shutdown"
        ) as backup:
            result = runtime.shutdown()
        self.assertFalse(result["safe_to_remove"])
        self.assertIn("checkpoint", result["error"])
        backup.assert_not_called()
        connection.close.assert_called_once()

    def test_safe_shutdown_cannot_finish_before_active_file_replacement(self):
        runtime = ApplicationRuntime(self.config)
        runtime.weather_companion.stop = unittest.mock.Mock()
        target = self.temporary / "media.mp4"
        partial = self.temporary / "media.mp4.partial"
        target.write_bytes(b"original")
        allow_finish = threading.Event()

        def replace_file():
            runtime.stop_event.wait()
            partial.write_bytes(b"finished conversion")
            allow_finish.wait()
            partial.replace(target)

        writer = threading.Thread(target=replace_file)
        runtime.normalization_batch_thread = writer
        writer.start()
        shutdown = threading.Thread(target=runtime.shutdown)
        shutdown.start()
        try:
            self.assertTrue(runtime.stop_event.wait(timeout=3))
            self.assertTrue(shutdown.is_alive())
            self.assertFalse(runtime.shutdown_status()["safe_to_remove"])
            self.assertEqual(target.read_bytes(), b"original")
        finally:
            allow_finish.set()
            writer.join(timeout=3)
            shutdown.join(timeout=5)
        self.assertFalse(shutdown.is_alive())
        self.assertTrue(runtime.shutdown_status()["safe_to_remove"])
        self.assertEqual(target.read_bytes(), b"finished conversion")
        self.assertFalse(partial.exists())

    def test_shutdown_progress_api_does_not_read_database(self):
        runtime = ApplicationRuntime(self.config)
        runtime.shutdown_state.update(state="stopping", stage="waiting for active work to finish",
                                      active_jobs=["video conversion"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config, runtime))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(runtime, "status", side_effect=AssertionError("No full status query")), patch(
                "retrotv.runtime.connect_database", side_effect=AssertionError("No database read")
            ):
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/runtime/shutdown-status") as response:
                    status = json.load(response)
            self.assertEqual(status["active_jobs"], ["video conversion"])
            self.assertEqual(status["state"], "stopping")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_guide_reload_is_requested_at_loop_boundary(self):
        runtime = ApplicationRuntime(self.config)
        timezone = datetime.now().astimezone().tzinfo
        current = SimpleNamespace(
            entry_type="guide",
            media_offset_seconds=3.0,
            starts_at=datetime.now(timezone),
        )

        class Controller:
            def __init__(self):
                self.current = current
                self.resolver = SimpleNamespace(
                    scheduler=SimpleNamespace(timezone=timezone)
                )
                self.retuned = False

            def request_retune(self):
                self.retuned = True

        controller = Controller()
        runtime.controller = controller
        runtime.requested_channel = "__guide__"
        captured = {}

        class ImmediateTimer:
            def __init__(self, delay, callback):
                captured["delay"] = delay
                self.callback = callback
                self.daemon = False

            def start(self):
                self.callback()

        with patch("retrotv.runtime.threading.Timer", ImmediateTimer):
            runtime._schedule_guide_reload(16.0)
        self.assertGreater(captured["delay"], 12.0)
        self.assertLessEqual(captured["delay"], 13.0)
        self.assertTrue(controller.retuned)

    def test_batch_conversion_skips_promoted_and_unapproved_media(self):
        self.config.data["normalization"] = {
            "output_policy": "replace_with_backup"
        }
        with connect_database(self.config.database_path) as connection:
            root_id = ensure_storage_root(connection, "media")
            now = utc_now()
            for asset_id, review_status, proxy_status, mode in (
                (1, "ready", "not_created", "pillarboxed_4_3"),
                (2, "ready", "ready", "stretched_4_3"),
                (3, "ready", "promoted", "custom"),
                (4, "open", "not_created", "pillarboxed_4_3"),
                (5, "ready", "not_created", "native_4_3"),
                (6, "ready", "not_created", "preserve_frame"),
                (7, "ready", "not_created", "native_4_3"),
            ):
                connection.execute(
                    """
                    INSERT INTO assets(
                        id, source_root_id, source_relative_path,
                        original_filename, file_size, modified_ns,
                        metadata_status, integrity_status, review_status,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, 1, 1, 'ready', 'ready', ?, ?, ?)
                    """,
                    (
                        asset_id,
                        root_id,
                        f"fox/all-day/episodes/{asset_id}.mp4",
                        f"{asset_id}.mp4",
                        review_status,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO asset_media_info(asset_id, width, height)
                    VALUES (?, ?, ?)
                    """,
                    (
                        asset_id,
                        1920 if asset_id == 6 else 960 if asset_id == 7 else 640,
                        1080 if asset_id == 6 else 720 if asset_id == 7 else 480,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO normalization_profiles(
                        asset_id, analysis_status, detected_mode,
                        proposed_mode, proxy_status
                    ) VALUES (?, 'analyzed', ?, ?, ?)
                    """,
                    (asset_id, mode, mode, proxy_status),
                )
            connection.commit()

        converted = []

        class FakePromotionService:
            def __init__(self, connection, config):
                pass

            def replace_with_backup(self, asset_id):
                converted.append(asset_id)
                return SimpleNamespace(asset_id=asset_id)

        runtime = ApplicationRuntime(self.config)
        with patch("retrotv.runtime.PromotionService", FakePromotionService):
            runtime._normalization_batch_worker()

        self.assertEqual(converted, [1, 2, 6, 7])
        self.assertEqual(runtime.normalization_batch["state"], "completed")
        self.assertEqual(runtime.normalization_batch["total"], 4)
        self.assertEqual(runtime.normalization_batch["converted"], 4)
        self.assertEqual(runtime.normalization_batch["skipped"], 1)
        self.assertEqual(runtime.normalization_batch["not_needed"], 1)
        self.assertEqual(runtime.normalization_batch["failed"], 0)


if __name__ == "__main__":
    unittest.main()
