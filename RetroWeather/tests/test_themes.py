from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from _support import WorkspaceTestCase
from retroweather.rendering.themes import (
    ThemeSelection,
    CLEAR_CODES,
    CLOUDY_CODES,
    FOG_CODES,
    KNOWN_WMO_CODES,
    PARTLY_CLOUDY_CODES,
    RAIN_CODES,
    SNOW_CODES,
    STORM_CODES,
    THEMES,
    build_background_catalog,
    classify_condition,
    current_theme,
    discover_backgrounds,
    select_background,
)
from retroweather.models import NormalizedWeatherSnapshot, WeatherAlert
from retroweather.providers.open_meteo_weather import WMO_CONDITIONS


FIXTURE = Path(__file__).parent / "fixtures" / "normalized-weather.json"


class WeatherThemeTests(WorkspaceTestCase):
    def test_condition_precedence_and_wind_thresholds(self) -> None:
        self.assertEqual(classify_condition(95, 40), "stormy")
        self.assertEqual(classify_condition(71, 40), "snowy")
        self.assertEqual(classify_condition(61, 40), "rainy")
        self.assertEqual(classify_condition(45, 40), "foggy")
        self.assertEqual(classify_condition(0, 26), "windy")
        self.assertEqual(classify_condition(2, 5), "partly-cloudy")
        self.assertEqual(classify_condition(0, 5), "sunny")
        self.assertEqual(classify_condition(999, 5), "default")

    def test_metric_wind_threshold_is_supported(self) -> None:
        self.assertEqual(classify_condition(0, 39, units="metric"), "sunny")
        self.assertEqual(classify_condition(0, 40, units="metric"), "windy")

    def test_every_supported_wmo_code_has_an_explicit_theme(self) -> None:
        self.assertEqual(KNOWN_WMO_CODES, frozenset(WMO_CONDITIONS))
        expected = {
            "sunny", "partly-cloudy", "cloudy", "foggy",
            "rainy", "snowy", "stormy",
        }
        self.assertTrue({classify_condition(code) for code in WMO_CONDITIONS} <= expected)
        for codes, theme in (
            (CLEAR_CODES, "sunny"),
            (PARTLY_CLOUDY_CODES, "partly-cloudy"),
            (CLOUDY_CODES, "cloudy"),
            (FOG_CODES, "foggy"),
            (RAIN_CODES, "rainy"),
            (SNOW_CODES, "snowy"),
            (STORM_CODES, "stormy"),
        ):
            for code in codes:
                self.assertEqual(classify_condition(code), theme, code)
        self.assertEqual(classify_condition(None), "default")
        self.assertEqual(classify_condition(999), "default")
        self.assertEqual(classify_condition(None, 30), "windy")
        self.assertEqual(classify_condition(None, visibility=0.5), "foggy")

    def test_reduced_visibility_overrides_wind_cloud_and_clear(self) -> None:
        self.assertEqual(classify_condition(0, 30, 0.9), "foggy")
        self.assertEqual(classify_condition(3, 30, 1.0), "foggy")
        self.assertEqual(classify_condition(0, 50, 1.6, units="metric"), "foggy")
        self.assertEqual(classify_condition(0, 50, 1.7, units="metric"), "windy")

    def test_active_severe_alert_has_highest_precedence(self) -> None:
        self.assertEqual(
            classify_condition(95, 80, 0.1, severe_alert=True),
            "alert",
        )
        snapshot = NormalizedWeatherSnapshot.from_dict(
            json.loads(FIXTURE.read_text(encoding="utf-8"))
        )
        alert = WeatherAlert(
            identifier="severe-fixture",
            severity="Severe",
            headline="Fixture warning",
            starts_at=datetime.fromisoformat("2026-08-06T14:00:00-07:00"),
            ends_at=datetime.fromisoformat("2026-08-06T15:00:00-07:00"),
            source="Fixture Weather Service",
        )
        snapshot = replace(snapshot, alerts=(alert,))
        self.assertEqual(current_theme(snapshot).family, "alert")

    def test_expired_or_moderate_alert_does_not_override_conditions(self) -> None:
        snapshot = NormalizedWeatherSnapshot.from_dict(
            json.loads(FIXTURE.read_text(encoding="utf-8"))
        )
        self.assertEqual(current_theme(snapshot).family, "sunny")

    def test_specific_artwork_falls_back_to_matching_default_variant(self) -> None:
        media = self.temporary / "media"
        default_night = media / "graphics" / "conditions" / "default" / "night"
        default_night.mkdir(parents=True)
        fallback = default_night / "fallback.JPG"
        fallback.write_bytes(b"fixture")

        found = discover_backgrounds(
            media,
            ThemeSelection("rainy", "night"),
            {"default/night": (fallback,)},
        )

        self.assertEqual(found, (fallback,))

    def test_asset_selection_is_stable_and_ignores_unsupported_files(self) -> None:
        directory = self.temporary / "media" / "graphics" / "conditions" / "sunny" / "day"
        directory.mkdir(parents=True)
        first = directory / "a.png"
        second = directory / "b.webp"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        (directory / "notes.txt").write_text("ignore", encoding="utf-8")
        theme = ThemeSelection("sunny", "day")

        catalog = {"sunny/day": (first, second)}
        selected = select_background(
            self.temporary / "media", theme, "stable-key", catalog
        )

        self.assertIn(selected, {first, second})
        self.assertEqual(
            selected,
            select_background(self.temporary / "media", theme, "stable-key", catalog),
        )

    def test_alert_artwork_uses_the_dedicated_alert_folder(self) -> None:
        directory = self.temporary / "media" / "graphics" / "alerts"
        directory.mkdir(parents=True)
        alert = directory / "warning.png"
        alert.write_bytes(b"fixture")

        self.assertEqual(
            discover_backgrounds(
                self.temporary / "media",
                ThemeSelection("alert", "night"),
                {"alert": (alert,)},
            ),
            (alert,),
        )

    def test_validated_catalog_includes_still_and_motion_but_not_invalid_files(self) -> None:
        media = self.temporary / "media"
        inventory = {
            "assets": [
                {
                    "relative_path": "graphics/conditions/rainy/day/rain.png",
                    "role": "condition_background", "media_type": "still", "eligible": True,
                },
                {
                    "relative_path": "graphics/conditions/rainy/day/rain.webm",
                    "role": "condition_background", "media_type": "motion", "eligible": True,
                },
                {
                    "relative_path": "graphics/conditions/rainy/day/broken.jpg",
                    "role": "condition_background", "media_type": "still", "eligible": False,
                },
            ]
        }

        with patch("retroweather.rendering.themes.scan_asset_library", return_value=inventory):
            catalog = build_background_catalog(media)

        self.assertEqual(
            tuple(path.name for path in catalog["rainy/day"]),
            ("rain.png", "rain.webm"),
        )

    def test_every_theme_supports_zero_one_and_multiple_assets(self) -> None:
        media = self.temporary / "media"
        for family in THEMES:
            for variant in ("day", "night"):
                theme = ThemeSelection(family, variant)
                first = media / f"{family}-{variant}.png"
                second = media / f"{family}-{variant}.mp4"
                self.assertEqual(discover_backgrounds(media, theme, {}), ())
                self.assertEqual(
                    discover_backgrounds(media, theme, {theme.key: (first,)}),
                    (first,),
                )
                self.assertEqual(
                    discover_backgrounds(media, theme, {theme.key: (first, second)}),
                    (first, second),
                )
