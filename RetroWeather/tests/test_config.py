from __future__ import annotations

import json
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, MEDIA_DIRECTORIES, initialize, load_settings


class InitializationTests(WorkspaceTestCase):
    def test_initialization_creates_portable_layout(self) -> None:
        paths = AppPaths.discover(self.temporary)
        result = initialize(paths)

        self.assertTrue(result.settings_created)
        self.assertTrue(paths.settings.is_file())
        self.assertTrue(paths.raw_cache.is_dir())
        self.assertTrue(paths.normalized_cache.is_dir())
        self.assertTrue(paths.state.is_dir())
        self.assertTrue(paths.versions.is_dir())
        for relative in MEDIA_DIRECTORIES:
            self.assertTrue((paths.media / relative).is_dir(), relative)

        settings = load_settings(paths)
        self.assertEqual(settings["schema_version"], 1)
        self.assertEqual(settings["channel"]["slug"], "weather")
        self.assertEqual(settings["channel"]["duration_seconds"], 120)
        self.assertTrue(settings["music"]["enabled"])
        self.assertEqual(settings["music"]["volume"], 0.28)
        self.assertEqual(settings["appearance"]["safe_margin"], 28)
        self.assertEqual(settings["appearance"]["palette"]["accent"], "0x59c7e8")

    def test_every_condition_has_day_and_night_directories(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)

        conditions = (
            "default",
            "sunny",
            "partly-cloudy",
            "cloudy",
            "rainy",
            "stormy",
            "snowy",
            "foggy",
            "windy",
        )
        for condition in conditions:
            root = paths.media / "graphics" / "conditions" / condition
            self.assertTrue((root / "day").is_dir(), f"{condition}/day")
            self.assertTrue((root / "night").is_dir(), f"{condition}/night")

    def test_reinitialization_preserves_existing_settings(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        payload = json.loads(paths.settings.read_text(encoding="utf-8"))
        payload["channel"]["display_name"] = "Weather 42"
        paths.settings.write_text(json.dumps(payload), encoding="utf-8")

        result = initialize(paths)

        self.assertFalse(result.settings_created)
        self.assertEqual(load_settings(paths)["channel"]["display_name"], "Weather 42")

    def test_environment_can_select_portable_home(self) -> None:
        paths = AppPaths.discover(
            environ={"RETROWEATHER_HOME": str(self.temporary)}
        )
        self.assertEqual(paths.home, Path(self.temporary).resolve())


if __name__ == "__main__":
    import unittest

    unittest.main()
