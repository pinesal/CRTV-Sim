import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig, load_config
from retrotv.db import apply_migrations, connect_database, utc_now
from retrotv.settings import SettingsService, settings_override_path


class SettingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / ".test-tmp" / f"settings-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.profile = self.root / "windows.local.toml"
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.backup_root = self.root / "backup"
        self.profile.write_text(
            f"""
[application]
database_path = "./state.sqlite3"
timezone = "America/Los_Angeles"
[storage_roots]
media = "{self.media_root.as_posix()}"
originals_backup = "{self.backup_root.as_posix()}"
[guide]
channel_number = 2
music_volume = 0.2
[scheduling]
[[scheduling.dayparts]]
slug = "morning"
start = "06:00"
end = "12:00"
boundary_policy = "must_fit"
[[scheduling.dayparts]]
slug = "afternoon"
start = "12:00"
end = "18:00"
boundary_policy = "must_fit"
[[scheduling.dayparts]]
slug = "night"
start = "18:00"
end = "06:00"
boundary_policy = "must_fit"
""".strip(),
            encoding="utf-8",
        )
        self.config = AppConfig(
            self.profile,
            self.project,
            {
                "profile": "test",
                "application": {"database_path": str(self.root / "state.sqlite3")},
                "storage_roots": {
                    "media": str(self.media_root),
                    "originals_backup": str(self.backup_root),
                },
                "guide": {"channel_number": 2, "music_volume": 0.2},
                "scheduling": {
                    "dayparts": [
                        {"slug": "morning", "start": "06:00", "end": "12:00", "boundary_policy": "must_fit"},
                        {"slug": "afternoon", "start": "12:00", "end": "18:00", "boundary_policy": "must_fit"},
                        {"slug": "night", "start": "18:00", "end": "06:00", "boundary_policy": "must_fit"},
                    ]
                },
                "seasonal": {"holidays": {}, "windows": {}},
            },
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_save_uses_json_overlay_and_applies_live_safe_settings(self):
        service = SettingsService(self.config)
        result = service.save({
            "values": {
                "guide.music_volume": 0.35,
                "storage_roots.media": "E:/RetroMedia",
            }
        })
        self.assertTrue(result["restart_required"])
        self.assertEqual(self.config.data["guide"]["music_volume"], 0.35)
        self.assertEqual(self.config.data["storage_roots"]["media"], str(self.media_root))
        saved = json.loads(settings_override_path(self.config).read_text(encoding="utf-8"))
        self.assertEqual(saved["storage_roots"]["media"], "E:/RetroMedia")

    def test_startup_destinations_include_the_guide_without_adding_a_channel(self):
        channels = [{"slug": "fox", "display_name": "FOX", "channel_number": 5}]
        snapshot = SettingsService(self.config).snapshot(channels)
        self.assertEqual([item["slug"] for item in snapshot["channels"]], ["fox"])
        self.assertEqual(
            [item["slug"] for item in snapshot["startup_destinations"]],
            ["fox", "__guide__"],
        )

    def test_load_config_merges_profile_specific_settings_overlay(self):
        settings_override_path(self.config).write_text(
            json.dumps({"guide": {"music_volume": 0.42}}), encoding="utf-8"
        )
        loaded = load_config(self.profile)
        self.assertEqual(loaded.data["guide"]["music_volume"], 0.42)

    def test_dayparts_must_be_contiguous(self):
        with self.assertRaisesRegex(ValueError, "contiguous"):
            SettingsService(self.config).save({
                "values": {},
                "dayparts": [
                    {"slug": "morning", "start": "06:00", "end": "12:00", "boundary_policy": "must_fit"},
                    {"slug": "night", "start": "13:00", "end": "06:00", "boundary_policy": "must_fit"},
                ],
            })

    def test_unchanged_dayparts_do_not_reclassify_on_unrelated_save(self):
        service = SettingsService(self.config)
        with patch.object(service, "_refresh_daypart_eligibility") as refresh:
            result = service.save({
                "values": {"guide.music_volume": 0.35},
                "dayparts": self.config.data["scheduling"]["dayparts"],
            })
        self.assertFalse(result["dayparts_changed"])
        self.assertEqual(result["daypart_directories_created"], [])
        refresh.assert_not_called()

    def test_real_daypart_change_preserves_active_schedule_until_rebuilt(self):
        with connect_database(self.config.database_path) as connection:
            apply_migrations(connection, self.project / "migrations")
            connection.execute(
                "INSERT INTO channels(id,slug,display_name,enabled) VALUES (1,'fox','FOX',1)"
            )
            connection.execute(
                """INSERT INTO schedule_generations(
                    channel_id,schedule_date,seed,configuration_fingerprint,
                    status,validation_status,generated_at
                ) VALUES (1,'2026-07-27','seed','old','active','valid',?)""",
                (utc_now(),),
            )
            connection.commit()
        dayparts = [dict(item) for item in self.config.data["scheduling"]["dayparts"]]
        dayparts[0]["end"] = "11:00"
        dayparts[1]["start"] = "11:00"
        result = SettingsService(self.config).save({"values": {}, "dayparts": dayparts})
        self.assertTrue(result["dayparts_changed"])
        with connect_database(self.config.database_path) as connection:
            status = connection.execute(
                "SELECT status FROM schedule_generations WHERE channel_id=1"
            ).fetchone()[0]
        self.assertEqual(status, "active")

    def test_saving_dayparts_creates_channel_template_and_shared_media_folders(self):
        (self.media_root / "Nickelodeon").mkdir()
        payload = {
            "values": {},
            "dayparts": [
                {"slug": "morning", "start": "06:00", "end": "12:00", "boundary_policy": "must_fit"},
                {"slug": "afternoon", "start": "12:00", "end": "18:00", "boundary_policy": "must_fit"},
                {"slug": "night", "start": "18:00", "end": "00:00", "boundary_policy": "must_fit"},
                {"slug": "over-night", "start": "00:00", "end": "06:00", "boundary_policy": "must_fit"},
            ],
        }
        result = SettingsService(self.config).save(payload)
        for root in (self.media_root / "Nickelodeon", self.media_root / "_channel_template"):
            self.assertTrue((root / "over-night" / "episodes").is_dir())
            self.assertTrue((root / "over-night" / "filler").is_dir())
        self.assertTrue((self.media_root / "shared" / "all-day" / "filler").is_dir())
        self.assertTrue((self.media_root / "shared" / "over-night" / "filler").is_dir())
        self.assertTrue(result["daypart_directories_created"])

    def test_duplicate_channel_numbers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "already in use"):
            SettingsService(self.config).save({
                "values": {},
                "channel_numbers": {"fox": 5, "hbo": 5},
            })

    def test_weather_settings_and_channel_number_are_persisted(self):
        result = SettingsService(self.config).save({
            "values": {
                "synthetic_channels.weather.enabled": True,
                "synthetic_channels.weather.root": "E:/RetroWeather/weather",
                "synthetic_channels.weather.display_name": "Weather 42",
                "synthetic_channels.weather.stale_warning_minutes": 45,
                "synthetic_channels.weather.manage_companion": True,
                "synthetic_channels.weather.companion_home": "E:/RetroWeather",
                "synthetic_channels.weather.companion_host": "127.0.0.1",
                "synthetic_channels.weather.companion_port": 8775,
            },
            "channel_numbers": {"weather": 42},
        })
        saved = json.loads(
            settings_override_path(self.config).read_text(encoding="utf-8")
        )

        self.assertTrue(result["restart_required"])
        self.assertTrue(self.config.data["synthetic_channels"]["weather"]["enabled"])
        self.assertEqual(self.config.data["guide"]["channel_numbers"]["weather"], 42)
        self.assertEqual(
            saved["synthetic_channels"]["weather"]["root"],
            "E:/RetroWeather/weather",
        )

    def test_saving_weekly_block_creates_drag_and_drop_collection_tree(self):
        service = SettingsService(self.config)
        payload = {
            "values": {},
            "holidays": {
                "halloween": {"start": "10-01", "end": "10-31", "mode": "boost"}
            },
            "windows": {
                "saturday-morning": {
                    "channel": "fox",
                    "weekdays": ["saturday"],
                    "start": "07:00",
                    "end": "10:00",
                    "collection": "saturday-morning",
                    "guide_label": "Saturday Morning",
                    "priority": 300,
                    "enabled": True,
                }
            },
        }

        first = service.save(payload)
        collection = (
            self.media_root / "fox" / "specials" / "weekly" / "saturday-morning"
        )
        expected = {
            "episodes", "shorts", "movies", "blocks", "bumpers", "commercials",
            "promos", "station-ids", "filler", "up-next", "interstitials",
        }
        self.assertEqual({path.name for path in collection.iterdir()}, expected)
        self.assertEqual(len(first["weekly_directories_created"]), len(expected))
        self.assertEqual(first["weekly_collection_roots"], [str(collection.resolve())])
        holiday_variant = (
            self.media_root / "fox" / "specials" / "holidays" / "halloween"
            / "specials" / "weekly" / "saturday-morning"
        )
        self.assertTrue((holiday_variant / "up-next").is_dir())
        self.assertTrue((holiday_variant / "episodes").is_dir())
        self.assertTrue((self.media_root / "fox" / "specials" / "holidays" / "halloween"
                         / "night" / "blocks").is_dir())
        self.assertTrue((self.media_root / "fox" / "specials" / "holidays" / "halloween"
                         / "daytime" / "episodes").is_dir())
        self.assertTrue((self.media_root / "fox" / "graphics" / "holidays" / "halloween"
                         / "up-next").is_dir())
        self.assertTrue((self.media_root / "_channel_template" / "graphics" / "night"
                         / "holidays" / "halloween" / "interstitials").is_dir())
        self.assertFalse((self.media_root / "fox" / "specials" / "holidays" / "halloween"
                          / "graphics").exists())
        self.assertTrue((self.media_root / "shared" / "specials" / "holidays" / "halloween"
                         / "daytime" / "episodes").is_dir())
        self.assertTrue(first["holiday_directories_created"])

        marker = collection / "episodes" / "keep-me.txt"
        marker.write_text("preserved", encoding="utf-8")
        second = service.save(payload)
        self.assertEqual(second["weekly_directories_created"], [])
        self.assertEqual(second["holiday_directories_created"], [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")
        later = service.save({
            "values": {},
            "holidays": {
                **payload["holidays"],
                "christmas": {"start": "12-01", "end": "12-31", "mode": "boost"},
            },
        })
        self.assertTrue((self.media_root / "fox" / "specials" / "holidays" / "christmas"
                         / "specials" / "weekly" / "saturday-morning" / "up-next").is_dir())
        self.assertTrue(later["holiday_directories_created"])

    def test_weekly_block_reuses_human_named_channel_folder(self):
        human_folder = self.media_root / "Cartoon Network"
        human_folder.mkdir(parents=True)
        payload = {
            "values": {},
            "windows": {
                "adult-swim": {
                    "channel": "cartoon-network",
                    "weekdays": ["monday"],
                    "start": "23:00",
                    "end": "05:00",
                    "collection": "adult-swim",
                    "guide_label": "Adult Swim",
                    "priority": 300,
                    "enabled": False,
                }
            },
        }

        result = SettingsService(self.config).save(payload)

        expected = human_folder / "specials" / "weekly" / "adult-swim"
        self.assertTrue(expected.is_dir())
        self.assertFalse((self.media_root / "cartoon-network").exists())
        self.assertEqual(
            result["weekly_collection_roots"], [str(expected.resolve())]
        )


if __name__ == "__main__":
    unittest.main()
