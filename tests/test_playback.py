import shutil
import json
import unittest
import uuid
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.playback import MpvIpcPlayer, PlaybackController, Player, ScheduleResolver
from retrotv.scheduling import ScheduleBuilder


class FakePlayer(Player):
    def __init__(self):
        self.loaded = []
        self.running = False

    def start(self):
        self.running = True

    def load(self, target):
        self.start()
        self.loaded.append(target)

    def is_running(self):
        return self.running

    def stop(self):
        self.running = False


class PlaybackTests(unittest.TestCase):
    def test_missing_scheduled_media_fills_only_remaining_slot_and_recovers_moved_path(self):
        from datetime import timedelta
        builder = ScheduleBuilder(self.connection, self.config)
        entry = next(row for row in builder.entries(self.generation.generation_id) if row["asset_id"])
        source = self.media / entry["source_relative_path"]
        moved = source.parent / "Season 1" / source.name
        moved.parent.mkdir()
        source.rename(moved)
        missing = []
        resolver = ScheduleResolver(self.connection, self.config, missing_media_handler=missing.append)
        end = datetime.fromisoformat(entry["ends_at"])
        clock = [end - timedelta(seconds=10)]
        continuity = self.temporary / "continuity.mp4"
        continuity.write_bytes(b"continuity")
        with patch("retrotv.playback.ensure_continuity_slate", return_value=continuity), patch(
                "retrotv.graphics.GraphicsService.select_asset", return_value=None):
            player = FakePlayer()
            controller = PlaybackController(resolver, player, clock=lambda:clock[0])
            target = controller.tune_once("fox")
            self.assertEqual(target.entry_type, "channel_fallback")
            self.assertEqual(target.ends_at, end)
            self.assertEqual(target.remaining_seconds, 10)
            self.assertEqual(target.channel_slug, "fox")
            self.assertEqual(missing[0]["asset_id"], self.asset_id)
            self.connection.execute("UPDATE assets SET source_relative_path=? WHERE id=?",
                                    (moved.relative_to(self.media).as_posix(), self.asset_id))
            self.connection.commit()
            recovered = controller.tune_once("fox", continue_synthetic=True)
            self.assertEqual(recovered.path, moved)
            self.assertEqual(recovered.entry_type, "episode")
            clock[0] = end
            following = controller.tune_once("fox")
            self.assertNotEqual(following.entry_id, target.entry_id)
            self.assertEqual(following.starts_at, end)

    def test_missing_scheduled_media_uses_station_interstitial(self):
        entry = next(row for row in ScheduleBuilder(self.connection, self.config).entries(
                     self.generation.generation_id) if row["asset_id"])
        (self.media / entry["source_relative_path"]).unlink()
        rendered = self.temporary / "station.mp4"
        rendered.write_bytes(b"station")
        with patch("retrotv.graphics.GraphicsService.select_asset", return_value={"id":1}), patch(
                "retrotv.graphics.GraphicsService.render_interstitial", return_value=rendered):
            target = ScheduleResolver(self.connection, self.config).resolve("fox", datetime.fromisoformat(entry["starts_at"]))
        self.assertEqual(target.path, rendered)
        self.assertEqual(target.representation, "generated_channel_fallback")
        self.assertIsNotNone(target.missing_media)

    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        base = project / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temporary = base / f"playback-{uuid.uuid4().hex}"
        self.media = self.temporary / "media"
        self.normalized = self.temporary / "normalized"
        self.media.mkdir(parents=True)
        self.normalized.mkdir()
        self.config = AppConfig(
            profile_path=self.temporary / "test.toml",
            project_root=project,
            data={
                "application": {
                    "database_path": str(self.temporary / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {
                    "media": str(self.media),
                    "normalized": str(self.normalized),
                    "previews": str(self.temporary / "previews"),
                    "state": str(self.temporary / "state"),
                    "originals_backup": str(self.temporary / "backup"),
                },
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                "scheduling": {"timezone": "America/Los_Angeles"},
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        root_id = ensure_storage_root(self.connection, "media")
        relative = "fox/all-day/episodes/Test/episode.mp4"
        source = self.media / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"test")
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO assets(
                source_root_id, source_relative_path, original_filename,
                file_size, modified_ns, media_type, display_title, series_title,
                channel_slug, daypart_slug, metadata_status, integrity_status,
                review_status, enabled_for_scheduling, first_seen_at, last_seen_at
            ) VALUES (?, ?, 'episode.mp4', 4, 1, 'episode', 'Test Episode',
                      'Test', 'fox', 'all-day', 'ready', 'ready', 'ready', 1,
                      ?, ?)
            """,
            (root_id, relative, now, now),
        )
        self.asset_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO asset_media_info(asset_id, duration_seconds) VALUES (?, 3600)",
            (self.asset_id,),
        )
        self.connection.execute(
            """
            INSERT INTO asset_eligibility(
                asset_id, channel_slug, daypart_slug, eligibility_source
            ) VALUES (?, 'fox', 'all-day', 'test')
            """,
            (self.asset_id,),
        )
        self.connection.commit()
        self.generation = ScheduleBuilder(
            self.connection, self.config
        ).generate_day("fox", date(2026, 7, 27))

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.temporary, ignore_errors=True)

    def publish_weather(self, generation_id: str) -> Path:
        root = self.temporary / "synthetic-weather"
        version = root / "versions" / generation_id
        version.mkdir(parents=True)
        video = version / "weather-channel.mp4"
        video.write_bytes(generation_id.encode("utf-8"))
        manifest = {
            "schema_version": 1,
            "channel": {"slug": "weather", "display_name": "RetroWeather"},
            "generation_id": generation_id,
            "generated_at": "2026-07-27T00:00:00-07:00",
            "valid_until": "2026-07-28T00:00:00-07:00",
            "timezone": "America/Los_Angeles",
            "video": "weather-channel.mp4",
            "duration_seconds": 120.0,
            "attribution": ["Weather data by Open-Meteo"],
            "segments": [
                {
                    "id": "conditions", "start_seconds": 0,
                    "end_seconds": 60, "title": "Current Conditions",
                    "condition_theme": "sunny/day", "background": None,
                },
                {
                    "id": "forecast", "start_seconds": 60,
                    "end_seconds": 120, "title": "Local Forecast",
                    "condition_theme": "sunny/day", "background": None,
                },
            ],
        }
        (version / "channel.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (root / "current.json").write_text(
            json.dumps({
                "schema_version": 1,
                "generation_id": generation_id,
                "manifest": f"versions/{generation_id}/channel.json",
            }),
            encoding="utf-8",
        )
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True, "root": str(root)}
        }
        return video

    def test_unconfigured_weather_plays_continuity_then_recovers_after_setup(self):
        from datetime import timedelta
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True, "root": str(self.temporary / "synthetic-weather")}
        }
        resolver = ScheduleResolver(self.connection, self.config)
        player = FakePlayer()
        now = datetime.fromisoformat("2026-08-09T10:10:00-07:00")
        controller = PlaybackController(resolver, player, clock=lambda: now)
        slate = self.temporary / "continuity.mp4"
        slate.write_bytes(b"continuity")
        with patch("retrotv.playback.ensure_continuity_slate", return_value=slate):
            fallback = controller.tune_once("weather")
        self.assertEqual(fallback.entry_type, "channel_fallback")
        self.assertEqual(fallback.ends_at, now + timedelta(seconds=30))
        self.assertTrue(player.is_running())
        video = self.publish_weather("ready-after-setup")
        recovered = controller.tune_once("weather", continue_synthetic=True)
        self.assertEqual(recovered.entry_type, "synthetic_channel")
        self.assertEqual(recovered.path, video)
        self.assertTrue(player.is_running())

    def test_page_and_arrow_keys_share_channel_actions(self):
        player = MpvIpcPlayer(self.config)
        bindings = Path(player._input_config()).read_text(encoding="utf-8")
        for key, action in (
            ("UP", "channel-up"), ("PGUP", "channel-up"),
            ("DOWN", "channel-down"), ("PGDWN", "channel-down"),
        ):
            self.assertIn(
                f"{key} script-message retrotv-action {action}\n", bindings
            )

    def test_channel_label_size_updates_a_running_mpv_player(self):
        player = MpvIpcPlayer(self.config)
        with patch.object(player, "_command") as command:
            player.set_osd_font_size(72)
        command.assert_called_once_with(["set_property", "osd-font-size", 72])

    def test_resolver_joins_entry_at_wall_clock_offset(self):
        resolver = ScheduleResolver(self.connection, self.config)
        target = resolver.resolve(
            "fox",
            datetime(2026, 7, 27, 0, 30, tzinfo=resolver.scheduler.timezone),
        )
        self.assertEqual(target.title, "Test Episode")
        self.assertAlmostEqual(target.media_offset_seconds, 1800, delta=0.01)
        self.assertAlmostEqual(target.remaining_seconds, 1800, delta=0.01)
        self.assertEqual(target.representation, "original")

    def test_synthetic_tuning_uses_epoch_offset_without_database_media(self):
        video = self.publish_weather("weather-1")
        resolver = ScheduleResolver(self.connection, self.config)
        when = datetime(
            2026, 7, 27, 12, 34, 45,
            tzinfo=resolver.scheduler.timezone,
        )
        before = self.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        target = resolver.resolve("weather", when)
        after = self.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(target.path, video.resolve())
        self.assertEqual(target.entry_type, "synthetic_channel")
        self.assertEqual(target.asset_id, None)
        self.assertEqual(target.generation_id, "weather-1")
        self.assertAlmostEqual(
            target.media_offset_seconds, when.timestamp() % 120.0, delta=0.001
        )

    def test_synthetic_loop_continues_until_generation_changes(self):
        first_video = self.publish_weather("weather-1")
        resolver = ScheduleResolver(self.connection, self.config)
        clock_value = [datetime(
            2026, 7, 27, 12, 34, 45,
            tzinfo=resolver.scheduler.timezone,
        )]
        player = FakePlayer()
        controller = PlaybackController(
            resolver, player, clock=lambda: clock_value[0]
        )

        first = controller.tune_once("weather")
        self.assertEqual(player.loaded[0].path, first_video.resolve())
        clock_value[0] = first.ends_at
        second = controller.tune_once("weather", continue_synthetic=True)
        self.assertEqual(len(player.loaded), 1)

        second_video = self.publish_weather("weather-2")
        clock_value[0] = second.ends_at
        third = controller.tune_once("weather", continue_synthetic=True)
        self.assertEqual(len(player.loaded), 2)
        self.assertEqual(third.generation_id, "weather-2")
        self.assertEqual(player.loaded[-1].path, second_video.resolve())

    def test_resolver_ignores_ready_staging_during_replace_policy(self):
        proxy = self.normalized / "fox/1.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"proxy")
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(
                asset_id, analysis_status, detected_mode, proposed_mode,
                proxy_status, proxy_relative_path
            ) VALUES (?, 'analyzed', 'stretched_4_3', 'stretched_4_3',
                      'ready', 'fox/1.mp4')
            """,
            (self.asset_id,),
        )
        self.connection.commit()
        resolver = ScheduleResolver(self.connection, self.config)
        target = resolver.resolve(
            "fox",
            datetime(2026, 7, 27, 0, 10, tzinfo=resolver.scheduler.timezone),
        )
        self.assertNotEqual(target.path, proxy)
        self.assertEqual(target.path, self.media / "fox/all-day/episodes/Test/episode.mp4")
        self.assertEqual(target.representation, "original")

    def test_resolver_supports_explicit_legacy_proxy_policy(self):
        self.config.data["normalization"] = {"output_policy": "proxy"}
        proxy = self.normalized / "fox/1.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"proxy")
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(
                asset_id, analysis_status, detected_mode, proposed_mode,
                proxy_status, proxy_relative_path
            ) VALUES (?, 'analyzed', 'stretched_4_3', 'stretched_4_3',
                      'ready', 'fox/1.mp4')
            """,
            (self.asset_id,),
        )
        self.connection.commit()
        resolver = ScheduleResolver(self.connection, self.config)
        target = resolver.resolve(
            "fox",
            datetime(2026, 7, 27, 0, 10, tzinfo=resolver.scheduler.timezone),
        )
        self.assertEqual(target.path, proxy)
        self.assertEqual(target.representation, "corrected_staging")

    def test_controller_passes_resolved_offset_to_player(self):
        resolver = ScheduleResolver(self.connection, self.config)
        when = datetime(
            2026, 7, 27, 0, 15, tzinfo=resolver.scheduler.timezone
        )
        player = FakePlayer()
        controller = PlaybackController(
            resolver,
            player,
            clock=lambda: when,
        )
        target = controller.tune_once("fox")
        self.assertEqual(player.loaded, [target])
        self.assertAlmostEqual(target.media_offset_seconds, 900, delta=0.01)

    def test_controller_accepts_channel_change_without_restart(self):
        resolver = ScheduleResolver(self.connection, self.config)
        player = FakePlayer()
        controller = PlaybackController(resolver, player)
        controller.request_tune("fox")
        self.assertEqual(controller.channel_slug, "fox")
        self.assertTrue(controller.retune_event.is_set())

    def test_long_graphic_gap_renders_only_short_loop(self):
        channel_id = int(
            self.connection.execute(
                "SELECT channel_id FROM schedule_generations WHERE id=?",
                (self.generation.generation_id,),
            ).fetchone()[0]
        )
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO graphic_assets(
                id,channel_id,relative_path,purpose,source_kind,file_size,
                modified_ns,validation_status,first_seen_at,last_seen_at
            ) VALUES (10,?,'fox/graphics/interstitials/test.png',
                      'interstitials','image',1,1,'valid',?,?)
            """,
            (channel_id, now, now),
        )
        self.connection.execute(
            "DELETE FROM schedule_entries WHERE generation_id=?",
            (self.generation.generation_id,),
        )
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,
                daypart_slug,sequence_position,is_program,graphic_asset_id
            ) VALUES (?,?,'2026-07-27T00:00:00-07:00',
                      '2026-07-27T01:00:00-07:00','graphic_interstitial',
                      'Station Interstitial','night',0,0,10)
            """,
            (self.generation.generation_id, channel_id),
        )
        self.connection.commit()
        self.config.data["graphics"] = {"interstitial_duration_seconds": 10}
        rendered = self.temporary / "graphic-loop.mp4"
        rendered.write_bytes(b"loop")

        resolver = ScheduleResolver(self.connection, self.config)
        with patch(
            "retrotv.graphics.GraphicsService.render_interstitial",
            return_value=rendered,
        ) as render:
            target = resolver.resolve(
                "fox",
                datetime(
                    2026, 7, 27, 0, 42, 35,
                    tzinfo=resolver.scheduler.timezone,
                ),
            )

        self.assertEqual(target.path, rendered)
        self.assertEqual(target.representation, "generated_graphic_interstitial")
        self.assertAlmostEqual(target.media_offset_seconds, 5.0, delta=0.01)
        self.assertEqual(render.call_args.kwargs["duration_seconds"], 10.0)

    def test_missing_graphic_falls_back_to_continuity(self):
        channel_id = int(
            self.connection.execute(
                "SELECT channel_id FROM schedule_generations WHERE id=?",
                (self.generation.generation_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            "DELETE FROM schedule_entries WHERE generation_id=?",
            (self.generation.generation_id,),
        )
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,
                daypart_slug,sequence_position,is_program
            ) VALUES (?,?,'2026-07-27T00:00:00-07:00',
                      '2026-07-27T01:00:00-07:00','graphic_interstitial',
                      'Station Interstitial','night',0,0)
            """,
            (self.generation.generation_id, channel_id),
        )
        self.connection.commit()
        fallback = self.temporary / "continuity.mp4"
        fallback.write_bytes(b"continuity")

        resolver = ScheduleResolver(self.connection, self.config)
        with patch(
            "retrotv.playback.ensure_continuity_slate",
            return_value=fallback,
        ):
            target = resolver.resolve(
                "fox",
                datetime(
                    2026, 7, 27, 0, 30, 7,
                    tzinfo=resolver.scheduler.timezone,
                ),
            )

        self.assertEqual(target.path, fallback)
        self.assertEqual(target.representation, "generated_continuity_fallback")
        self.assertAlmostEqual(target.media_offset_seconds, 7.0, delta=0.01)

    def test_channel_without_a_schedule_remains_tunable_on_continuity(self):
        fallback = self.temporary / "continuity.mp4"
        fallback.write_bytes(b"continuity")
        resolver = ScheduleResolver(self.connection, self.config)
        with patch(
            "retrotv.playback.ensure_continuity_slate", return_value=fallback
        ):
            target = resolver.resolve(
                "fox",
                datetime(2026, 7, 28, 12, 0, tzinfo=resolver.scheduler.timezone),
            )
        self.assertEqual(target.entry_type, "channel_fallback")
        self.assertEqual(target.path, fallback)
        self.assertEqual(target.representation, "generated_continuity_fallback")

    def test_channel_without_a_schedule_prefers_its_interstitial_graphic(self):
        channel_id = int(
            self.connection.execute(
                "SELECT id FROM channels WHERE slug='fox'"
            ).fetchone()[0]
        )
        graphic_path = self.media / "fox/graphics/interstitials/placeholder.png"
        graphic_path.parent.mkdir(parents=True, exist_ok=True)
        graphic_path.write_bytes(b"graphic")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO graphic_assets(
                channel_id,relative_path,purpose,source_kind,file_size,modified_ns,
                validation_status,provenance,first_seen_at,last_seen_at
            ) VALUES (?,'fox/graphics/interstitials/placeholder.png','interstitials',
                      'image',7,1,'valid','placeholder',?,?)
            """,
            (channel_id, now, now),
        )
        self.connection.commit()
        rendered = self.temporary / "channel-fallback.mp4"
        rendered.write_bytes(b"rendered")
        resolver = ScheduleResolver(self.connection, self.config)
        with patch(
            "retrotv.graphics.GraphicsService.render_interstitial",
            return_value=rendered,
        ):
            target = resolver.resolve(
                "fox",
                datetime(2026, 7, 28, 12, 0, tzinfo=resolver.scheduler.timezone),
            )
        self.assertEqual(target.entry_type, "channel_fallback")
        self.assertEqual(target.path, rendered)
        self.assertEqual(target.representation, "generated_channel_fallback")

    def test_weekly_promo_opportunity_plays_its_silent_visual_fallback(self):
        entry = self.connection.execute(
            """SELECT id,generation_id,channel_id,programming_block_id,starts_at,ends_at
               FROM schedule_entries WHERE generation_id=?
                 AND entry_type='announcement_opportunity' ORDER BY starts_at LIMIT 1""",
            (self.generation.generation_id,),
        ).fetchone()
        self.assertIsNotNone(entry)
        self.connection.execute(
            "UPDATE schedule_entries SET entry_type='weekly_special_promo_opportunity',"
            "title='Weekly Special Promo: SNICK' WHERE id=?", (entry["id"],)
        )
        fallback = self.temporary / "weekly-promo-silent.mp4"
        fallback.write_bytes(b"promo")
        now = utc_now()
        facts = {"channel": "fox", "weekly_special_promo": {
            "window_slug": "snick", "collection_slug": "snick",
            "special_name": "SNICK", "airing_at": "2026-08-03T20:00:00-07:00",
            "airing_ends_at": "2026-08-03T22:00:00-07:00",
        }, "lineup": [], "selected_lineup": [], "lineup_more": False}
        self.connection.execute(
            """UPDATE announcement_opportunities SET facts_json=?,cache_key=?,
               weekly_collection_slug='snick',opportunity_type='weekly_special_promo',
               fallback_path=?,render_status='ready',updated_at=?
               WHERE schedule_entry_id=?""",
            (json.dumps(facts), "promo-cache", str(fallback), now, entry["id"]),
        )
        self.connection.commit()
        resolver = ScheduleResolver(self.connection, self.config)
        target = resolver.resolve("fox", datetime.fromisoformat(entry["starts_at"]))
        self.assertEqual(target.entry_type, "weekly_special_promo_opportunity")
        self.assertEqual(target.path, fallback.resolve())
        self.assertEqual(target.representation, "generated_announcement_silent")

    def test_guide_is_a_playable_generated_channel(self):
        guide_path = self.temporary / "guide.mp4"
        guide_path.write_bytes(b"guide")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO guide_renders(
                window_starts_at, window_ends_at, output_relative_path,
                source_fingerprint, page_count, duration_seconds, status,
                generated_at, activated_at
            ) VALUES (?, ?, ?, 'test', 1, 20, 'valid', ?, ?)
            """,
            (
                "2026-07-27T00:00:00-07:00",
                "2026-07-27T01:30:00-07:00",
                str(guide_path),
                now,
                now,
            ),
        )
        self.connection.commit()
        resolver = ScheduleResolver(self.connection, self.config)
        target = resolver.resolve(
            "__guide__",
            datetime(2026, 7, 27, 0, 30, tzinfo=resolver.scheduler.timezone),
        )
        self.assertEqual(target.path, guide_path)
        self.assertEqual(target.entry_type, "guide")
        self.assertEqual(target.representation, "generated_guide")


if __name__ == "__main__":
    unittest.main()
