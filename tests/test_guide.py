import shutil
import json
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.guide import GuideRenderer, GuideService


class GuideServiceTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / ".test-tmp" / f"guide-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.config = AppConfig(
            self.root / "test.toml",
            self.project,
            {
                "application": {
                    "database_path": str(self.root / "guide.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {"media": str(self.root / "media")},
                "scheduling": {"timezone": "America/Los_Angeles"},
                "guide": {
                    "channel_number": 2,
                    "columns_minutes": [0, 30, 60],
                    "slot_minutes": 30,
                    "channel_numbers": {"fox": 5},
                    "output": str(self.root / "guide.mp4"),
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        storage = ensure_storage_root(self.connection, "media")
        now = utc_now()
        for asset_id, media_type, title in (
            (1, "commercial", "Ad"),
            (2, "episode", "Current Episode"),
            (3, "episode", "Next Episode"),
            (4, "complete_block", "Recorded Block"),
        ):
            self.connection.execute(
                """
                INSERT INTO assets(
                    id, source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, media_type, display_title,
                    channel_slug, daypart_slug, metadata_status, integrity_status,
                    review_status, enabled_for_scheduling, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, 'fox', 'afternoon',
                          'ready', 'ready', 'ready', 1, ?, ?)
                """,
                (
                    asset_id,
                    storage,
                    f"fox/afternoon/{media_type}s/{asset_id}.mp4",
                    f"{asset_id}.mp4",
                    media_type,
                    title,
                    now,
                    now,
                ),
            )
        self.connection.execute(
            "INSERT INTO channels(id, slug, display_name, enabled) VALUES (1, 'fox', 'FOX', 1)"
        )
        self.connection.execute(
            """
            INSERT INTO schedule_generations(
                id, channel_id, schedule_date, seed, configuration_fingerprint,
                status, validation_status, generated_at
            ) VALUES (1, 1, '2026-07-28', 'seed', 'fingerprint',
                      'active', 'valid', ?)
            """,
            (now,),
        )
        entries = [
            (1, "2026-07-28T12:00:00-07:00", "2026-07-28T12:01:00-07:00", "commercial", "Ad", 1, 0),
            (2, "2026-07-28T12:01:00-07:00", "2026-07-28T12:31:00-07:00", "episode", "Current Episode", 1, 1),
            (3, "2026-07-28T12:31:00-07:00", "2026-07-28T13:01:00-07:00", "episode", "Next Episode", 2, 1),
            (4, "2026-07-28T14:00:00-07:00", "2026-07-28T16:00:00-07:00", "complete_block", "Recorded Block", 3, 1),
        ]
        for position, (asset, starts, ends, kind, title, group, program) in enumerate(entries):
            self.connection.execute(
                """
                INSERT INTO schedule_entries(
                    generation_id, channel_id, asset_id, starts_at, ends_at,
                    media_start_ms, media_end_ms, entry_type, title, daypart_slug,
                    sequence_group, sequence_position, is_program, protected_block,
                    guide_listing_title
                ) VALUES (1, 1, ?, ?, ?, 0, ?, ?, ?, 'afternoon', ?, ?, ?, ?, ?)
                """,
                (
                    asset,
                    starts,
                    ends,
                    round(
                        (
                            datetime.fromisoformat(ends)
                            - datetime.fromisoformat(starts)
                        ).total_seconds()
                        * 1000
                    ),
                    kind,
                    title,
                    group,
                    position,
                    program,
                    int(kind == "complete_block"),
                    title if kind == "complete_block" else None,
                ),
            )
        self.connection.execute(
            """
            INSERT INTO block_cues(
                asset_id, offset_ms, end_offset_ms, cue_type, title,
                sequence_position, updated_at
            ) VALUES (4, 0, 3600000, 'program', 'Show Inside Block', 0, ?)
            """,
            (now,),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def publish_weather(self) -> None:
        root = self.root / "weather-channel"
        version = root / "versions" / "weather-1"
        version.mkdir(parents=True)
        (version / "weather-channel.mp4").write_bytes(b"weather")
        manifest = {
            "schema_version": 1,
            "channel": {"slug": "weather", "display_name": "RetroWeather"},
            "generation_id": "weather-1",
            "generated_at": "2026-07-28T11:30:00-07:00",
            "valid_until": "2026-07-28T14:30:00-07:00",
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
                "generation_id": "weather-1",
                "manifest": "versions/weather-1/channel.json",
            }),
            encoding="utf-8",
        )
        self.config.data["synthetic_channels"] = {
            "weather": {
                "enabled": True,
                "root": str(root),
                "display_name": "RetroWeather",
                "stale_warning_minutes": 60,
            }
        }
        self.config.data["guide"]["channel_numbers"]["weather"] = 42

    def test_weather_stays_out_of_guide_until_first_playable_publication(self):
        self.config.data["synthetic_channels"] = {
            "weather": {"enabled": True, "root": str(self.root / "weather-channel")}
        }
        service = GuideService(self.connection, self.config)
        moment = datetime.fromisoformat("2026-07-28T12:00:00-07:00")
        before = service.snapshot(moment)
        self.assertNotIn("weather", [item["slug"] for item in before["channels"]])
        self.publish_weather()
        after = service.snapshot(moment)
        self.assertIn("weather", [item["slug"] for item in after["channels"]])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_text_size_changes_render_fingerprint_and_font_styles(self):
        when = datetime.fromisoformat("2026-07-28T12:00:30-07:00")
        service = GuideService(self.connection, self.config)
        original = service.snapshot(when)
        self.config.data["guide"]["text_size_percent"] = 150
        enlarged = GuideService(self.connection, self.config).snapshot(when)
        self.assertNotEqual(original["fingerprint"], enlarged["fingerprint"])
        renderer = GuideRenderer(self.connection, self.config)
        ass = renderer._ass_scroll(
            enlarged, enlarged["channels"], 10.0, 10.0, 54,
            54 * len(enlarged["channels"]),
        )
        self.assertIn("Style: Cell,VCR OSD Mono,18,", ass)
        self.assertIn("Style: Header,VCR OSD Mono,26,", ass)

    def test_interstitial_is_grouped_under_its_program(self):
        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:00:30-07:00")
        )
        fox = payload["channels"][0]
        self.assertEqual(fox["channel_number"], 5)
        self.assertEqual(fox["now"]["title"], "Current Episode")
        self.assertEqual(fox["next"]["title"], "Next Episode")

    def test_final_up_next_keeps_the_outgoing_program_title_until_next_block(self):
        self.connection.execute(
            "UPDATE schedule_entries SET ends_at=?,media_end_ms=?,programming_block_id=? WHERE asset_id=2",
            ("2026-07-28T12:29:00-07:00", 1680000, 10),
        )
        self.connection.execute(
            "UPDATE schedule_entries SET starts_at=?,programming_block_id=? WHERE asset_id=3",
            ("2026-07-28T12:30:00-07:00", 11),
        )
        self.connection.executemany(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,
                sequence_position,is_program,programming_block_id
            ) VALUES (1,1,?,?,?,?,?,?,?,?)
            """,
            [
                ("2026-07-28T12:29:00-07:00", "2026-07-28T12:29:30-07:00",
                 "graphic_interstitial", "Station Interstitial", "afternoon", 20, 0, 10),
                ("2026-07-28T12:29:30-07:00", "2026-07-28T12:30:00-07:00",
                 "announcement_opportunity", "Up Next", "afternoon", 21, 0, 11),
            ],
        )
        self.connection.commit()

        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:29:45-07:00")
        )
        fox = payload["channels"][0]
        self.assertEqual(fox["now"]["title"], "Current Episode")
        self.assertEqual(fox["next"]["title"], "Next Episode")

    def test_unowned_continuity_remains_a_real_guide_gap(self):
        self.connection.execute(
            "UPDATE schedule_entries SET ends_at=?,media_end_ms=?,programming_block_id=? WHERE asset_id=2",
            ("2026-07-28T12:29:00-07:00", 1680000, 10),
        )
        self.connection.execute(
            "UPDATE schedule_entries SET starts_at=?,programming_block_id=? WHERE asset_id=3",
            ("2026-07-28T12:30:00-07:00", 11),
        )
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,
                sequence_position,is_program,programming_block_id
            ) VALUES (1,1,'2026-07-28T12:29:00-07:00','2026-07-28T12:30:00-07:00',
                      'continuity','Continuity','afternoon',20,0,NULL)
            """
        )
        self.connection.commit()

        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:29:45-07:00")
        )
        self.assertIsNone(payload["channels"][0]["now"])

    def test_up_next_from_previous_owner_does_not_pull_program_earlier(self):
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id, channel_id, asset_id, starts_at, ends_at,
                entry_type, title, daypart_slug, sequence_group,
                sequence_position, is_program, programming_owner, guide_label
            ) VALUES (
                1, 1, NULL,
                '2026-07-28T12:00:00-07:00',
                '2026-07-28T12:00:30-07:00',
                'announcement_opportunity', 'Up Next', 'afternoon', 2,
                99, 0, 'snick', 'SNICK'
            )
            """
        )
        self.connection.commit()

        listings = GuideService(self.connection, self.config)._program_listings(1)
        upcoming = next(item for item in listings if item.asset_id == 3)

        self.assertEqual(
            upcoming.starts_at,
            "2026-07-28T12:31:00-07:00",
        )

    def test_weather_guide_listings_repeat_across_loop_boundary(self):
        self.publish_weather()
        service = GuideService(self.connection, self.config)
        reference = datetime.fromisoformat("2026-07-28T12:00:00-07:00")
        near_loop_end = datetime.fromtimestamp(
            reference.timestamp() - reference.timestamp() % 120 + 119,
            tz=service.timezone,
        )
        before_count = self.connection.execute(
            "SELECT COUNT(*) FROM assets"
        ).fetchone()[0]
        payload = service.snapshot(near_loop_end)
        after_count = self.connection.execute(
            "SELECT COUNT(*) FROM assets"
        ).fetchone()[0]
        weather = next(
            item for item in payload["channels"] if item["slug"] == "weather"
        )

        self.assertEqual(before_count, after_count)
        self.assertEqual(weather["channel_number"], 42)
        self.assertEqual(weather["now"]["title"], "Local Forecast")
        self.assertEqual(weather["next"]["title"], "Current Conditions")
        self.assertEqual(weather["now"]["entry_type"], "synthetic_channel")
        self.assertEqual(payload["synthetic_generations"], {"weather": "weather-1"})
        after_loop = service.snapshot(near_loop_end + timedelta(seconds=1))
        self.assertEqual(payload["fingerprint"], after_loop["fingerprint"])

    def test_split_episode_remains_one_listing_through_midroll(self):
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET ends_at='2026-07-28T12:12:00-07:00', media_end_ms=660000,
                programming_block_id=10
            WHERE asset_id=2
            """
        )
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,asset_id,starts_at,ends_at,
                media_start_ms,media_end_ms,entry_type,title,daypart_slug,
                sequence_group,sequence_position,is_program,protected_block,
                programming_block_id
            ) VALUES
              (1,1,1,'2026-07-28T12:12:00-07:00','2026-07-28T12:14:00-07:00',
               0,120000,'commercial','Ad','afternoon',1,20,0,0,10),
              (1,1,2,'2026-07-28T12:14:00-07:00','2026-07-28T12:31:00-07:00',
               660000,1680000,'episode','Current Episode','afternoon',1,21,1,0,10)
            """
        )
        self.connection.commit()
        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:13:00-07:00")
        )
        listing = payload["channels"][0]["now"]
        self.assertEqual(listing["title"], "Current Episode")
        self.assertNotEqual(listing["entry_type"], "episode_package")
        self.assertEqual(listing["programming_block_id"], 10)

    def test_current_block_uses_program_cue_title(self):
        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T14:30:00-07:00")
        )
        self.assertEqual(
            payload["channels"][0]["now"]["title"],
            "Show Inside Block",
        )

    def test_columns_are_anchored_to_the_current_half_hour(self):
        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T13:29:00-07:00")
        )
        self.assertEqual(
            payload["column_times"],
            [
                "2026-07-28T13:00:00-07:00",
                "2026-07-28T13:30:00-07:00",
                "2026-07-28T14:00:00-07:00",
            ],
        )
        self.assertIsNone(payload["channels"][0]["columns"][0])
        self.assertIsNone(payload["channels"][0]["now"])

    def test_columns_shift_forward_at_half_hour_boundary(self):
        service = GuideService(self.connection, self.config)
        before = service.snapshot(
            datetime.fromisoformat("2026-07-28T13:29:59-07:00")
        )
        after = service.snapshot(
            datetime.fromisoformat("2026-07-28T13:30:00-07:00")
        )
        self.assertEqual(before["column_times"][1:], after["column_times"][:2])
        self.assertEqual(
            before["channels"][0]["columns"][1:],
            after["channels"][0]["columns"][:2],
        )

    def test_now_column_uses_program_playing_at_current_instant(self):
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET ends_at = '2026-07-28T12:13:00-07:00',
                media_end_ms = 720000
            WHERE asset_id = 2
            """
        )
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET starts_at = '2026-07-28T12:13:00-07:00'
            WHERE asset_id = 3
            """
        )
        self.connection.commit()

        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:20:00-07:00")
        )
        fox = payload["channels"][0]
        self.assertEqual(fox["now"]["title"], "Next Episode")
        self.assertEqual(fox["columns"][0]["title"], "Next Episode")

        before = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:05:00-07:00")
        )
        self.assertEqual(
            before["channels"][0]["columns"][0]["title"],
            "Current Episode",
        )
        self.assertEqual(before["column_times"], payload["column_times"])
        self.assertNotEqual(before["fingerprint"], payload["fingerprint"])

    def test_listing_uses_series_folder_then_filename(self):
        self.connection.execute(
            """
            UPDATE assets
            SET series_title = 'Clarissa Explains It All',
                original_filename = 'CEIA S01E04 Urge to Drive.mp4'
            WHERE id = 2
            """
        )
        self.connection.commit()
        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        listing = payload["channels"][0]["now"]
        self.assertEqual(listing["series_title"], "Clarissa Explains It All")
        self.assertEqual(listing["file_title"], "CEIA S01E04 Urge to Drive")
        self.assertEqual(
            listing["title"],
            "Clarissa Explains It All — CEIA S01E04 Urge to Drive",
        )

    def test_future_slot_groups_two_episodes_from_same_series(self):
        self.connection.execute(
            """
            UPDATE assets
            SET series_title = 'Short Series'
            WHERE id IN (2, 3)
            """
        )
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET ends_at = '2026-07-28T12:13:00-07:00',
                media_end_ms = 720000
            WHERE asset_id = 2
            """
        )
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET starts_at = '2026-07-28T12:13:00-07:00'
            WHERE asset_id = 3
            """
        )
        self.connection.commit()
        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T11:40:00-07:00")
        )
        package = payload["channels"][0]["columns"][1]
        self.assertEqual(package["series_title"], "Short Series")
        self.assertEqual(package["file_title"], "2 episodes")
        self.assertEqual(package["entry_type"], "episode_package")

    def test_future_slot_groups_three_episodes_from_same_series(self):
        now = utc_now()
        storage = ensure_storage_root(self.connection, "media")
        self.connection.execute(
            """
            INSERT INTO assets(
                id, source_root_id, source_relative_path, original_filename,
                file_size, modified_ns, media_type, display_title, series_title,
                channel_slug, daypart_slug, metadata_status, integrity_status,
                review_status, enabled_for_scheduling, first_seen_at, last_seen_at
            ) VALUES (5, ?, 'fox/afternoon/episodes/5.mp4', '5.mp4', 1, 1,
                      'episode', 'Third Short', 'Short Series', 'fox', 'afternoon',
                      'ready', 'ready', 'ready', 1, ?, ?)
            """,
            (storage, now, now),
        )
        self.connection.execute(
            "UPDATE assets SET series_title = 'Short Series' WHERE id IN (2, 3)"
        )
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET ends_at = '2026-07-28T12:13:00-07:00', media_end_ms = 720000
            WHERE asset_id = 2
            """
        )
        self.connection.execute(
            """
            UPDATE schedule_entries
            SET starts_at = '2026-07-28T12:13:00-07:00',
                ends_at = '2026-07-28T12:21:00-07:00', media_end_ms = 480000
            WHERE asset_id = 3
            """
        )
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id, channel_id, asset_id, starts_at, ends_at,
                media_start_ms, media_end_ms, entry_type, title, daypart_slug,
                sequence_group, sequence_position, is_program, protected_block
            ) VALUES (1, 1, 5, '2026-07-28T12:21:00-07:00',
                      '2026-07-28T12:29:00-07:00', 0, 480000, 'episode',
                      'Third Short', 'afternoon', 4, 5, 1, 0)
            """
        )
        self.connection.commit()

        payload = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T11:40:00-07:00")
        )
        package = payload["channels"][0]["columns"][1]
        self.assertEqual(package["series_title"], "Short Series")
        self.assertEqual(package["file_title"], "3 episodes")
        self.assertEqual(package["entry_type"], "episode_package")

    def test_prerendered_guide_expires_at_the_next_half_hour(self):
        guide_path = self.root / "guide.mp4"
        guide_path.write_bytes(b"guide")
        now = utc_now()
        timezone = GuideRenderer(
            self.connection, self.config
        ).service.timezone
        slot_time = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone)
        fingerprint = GuideService(
            self.connection, self.config
        ).snapshot(slot_time)["fingerprint"]
        self.connection.execute(
            """
            INSERT INTO guide_renders(
                window_starts_at, window_ends_at, output_relative_path,
                source_fingerprint, page_count, duration_seconds, status,
                generated_at, activated_at
            ) VALUES (?, ?, ?, ?, 1, 10, 'valid', ?, ?)
            """,
            (
                "2026-07-28T10:00:00-07:00",
                "2026-07-28T11:30:00-07:00",
                guide_path.relative_to(self.project).as_posix(),
                fingerprint,
                now,
                now,
            ),
        )
        self.connection.commit()
        renderer = GuideRenderer(self.connection, self.config)
        self.assertFalse(
            renderer.needs_refresh(
                datetime(2026, 7, 28, 10, 29, 59, tzinfo=timezone)
            )
        )

    def test_prerendered_guide_expires_when_listing_fingerprint_changes(self):
        guide_path = self.root / "guide.mp4"
        guide_path.write_bytes(b"guide")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO guide_renders(
                window_starts_at, window_ends_at, output_relative_path,
                source_fingerprint, page_count, duration_seconds, status,
                generated_at, activated_at
            ) VALUES (
                '2026-07-28T10:00:00-07:00',
                '2026-07-28T11:30:00-07:00',
                ?, 'obsolete-renderer-fingerprint', 1, 10, 'valid', ?, ?
            )
            """,
            (guide_path.relative_to(self.project).as_posix(), now, now),
        )
        self.connection.commit()
        renderer = GuideRenderer(self.connection, self.config)
        timezone = renderer.service.timezone
        self.assertTrue(
            renderer.needs_refresh(
                datetime(2026, 7, 28, 10, 10, 0, tzinfo=timezone)
            )
        )
        self.assertTrue(
            renderer.needs_refresh(
                datetime(2026, 7, 28, 10, 30, 0, tzinfo=timezone)
            )
        )

    def test_scroll_belt_duplicates_rows_for_seamless_wrap(self):
        self.connection.execute(
            """
            UPDATE assets
            SET series_title = 'Current Series',
                original_filename = 'S01E01 Pilot.mp4'
            WHERE id = 2
            """
        )
        self.connection.commit()
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        ass = renderer._ass_scroll(
            snapshot,
            snapshot["channels"],
            duration=3.2,
            cycle_duration=3.2,
            row_height=64,
            scroll_distance=64,
        )
        self.assertIn(r"\move(0,102,0,38,0,3200)", ass)
        self.assertIn(r"\move(0,166,0,102,0,3200)", ass)
        # One-channel test belts need seven copies to cover the 338px viewport
        # while the belt travels its full 64px loop distance.
        self.assertEqual(ass.count("5  FOX"), 7)
        self.assertIn("Good Afternoon!", ass)
        self.assertIn("Style: ChannelHeader,VCR OSD Mono,17,&H00FFFFFF", ass)
        self.assertIn("Style: Channel,VCR OSD Mono,16,&H0000FFFF", ass)
        self.assertNotIn(r"\fad", ass)
        self.assertNotIn("Style: Clock", ass)
        self.assertIn(r"{\fs13\b1}", ass)
        self.assertIn(r"{\fs9\b0}", ass)

    def test_scroll_belt_repeats_for_each_music_cycle(self):
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        ass = renderer._ass_scroll(
            snapshot,
            snapshot["channels"],
            duration=6.4,
            cycle_duration=3.2,
            row_height=64,
            scroll_distance=64,
        )
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:03.20,ChannelBox", ass)
        self.assertIn("Dialogue: 0,0:00:03.20,0:00:06.40,ChannelBox", ass)
        self.assertEqual(ass.count("5  FOX"), 14)

    def test_continuous_program_spans_adjacent_guide_columns(self):
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        channel = snapshot["channels"][0]
        hour_program = dict(channel["columns"][0])
        hour_program.update(
            {
                "entry_id": 900,
                "title": "The Crocodile Hunter",
                "series_title": "The Crocodile Hunter",
                "file_title": "Outback Adventures",
                "starts_at": "2026-07-28T12:00:00-07:00",
                "ends_at": "2026-07-28T13:00:00-07:00",
            }
        )
        channel["columns"] = [hour_program, dict(hour_program), None]
        ass = renderer._ass_scroll(
            snapshot,
            [channel],
            duration=3.2,
            cycle_duration=3.2,
            row_height=64,
            scroll_distance=64,
        )
        self.assertIn("m 164 0 l 464 0 464 62 164 62", ass)
        self.assertNotIn("m 164 0 l 312 0 312 62 164 62", ass)

    def test_separate_programs_with_same_title_do_not_span_columns(self):
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        channel = snapshot["channels"][0]
        first = dict(channel["columns"][0])
        first.update(
            {
                "entry_id": 900,
                "title": "The Crocodile Hunter",
                "starts_at": "2026-07-28T12:00:00-07:00",
                "ends_at": "2026-07-28T12:30:00-07:00",
            }
        )
        second = dict(first)
        second.update(
            {
                "entry_id": 901,
                "starts_at": "2026-07-28T12:30:00-07:00",
                "ends_at": "2026-07-28T13:00:00-07:00",
            }
        )
        channel["columns"] = [first, second, None]
        ass = renderer._ass_scroll(
            snapshot,
            [channel],
            duration=3.2,
            cycle_duration=3.2,
            row_height=64,
            scroll_distance=64,
        )
        self.assertIn("m 164 0 l 312 0 312 62 164 62", ass)
        self.assertIn("m 316 0 l 464 0 464 62 316 62", ass)

    def test_guide_music_folder_is_created_and_previous_track_is_avoided(self):
        renderer = GuideRenderer(self.connection, self.config)
        music_dir = renderer._music_directory()
        first = music_dir / "first.mp3"
        second = music_dir / "second.mp3"
        first.write_bytes(b"test")
        second.write_bytes(b"test")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO guide_renders(
                window_starts_at, window_ends_at, output_relative_path,
                source_fingerprint, page_count, duration_seconds, status,
                generated_at, activated_at, music_relative_path
            ) VALUES ('2026-07-28T10:00:00-07:00',
                      '2026-07-28T11:30:00-07:00', 'guide.mp4', 'old',
                      1, 10, 'valid', ?, ?, '_guide/music/first.mp3')
            """,
            (now, now),
        )
        self.connection.commit()
        renderer._audio_duration = lambda _path: 90.0
        selected, duration, relative = renderer._select_music()
        self.assertEqual(selected, second)
        self.assertEqual(duration, 90.0)
        self.assertEqual(relative, "_guide/music/second.mp3")

    def test_guide_music_stops_at_previous_complete_visual_seam(self):
        self.assertEqual(GuideRenderer._music_cycle_count(100.0, 16.8), 5)
        self.assertEqual(GuideRenderer._music_cycle_count(33.6, 16.8), 2)
        self.assertEqual(GuideRenderer._music_cycle_count(8.0, 16.8), 1)

    def test_guide_music_is_padded_not_restarted(self):
        renderer = GuideRenderer(self.connection, self.config)
        captured = {}

        def capture(command, *, cwd):
            captured["command"] = command
            captured["cwd"] = cwd

        renderer._run = capture
        renderer._encode_page(
            self.root,
            "guide.ass",
            "guide.mp4",
            16.8,
            music_path=self.root / "music.mp3",
        )
        command = captured["command"]
        self.assertNotIn("-stream_loop", command)
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("apad,atrim=0:16.8", filter_graph)

    def test_channel_labels_can_be_full_or_vowel_abbreviated(self):
        self.assertEqual(
            GuideRenderer._channel_label(33, "Nickelodeon"),
            "33  NICKELODEON",
        )
        self.assertEqual(
            GuideRenderer._channel_label(
                32, "Cartoon Network", abbreviate=True
            ),
            "32  CRTNNTWRK",
        )
        self.assertEqual(
            GuideRenderer._channel_label(5, "Fox", abbreviate=True),
            "5  FOX",
        )

    def test_greeting_replaces_continuous_listings_footer(self):
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        ass = renderer._ass_scroll(
            snapshot,
            snapshot["channels"],
            duration=3.2,
            cycle_duration=3.2,
            row_height=64,
            scroll_distance=64,
        )
        self.assertIn("Good Afternoon!", ass)
        self.assertNotIn("CONTINUOUS LISTINGS", ass)

    def test_pending_dashboard_action_renders_yellow_alert_badge(self):
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T12:10:00-07:00")
        )
        snapshot["pending_actions"] = {
            "library_review": 1,
            "aspect_review": 0,
            "conversion_ready": 0,
            "total": 1,
            "show_indicator": True,
        }
        ass = renderer._ass_scroll(
            snapshot,
            snapshot["channels"],
            duration=3.2,
            cycle_duration=3.2,
            row_height=64,
            scroll_distance=64,
        )
        self.assertIn("Style: AlertBox,Arial,1,&H0000FFFF", ass)
        self.assertIn("[!]", ass)

    def test_holiday_greeting_overrides_the_normal_daypart_message(self):
        self.config.data["seasonal"] = {
            "holidays": {
                "halloween": {"start": "10-01", "end": "10-31"}
            }
        }
        self.config.data["guide"]["holiday_greetings"] = {
            "halloween": {"morning": "A Spooky Good Morning!"}
        }
        service = GuideService(self.connection, self.config)
        moment = datetime.fromisoformat("2026-10-15T09:00:00-07:00")
        self.assertEqual(
            service._guide_greeting(moment, service._daypart_at(moment)),
            "A Spooky Good Morning!",
        )

    def test_gap_is_rendered_as_tbd_instead_of_next_future_show(self):
        renderer = GuideRenderer(self.connection, self.config)
        snapshot = GuideService(self.connection, self.config).snapshot(
            datetime.fromisoformat("2026-07-28T13:30:00-07:00")
        )
        self.assertIsNone(snapshot["channels"][0]["columns"][0])
        ass = renderer._ass_scroll(
            snapshot,
            snapshot["channels"],
            duration=3.2,
            cycle_duration=3.2,
            row_height=48,
            scroll_distance=48,
        )
        self.assertIn("TBD", ass)
        self.assertNotIn("No listing", ass)


if __name__ == "__main__":
    unittest.main()
