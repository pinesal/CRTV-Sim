from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, DEFAULT_SETTINGS, initialize
from retroweather.models import NormalizedWeatherSnapshot, WeatherAlert
from retroweather.planning import SegmentCue, build_forecast_plan
from retroweather.rendering import (
    RenderError,
    build_screens,
    render_and_publish,
    resolve_screen_backgrounds,
)
from retroweather.rendering.static import (
    _background_filters,
    _probe_video,
    _render_segment,
    _temperature_badge,
    _validate_probe,
)
from retroweather.rendering.design import appearance_settings, fit_text
from retroweather.rendering.seasonal import (
    CHRISTMAS_PALETTE,
    HALLOWEEN_PALETTE,
    active_season,
)


FIXTURE = Path(__file__).parent / "fixtures" / "normalized-weather.json"


class StaticRenderingTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.snapshot = NormalizedWeatherSnapshot.from_dict(
            json.loads(FIXTURE.read_text(encoding="utf-8"))
        )

    def test_seven_distinct_forecast_screens_are_built(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)

        screens = build_screens(self.snapshot, plan)

        self.assertEqual(len(screens), 7)
        self.assertEqual(screens[0].title, "RETROWEATHER")
        self.assertEqual(screens[1].title, "CURRENT CONDITIONS")
        self.assertEqual(screens[5].title, "EXTENDED FORECAST")
        self.assertEqual(screens[1].theme.key, "sunny/day")
        self.assertEqual(screens[4].theme.variant, "night")
        self.assertEqual(screens[5].theme.key, "default/day")
        self.assertTrue(any("78°F" in line.text for line in screens[1].lines))

    def test_severe_alert_overrides_each_overlapping_forecast_period(self) -> None:
        alert = WeatherAlert(
            identifier="severe-fixture",
            severity="extreme",
            headline="Fixture warning",
            starts_at=datetime.fromisoformat("2026-08-06T14:00:00-07:00"),
            ends_at=datetime.fromisoformat("2026-08-06T21:00:00-07:00"),
            source="Fixture Weather Service",
        )
        snapshot = replace(self.snapshot, alerts=(alert,))
        screens = build_screens(snapshot, build_forecast_plan(snapshot, DEFAULT_SETTINGS))

        self.assertEqual(screens[0].theme.key, "default/day")
        self.assertEqual(screens[1].theme.key, "alert/day")
        self.assertEqual(screens[2].theme.key, "alert/day")
        self.assertEqual(screens[3].theme.key, "alert/day")
        self.assertEqual(screens[4].theme.key, "alert/night")
        self.assertEqual(screens[5].theme.key, "default/day")

    def test_custom_background_has_no_full_screen_darkening_wash(self) -> None:
        screens = build_screens(
            self.snapshot,
            build_forecast_plan(self.snapshot, DEFAULT_SETTINGS),
        )
        screen = replace(
            screens[1],
            background_asset=self.temporary / "background.png",
        )

        filters = _background_filters(screen)

        self.assertIn("scale=640:480:force_original_aspect_ratio=increase", filters)
        self.assertIn(
            "drawbox=x=28:y=104:w=300:h=232:color=0x071827@0.58:t=fill",
            filters,
        )
        self.assertIn(
            "drawbox=x=344:y=104:w=268:h=232:color=0x102c44@0.58:t=fill",
            filters,
        )
        self.assertFalse(any("x=20:y=92:w=600:h=332" in item for item in filters))
        self.assertFalse(any("w=iw:h=ih" in item for item in filters))

    def test_custom_brand_palette_and_accessibility_bounds_are_applied(self) -> None:
        settings = {
            "channel": {"display_name": "Neighborhood Weather Network"},
            "appearance": {
                "panel_opacity": 2,
                "safe_margin": 2,
                "text_scale": 3,
                "palette": {"accent": "#ff8800", "primary": "invalid"},
            },
        }

        appearance = appearance_settings(settings)
        screen = build_screens(
            self.snapshot, build_forecast_plan(self.snapshot, DEFAULT_SETTINGS), settings
        )[0]

        self.assertEqual(screen.title, "NEIGHBORHOOD WEATHER NETWORK")
        self.assertEqual(screen.accent, "0xff8800")
        self.assertEqual(screen.palette.primary, "0xf8fbff")
        self.assertEqual(appearance.panel_opacity, 0.90)
        self.assertEqual(appearance.safe_margin, 20)
        self.assertEqual(appearance.text_scale, 1.15)

    def test_halloween_palette_is_active_only_during_october(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        october = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-10-15T18:00:00-07:00"),
        )
        november = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-11-01T00:00:00-07:00"),
        )

        self.assertEqual(active_season(datetime.fromisoformat("2026-10-01T00:00:00-07:00")), "halloween")
        self.assertEqual(active_season(datetime.fromisoformat("2026-10-31T23:59:59-07:00")), "halloween")
        self.assertIsNone(active_season(datetime.fromisoformat("2026-11-01T00:00:00-07:00")))
        self.assertTrue(all(screen.season == "halloween" for screen in october))
        self.assertTrue(all(screen.palette == HALLOWEEN_PALETTE for screen in october))
        self.assertNotEqual(november[0].palette, HALLOWEEN_PALETTE)

    def test_christmas_palette_is_active_december_first_through_twenty_fifth(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        christmas = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-12-25T23:59:59-08:00"),
        )
        december_twenty_six = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-12-26T00:00:00-08:00"),
        )

        self.assertEqual(active_season(datetime.fromisoformat("2026-12-01T00:00:00-08:00")), "christmas")
        self.assertEqual(active_season(datetime.fromisoformat("2026-12-25T23:59:59-08:00")), "christmas")
        self.assertIsNone(active_season(datetime.fromisoformat("2026-12-26T00:00:00-08:00")))
        self.assertTrue(all(screen.season == "christmas" for screen in christmas))
        self.assertTrue(all(screen.palette == CHRISTMAS_PALETTE for screen in christmas))
        self.assertNotEqual(december_twenty_six[0].palette, CHRISTMAS_PALETTE)

    def test_halloween_overlay_is_selected_once_for_every_screen(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screens = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-10-15T18:00:00-07:00"),
        )
        overlay = self.temporary / "media/graphics/holidays/halloween/overlays/border.png"
        inventory = {
            "assets": [
                {
                    "relative_path": "graphics/holidays/halloween/overlays/border.png",
                    "role": "holiday_halloween_artwork",
                    "media_type": "still",
                    "valid": True,
                    "has_alpha": True,
                }
            ]
        }

        with patch("retroweather.rendering.seasonal.scan_asset_library", return_value=inventory):
            resolved = resolve_screen_backgrounds(
                screens, self.temporary / "media", "october-test"
            )

        self.assertTrue(all(screen.holiday_overlay == overlay for screen in resolved))

    def test_missing_halloween_overlay_keeps_palette_and_renders_normally(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screens = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-10-15T18:00:00-07:00"),
        )
        with patch(
            "retroweather.rendering.seasonal.scan_asset_library",
            return_value={"assets": []},
        ):
            resolved = resolve_screen_backgrounds(
                screens, self.temporary / "media", "missing"
            )

        self.assertTrue(all(screen.holiday_overlay is None for screen in resolved))
        self.assertTrue(all(screen.palette == HALLOWEEN_PALETTE for screen in resolved))

    def test_christmas_overlay_is_selected_for_every_screen_with_safe_fallback(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screens = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-12-20T18:00:00-08:00"),
        )
        relative = "graphics/holidays/christmas/overlays/garland.png"
        inventory = {
            "assets": [
                {
                    "relative_path": relative,
                    "role": "holiday_christmas_artwork",
                    "media_type": "still",
                    "valid": True,
                    "has_alpha": True,
                }
            ]
        }
        with patch("retroweather.rendering.seasonal.scan_asset_library", return_value=inventory):
            resolved = resolve_screen_backgrounds(
                screens, self.temporary / "media", "christmas-test"
            )
        expected = self.temporary / "media" / Path(relative)
        self.assertTrue(all(screen.holiday_overlay == expected for screen in resolved))

        with patch(
            "retroweather.rendering.seasonal.scan_asset_library",
            return_value={"assets": []},
        ):
            fallback = resolve_screen_backgrounds(
                screens, self.temporary / "media", "christmas-missing"
            )
        self.assertTrue(all(screen.holiday_overlay is None for screen in fallback))
        self.assertTrue(all(screen.palette == CHRISTMAS_PALETTE for screen in fallback))

    def test_halloween_overlay_is_composited_before_panels_and_text(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screen = build_screens(
            self.snapshot,
            plan,
            DEFAULT_SETTINGS,
            now=datetime.fromisoformat("2026-10-15T18:00:00-07:00"),
        )[1]
        screen = replace(
            screen,
            background_asset=self.temporary / "weather.png",
            holiday_overlay=self.temporary / "border.png",
        )
        commands: list[list[str]] = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        _render_segment(
            self.temporary / "halloween.mp4",
            plan.segments[1],
            screen,
            self.temporary,
            22,
            runner,
        )

        command = commands[0]
        graph = (self.temporary / "segment-22.filter").read_text(encoding="utf-8")
        self.assertEqual(
            command[command.index("-/filter_complex") + 1],
            str(self.temporary / "segment-22.filter"),
        )
        self.assertIn(str(screen.holiday_overlay), command)
        self.assertEqual(command[command.index("-map") + 1], "[vout]")
        self.assertLess(graph.index("[weather][holiday]overlay"), graph.index("drawbox="))
        self.assertLess(graph.index("drawbox="), graph.index("drawtext="))

    def test_plain_screen_loads_filter_file_with_current_ffmpeg_syntax(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screen = build_screens(self.snapshot, plan)[1]
        commands: list[list[str]] = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        _render_segment(
            self.temporary / "plain.mp4", plan.segments[1], screen,
            self.temporary, 23, runner,
        )

        command = commands[0]
        graph_path = self.temporary / "segment-23.filter"
        self.assertEqual(command[command.index("-/filter:v") + 1], str(graph_path))
        self.assertIn("drawtext=", graph_path.read_text(encoding="utf-8"))
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")
        self.assertIn("1:a:0", command)

    def test_long_text_shrinks_then_ellipsizes_inside_safe_width(self) -> None:
        text, size = fit_text(
            "A VERY LONG WEATHER CONDITION NAME THAT CANNOT POSSIBLY FIT",
            28,
            170,
            minimum_size=12,
            bold=True,
        )

        self.assertEqual(size, 12)
        self.assertTrue(text.endswith("…"))
        self.assertLess(len(text), 30)

    def test_motion_background_is_looped_trimmed_and_not_tuned_as_a_still(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screen = replace(
            build_screens(self.snapshot, plan)[1],
            background_asset=self.temporary / "background.webm",
        )
        commands: list[list[str]] = []

        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        _render_segment(
            self.temporary / "segment.mp4",
            plan.segments[1],
            screen,
            self.temporary,
            1,
            runner,
        )

        command = commands[0]
        self.assertIn("-stream_loop", command)
        self.assertIn("-1", command)
        self.assertNotIn("stillimage", command)
        self.assertEqual(command[command.index("-t") + 1], str(plan.segments[1].duration_seconds))
        filters = (self.temporary / "segment-01.filter").read_text(encoding="utf-8")
        self.assertIn("scale=640:480:force_original_aspect_ratio=increase", filters)
        self.assertIn("crop=640:480", filters)
        self.assertIn("fps=30", filters)

    def test_real_motion_background_loops_and_renders_to_exact_cue(self) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg and FFprobe are required for motion integration")
        source = self.temporary / "motion.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=s=320x240:r=12",
                "-t", "0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ],
            check=True,
        )
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)
        screen = replace(build_screens(self.snapshot, plan)[1], background_asset=source)
        cue = SegmentCue("motion-test", "Motion Test", 0.0, 0.75)
        output = self.temporary / "motion-segment.mp4"

        _render_segment(output, cue, screen, self.temporary, 8, subprocess.run)

        self.assertTrue(output.is_file())
        self.assertAlmostEqual(_validate_probe(_probe_video(output, subprocess.run), 0.75), 0.75, delta=0.15)

    def test_temperature_extremes_add_badges_without_changing_theme(self) -> None:
        hot = replace(
            self.snapshot,
            current=replace(self.snapshot.current, temperature=105.0),
        )
        hot_screen = build_screens(hot, build_forecast_plan(hot, DEFAULT_SETTINGS))[1]
        self.assertEqual(hot_screen.theme.key, "sunny/day")
        self.assertEqual(hot_screen.badge.text, "EXTREME HEAT")

        cold_today = replace(self.snapshot.daily[0], low_temperature=20.0)
        cold = replace(self.snapshot, daily=(cold_today, *self.snapshot.daily[1:]))
        cold_screen = build_screens(cold, build_forecast_plan(cold, DEFAULT_SETTINGS))[2]
        self.assertEqual(cold_screen.theme.key, "sunny/day")
        self.assertEqual(cold_screen.badge.text, "FREEZING")
        self.assertEqual(_temperature_badge((38.0,), "°C").text, "EXTREME HEAT")
        self.assertEqual(_temperature_badge((0.0,), "°C").text, "FREEZING")

    def test_probe_validation_requires_expected_playback_contract(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 640,
                    "height": 480,
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "30/1",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
            "format": {"duration": "120.021"},
        }

        self.assertAlmostEqual(_validate_probe(probe, 120), 120.021)
        probe["streams"][0]["width"] = 1280
        with self.assertRaisesRegex(RenderError, "exactly 640x480"):
            _validate_probe(probe, 120)

    def test_probe_validation_rejects_wrong_frame_rate(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264", "width": 640,
                    "height": 480, "pix_fmt": "yuv420p", "avg_frame_rate": "25/1",
                },
                {
                    "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
                    "channels": 2,
                },
            ],
            "format": {"duration": "120.021"},
        }

        with self.assertRaisesRegex(RenderError, "must be 30 fps"):
            _validate_probe(probe, 120)

    def test_render_requires_weather_snapshot(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)

        with self.assertRaisesRegex(RenderError, "Fetch weather"):
            render_and_publish(paths)

    def test_deliberate_late_render_failure_preserves_active_pointer(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        self.snapshot.save(paths.latest_snapshot)
        active = paths.versions / "previous-valid"
        active.mkdir(parents=True)
        (active / "keep.txt").write_text("active", encoding="utf-8")
        pointer = paths.output / "current.json"
        pointer.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "previous-valid",
                    "manifest": "versions/previous-valid/channel.json",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        before = pointer.read_bytes()
        valid_probe = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264", "width": 640,
                    "height": 480, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
                },
                {
                    "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
                    "channels": 2,
                },
            ],
            "format": {"duration": "120.021"},
        }

        def fake_segment(output: Path, *args, **kwargs) -> None:
            output.write_bytes(b"segment")

        def fake_concat(segments, output: Path, *args, **kwargs) -> None:
            output.write_bytes(b"video")

        with (
            patch("retroweather.rendering.static._render_segment", side_effect=fake_segment),
            patch("retroweather.rendering.static._concatenate_segments", side_effect=fake_concat),
            patch("retroweather.rendering.static._probe_video", return_value=valid_probe),
            patch("retroweather.rendering.static._validate_seeking"),
            patch(
                "retroweather.rendering.static.validate_version_directory",
                side_effect=RenderError("deliberate validation failure"),
            ),
        ):
            with self.assertRaisesRegex(RenderError, "deliberate validation failure"):
                render_and_publish(
                    paths,
                    now=datetime.fromisoformat("2026-08-06T15:00:00-07:00"),
                )

        self.assertEqual(pointer.read_bytes(), before)
        self.assertTrue((active / "keep.txt").is_file())
        self.assertEqual(
            [item.name for item in paths.versions.iterdir()],
            ["previous-valid"],
        )
        self.assertFalse((paths.state / "render.lock").exists())
