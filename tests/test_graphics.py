import shutil
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database
from retrotv.graphics import GraphicsService


class GraphicsTests(unittest.TestCase):
    @patch("retrotv.graphics.subprocess.run")
    def test_holiday_global_overrides_standard_daypart_and_canonical_wins_legacy(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        graphics = self.media / "nickelodeon" / "graphics"
        for purpose in ("up-next", "interstitials"):
            # Seeded placeholders must fall through to authored holiday art.
            night_holiday = graphics / "night" / "holidays" / "halloween" / purpose
            standard = graphics / "night" / purpose / "standard.png"
            standard.write_bytes(b"standard")
            global_holiday = graphics / "holidays" / "halloween" / purpose / "canonical.png"
            global_holiday.write_bytes(b"canonical")
            legacy = self.media / "nickelodeon" / "specials" / "holidays" / "halloween" / "graphics" / purpose / "legacy.png"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")
            service.scan_catalog()
            self.assertEqual(service.select("nickelodeon", purpose,
                datetime.fromisoformat("2026-10-15T22:30:00-07:00")), global_holiday)
            daypart = night_holiday / "night-halloween.png"
            daypart.write_bytes(b"night")
            service.scan_catalog()
            self.assertEqual(service.select("nickelodeon", purpose,
                datetime.fromisoformat("2026-10-15T22:30:00-07:00")), daypart)
            self.assertEqual(service.select("nickelodeon", purpose,
                datetime.fromisoformat("2026-09-15T22:30:00-07:00")), standard)

    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / ".test-tmp" / f"graphics-{uuid.uuid4().hex}"
        self.media = self.root / "media"
        self.media.mkdir(parents=True)
        self.config = AppConfig(
            self.root / "test.toml",
            self.project,
            {
                "application": {"database_path": str(self.root / "db.sqlite3")},
                "storage_roots": {"media": str(self.media)},
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                "graphics": {"holidays": ["halloween", "christmas"]},
                "scheduling": {"dayparts": [
                    {"slug": "evening", "start": "16:00", "end": "20:00"},
                    {"slug": "night", "start": "20:00", "end": "06:00"},
                    {"slug": "morning", "start": "06:00", "end": "12:00"},
                    {"slug": "afternoon", "start": "12:00", "end": "16:00"},
                ]},
                "daypart_branding": {
                    "nickelodeon": {
                        "night": {"display_name": "Nick at Nite"}
                    }
                },
                "seasonal": {
                    "windows": {
                        "snick": {
                            "channel": "nickelodeon",
                            "weekdays": ["saturday"],
                            "start": "20:00",
                            "end": "22:00",
                            "collection": "snick",
                            "guide_label": "SNICK",
                            "priority": 300,
                            "enabled": True,
                        }
                    }
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        self.connection.execute(
            "INSERT INTO channels(id,slug,display_name,enabled) VALUES (1,'nickelodeon','Nickelodeon',1)"
        )
        (self.media / "nickelodeon").mkdir()
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @patch("retrotv.graphics.subprocess.run")
    def test_structure_and_placeholders_are_created_without_overwrite(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        result = service.ensure_all()
        default = self.media / "nickelodeon" / "graphics" / "up-next" / "placeholder.png"
        holiday = self.media / "nickelodeon" / "graphics" / "holidays" / "halloween" / "interstitials" / "placeholder.png"
        template = self.media / "_channel_template" / "graphics" / "up-next"
        self.assertTrue(default.is_file())
        self.assertTrue(holiday.is_file())
        self.assertTrue(template.is_dir())
        self.assertEqual(
            default.read_bytes(),
            (self.project / "assets" / "templates" / "up-next-background-template.png").read_bytes(),
        )
        original = default.read_bytes()
        service.ensure_all()
        self.assertEqual(default.read_bytes(), original)
        self.assertGreaterEqual(result["cataloged"], 6)

    @patch("retrotv.graphics.subprocess.run")
    def test_night_override_structure_and_selection_precede_channel_default(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        graphics = self.media / "nickelodeon" / "graphics"
        night = graphics / "night" / "up-next"
        self.assertTrue((night / "placeholder.png").is_file())
        (graphics / "up-next" / "nick.png").write_bytes(b"nick")
        (night / "nick-at-nite.png").write_bytes(b"nite")
        service.scan_catalog()

        selected = service.select(
            "nickelodeon", "up-next",
            datetime.fromisoformat("2026-09-15T20:30:00-07:00"),
        )
        self.assertEqual(selected.name, "nick-at-nite.png")
        daytime = service.select(
            "nickelodeon", "up-next",
            datetime.fromisoformat("2026-09-15T19:30:00-07:00"),
        )
        self.assertEqual(daytime.name, "nick.png")

    @patch("retrotv.graphics.subprocess.run")
    def test_explicit_promoted_daypart_can_switch_branding_before_boundary(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        selected = service.select(
            "nickelodeon", "up-next",
            datetime.fromisoformat("2026-09-15T19:59:30-07:00"),
            daypart_slug="night",
        )
        self.assertIn("night", [part.casefold() for part in selected.parts])

    @patch("retrotv.graphics.subprocess.run")
    def test_holiday_graphic_precedes_default(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        selected = service.select(
            "nickelodeon", "up-next", datetime.fromisoformat("2026-10-15T12:00:00-07:00")
        )
        self.assertIn("holidays", selected.parts)
        self.assertIn("halloween", [part.casefold() for part in selected.parts])

    @patch("retrotv.graphics.subprocess.run")
    def test_channel_shaped_holiday_graphics_are_cataloged(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        root = (self.media / "nickelodeon" / "specials" / "holidays"
                / "halloween" / "graphics")
        image = root / "up-next" / "halloween.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"halloween")
        # A copied channel template may contain another holidays directory.
        # It is not a second level of holiday overrides.
        nested = root / "holidays" / "christmas" / "up-next"
        nested.mkdir(parents=True)
        (nested / "placeholder.png").write_bytes(b"placeholder")
        service.scan_catalog()
        self.assertEqual(
            service.select("nickelodeon", "up-next", datetime.fromisoformat("2026-10-15T12:00:00-07:00")),
            image,
        )
        self.assertIsNone(self.connection.execute(
            "SELECT id FROM graphic_assets WHERE relative_path LIKE '%halloween/graphics/holidays/%'"
        ).fetchone())

    @patch("retrotv.graphics.subprocess.run")
    def test_user_graphics_are_random_choices_without_placeholder_competition(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        folder = self.media / "nickelodeon" / "graphics" / "interstitials"
        (folder / "alpha.png").write_bytes(b"alpha")
        (folder / "beta.png").write_bytes(b"beta")
        service.scan_catalog()

        class PickLast:
            @staticmethod
            def choice(items):
                return items[-1]

        selected = service.select_asset(
            "nickelodeon",
            "interstitials",
            datetime.fromisoformat("2026-09-15T12:00:00-07:00"),
            rng=PickLast(),
        )
        self.assertEqual(selected["provenance"], "user")
        self.assertIn(Path(selected["relative_path"]).name, {"alpha.png", "beta.png"})

    @patch("retrotv.graphics.subprocess.run")
    def test_weekly_collection_graphics_and_music_override_then_fall_back(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        weekly = self.media / "nickelodeon" / "specials" / "weekly" / "snick"
        self.assertTrue((weekly / "up-next").is_dir())
        self.assertTrue((weekly / "interstitials").is_dir())
        self.assertFalse((weekly / "up-next" / "placeholder.png").exists())
        (weekly / "up-next" / "snick.png").write_bytes(b"snick")
        (weekly / "up-next" / "snick.mp3").write_bytes(b"music")
        service.scan_catalog()

        during = datetime.fromisoformat("2026-09-19T20:30:00-07:00")
        selected = service.select("nickelodeon", "up-next", during)
        self.assertEqual(selected, weekly / "up-next" / "snick.png")
        self.assertEqual(
            service.select_music("nickelodeon", "up-next", during),
            weekly / "up-next" / "snick.mp3",
        )
        leaving = service.select("nickelodeon", "up-next", during,
                                 weekly_collection_slug=None, daypart_slug="night")
        self.assertNotEqual(leaving, weekly / "up-next" / "snick.png")
        self.assertIn("graphics", leaving.parts)
        self.assertNotEqual(service.select_music(
            "nickelodeon", "up-next", during, weekly_collection_slug=None,
            daypart_slug="night"), weekly / "up-next" / "snick.mp3")
        cataloged = self.connection.execute(
            "SELECT weekly_collection_slug FROM graphic_assets WHERE relative_path LIKE ?",
            ("%snick.png",),
        ).fetchone()
        self.assertEqual(cataloged["weekly_collection_slug"], "snick")

        # Empty optional folders do not create placeholders or suppress the
        # normal Nick at Nite fallback hierarchy.
        fallback = service.select("nickelodeon", "interstitials", during)
        self.assertIn("graphics", fallback.parts)
        self.assertIn("night", [part.casefold() for part in fallback.parts])

    @patch("retrotv.graphics.subprocess.run")
    def test_holiday_weekly_graphics_override_only_that_collection_and_holiday(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        weekly = self.media / "nickelodeon" / "specials" / "weekly" / "snick" / "up-next"
        variant = self.media / "nickelodeon" / "specials" / "holidays" / "halloween" / "specials" / "weekly" / "snick" / "up-next"
        self.assertTrue(variant.is_dir())
        (weekly / "ordinary.png").write_bytes(b"ordinary")
        (variant / "halloween.png").write_bytes(b"halloween")
        service.scan_catalog()
        halloween = datetime.fromisoformat("2026-10-03T20:30:00-07:00")
        september = datetime.fromisoformat("2026-09-19T20:30:00-07:00")
        self.assertEqual(service.select("nickelodeon", "up-next", halloween), variant / "halloween.png")
        self.assertEqual(service.select("nickelodeon", "up-next", september), weekly / "ordinary.png")

    @patch("retrotv.graphics.subprocess.run")
    def test_placeholder_todo_disappears_when_exact_scope_has_user_art(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        todo = service.placeholder_todo()
        self.assertTrue(any(item["purpose"] == "interstitials" for item in todo))
        folder = self.media / "nickelodeon" / "graphics" / "interstitials"
        (folder / "replacement.png").write_bytes(b"replacement")
        service.scan_catalog()
        todo = service.placeholder_todo()
        self.assertFalse(
            any(
                item["purpose"] == "interstitials"
                and item["daypart_slug"] is None
                and item["holiday_slug"] is None
                for item in todo
            )
        )
        inventory = service.inventory()
        self.assertFalse(any(
            item["provenance"] == "placeholder"
            and item["purpose"] == "interstitials"
            and item["daypart_slug"] is None
            and item["holiday_slug"] is None
            for item in inventory
        ))
        self.assertTrue(any(
            item["provenance"] == "placeholder"
            and item["purpose"] == "interstitials"
            and item["holiday_slug"] == "halloween"
            for item in inventory
        ))

    @patch("retrotv.graphics.subprocess.run")
    def test_ai_graphic_is_cataloged_at_the_exact_weekly_holiday_scope(self, run):
        run.return_value.returncode = 1
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        folder = service.destination_folder(
            "nickelodeon", "up-next", holiday_slug="halloween",
            weekly_collection_slug="snick",
        )
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ai-example.png").write_bytes(b"ai")
        service.scan_catalog()
        row = self.connection.execute(
            """SELECT provenance,weekly_collection_slug,holiday_slug
               FROM graphic_assets WHERE relative_path LIKE '%ai-example.png'"""
        ).fetchone()
        self.assertEqual(tuple(row), ("ai", "snick", "halloween"))

        (folder / "ai-example.png").unlink()
        service.scan_catalog()
        inventory = service.inventory()
        self.assertTrue(any(
            item["provenance"] == "placeholder"
            and item["purpose"] == "interstitials"
            and item["daypart_slug"] is None
            and item["holiday_slug"] is None
            for item in inventory
        ))

    @patch("retrotv.graphics.subprocess.run")
    def test_music_uses_holiday_then_purpose_then_channel_root(self, run):
        run.return_value.returncode = 1
        run.return_value.stdout = "12"
        service = GraphicsService(self.connection, self.config)
        service.ensure_all()
        graphics = self.media / "nickelodeon" / "graphics"
        (graphics / "root.mp3").write_bytes(b"root")
        (graphics / "up-next" / "up.mp3").write_bytes(b"up")
        holiday = graphics / "holidays" / "halloween" / "up-next" / "spooky.mp3"
        holiday.write_bytes(b"spooky")
        service.scan_catalog()
        self.assertEqual(
            service.select_music(
                "nickelodeon", "up-next",
                datetime.fromisoformat("2026-10-15T12:00:00-07:00"),
            ).name,
            "spooky.mp3",
        )
        self.assertEqual(
            service.select_music(
                "nickelodeon", "up-next",
                datetime.fromisoformat("2026-09-15T12:00:00-07:00"),
            ).name,
            "up.mp3",
        )
        self.assertEqual(
            service.select_music(
                "nickelodeon", "interstitials",
                datetime.fromisoformat("2026-09-15T12:00:00-07:00"),
            ).name,
            "root.mp3",
        )
