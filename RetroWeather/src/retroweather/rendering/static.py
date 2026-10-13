from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from retroweather.config import AppPaths, load_settings
from retroweather.models import DailyForecast, NormalizedWeatherSnapshot
from retroweather.planning import ForecastPlan, SegmentCue, build_forecast_plan
from retroweather.publication import ManifestValidationError, validate_version_manifest
from retroweather.providers.open_meteo_weather import condition_text
from retroweather.rendering.themes import (
    MOTION_EXTENSIONS,
    ThemeSelection,
    build_background_catalog,
    classify_condition,
    current_theme,
    daily_theme,
    has_active_severe_alert,
    hourly_theme,
    select_background,
)
from retroweather.rendering.music import (
    MusicAsset,
    music_settings,
    select_music,
    soundtrack_filter,
)
from retroweather.rendering.seasonal import seasonal_appearance, select_holiday_overlay
from retroweather.rendering.design import (
    Appearance,
    DEFAULT_PALETTE,
    DesignPalette,
    appearance_settings,
    fit_text,
)


VIDEO_NAME = "weather-channel.mp4"
MANIFEST_NAME = "channel.json"
SNAPSHOT_NAME = "normalized-weather.json"
RENDER_METADATA_NAME = "render-metadata.json"
POINTER_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ProgressCallback = Callable[[str, int], None]


class RenderError(RuntimeError):
    """Raised when a forecast loop cannot be rendered or validated."""


@dataclass(frozen=True)
class RenderResult:
    generation_id: str
    version_directory: Path
    video: Path
    manifest: Path
    duration_seconds: float


@dataclass(frozen=True)
class ScreenLine:
    text: str
    x: int
    y: int
    size: int = 24
    color: str = "white"
    bold: bool = False
    max_width: int | None = None
    align: str = "left"


@dataclass(frozen=True)
class TemperatureBadge:
    text: str
    color: str


@dataclass(frozen=True)
class Screen:
    title: str
    subtitle: str
    lines: tuple[ScreenLine, ...]
    background: str
    accent: str
    theme: ThemeSelection = ThemeSelection("default", "day")
    background_asset: Path | None = None
    badge: TemperatureBadge | None = None
    layout: str = "standard"
    palette: DesignPalette = DEFAULT_PALETTE
    appearance: Appearance | None = None
    season: str | None = None
    holiday_overlay: Path | None = None


BACKGROUND_COLORS = (
    "0x10243b",
    "0x123457",
    "0x15304b",
    "0x173b55",
    "0x192f49",
    "0x1d3850",
    "0x10243b",
)


def render_and_publish(
    paths: AppPaths,
    *,
    runner: CommandRunner = subprocess.run,
    now: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> RenderResult:
    with _render_lock(paths):
        return _render_and_publish_locked(
            paths, runner=runner, now=now, progress=progress
        )


def _render_and_publish_locked(
    paths: AppPaths,
    *,
    runner: CommandRunner,
    now: datetime | None,
    progress: ProgressCallback | None,
) -> RenderResult:
    _notify_progress(progress, "Loading weather and settings", 2)
    settings = load_settings(paths)
    if not paths.latest_snapshot.is_file():
        raise RenderError("No weather snapshot is available. Fetch weather before rendering.")
    snapshot = NormalizedWeatherSnapshot.load(paths.latest_snapshot)
    plan = build_forecast_plan(snapshot, settings)
    _notify_progress(progress, "Planning forecast screens", 6)
    local_now = now or datetime.now(ZoneInfo(snapshot.location.timezone))
    if local_now.tzinfo is None or local_now.utcoffset() is None:
        raise RenderError("Render time must include a timezone offset.")
    generation_id = local_now.strftime("%Y-%m-%dT%H%M%S%z")
    final_directory = paths.versions / generation_id
    if final_directory.exists():
        generation_id = f"{generation_id}-{uuid.uuid4().hex[:6]}"
        final_directory = paths.versions / generation_id
    building = paths.versions / f".{generation_id}.building"
    building.mkdir(parents=True, exist_ok=False)
    try:
        screens = resolve_screen_backgrounds(
            build_screens(snapshot, plan, settings, now=local_now), paths.media, generation_id
        )
        selected_music_settings = music_settings(settings.get("music"))
        selected_music = (
            select_music(
                paths.media,
                (screen.theme.family for screen in screens),
                generation_id,
            )
            if selected_music_settings["enabled"]
            else None
        )
        segment_files: list[Path] = []
        for index, (cue, screen) in enumerate(zip(plan.segments, screens, strict=True)):
            _notify_progress(
                progress,
                f"Rendering {cue.title}",
                10 + index * 8,
            )
            segment = building / f"segment-{index:02d}.mp4"
            _render_segment(segment, cue, screen, building, index, runner)
            segment_files.append(segment)
        _notify_progress(progress, "Joining forecast segments", 66)
        joined_video = building / "weather-channel-silent.mp4"
        _concatenate_segments(segment_files, joined_video, building, runner)
        _notify_progress(progress, "Mixing loop soundtrack", 71)
        video = building / VIDEO_NAME
        loop_strategy = _apply_soundtrack(
            joined_video,
            video,
            selected_music,
            plan.duration_seconds,
            selected_music_settings,
            runner,
        )
        _notify_progress(progress, "Checking video and audio", 76)
        probe = _probe_video(video, runner)
        validated_duration = _validate_probe(probe, plan.duration_seconds)
        _notify_progress(progress, "Checking video seeking", 81)
        _validate_seeking(video, plan.duration_seconds, runner)

        _notify_progress(progress, "Writing portable channel version", 85)
        snapshot.save(building / SNAPSHOT_NAME)
        manifest_payload = _manifest(
            snapshot, plan, screens, settings, generation_id, local_now, validated_duration
        )
        _write_json(building / MANIFEST_NAME, manifest_payload)
        _write_json(
            building / RENDER_METADATA_NAME,
            {
                "schema_version": 1,
                "music": {
                    "enabled": bool(selected_music_settings["enabled"]),
                    "source": selected_music.source if selected_music else "silence",
                    "file": selected_music.path.name if selected_music else None,
                    "volume": selected_music_settings["volume"],
                    "target_lufs": selected_music_settings["target_lufs"],
                    "loop_strategy": loop_strategy,
                },
                "seasonal": {
                    "theme": screens[0].season,
                    "overlay": screens[0].holiday_overlay.name
                    if screens[0].holiday_overlay
                    else None,
                },
            },
        )
        for segment in segment_files:
            segment.unlink(missing_ok=True)
        for temporary in building.glob("segment-*.filter"):
            temporary.unlink(missing_ok=True)
        for temporary in building.glob("segment-*.txt"):
            temporary.unlink(missing_ok=True)
        (building / "concat.txt").unlink(missing_ok=True)

        # Re-read and independently validate the complete portable version.
        # The active pointer is not touched unless this acceptance gate passes.
        _notify_progress(progress, "Validating complete publication", 89)
        validated_duration = validate_version_directory(
            building,
            runner=runner,
            expected_generation_id=generation_id,
        )

        _notify_progress(progress, "Publishing validated generation", 97)
        os.replace(building, final_directory)
        pointer = {
            "schema_version": POINTER_SCHEMA_VERSION,
            "generation_id": generation_id,
            "manifest": f"versions/{generation_id}/{MANIFEST_NAME}",
        }
        try:
            _write_json_atomic(paths.output / "current.json", pointer)
        except Exception:
            # A version is not active until the pointer swap succeeds. Remove
            # an orphaned final directory so a full/interrupted output volume
            # cannot accumulate apparently publishable generations.
            shutil.rmtree(final_directory, ignore_errors=True)
            raise
        _prune_versions(paths, settings, keep=generation_id)
        _notify_progress(progress, "Publication complete", 100)
        return RenderResult(
            generation_id=generation_id,
            version_directory=final_directory,
            video=final_directory / VIDEO_NAME,
            manifest=final_directory / MANIFEST_NAME,
            duration_seconds=validated_duration,
        )
    except Exception:
        shutil.rmtree(building, ignore_errors=True)
        raise


def build_screens(
    snapshot: NormalizedWeatherSnapshot,
    plan: ForecastPlan,
    settings: Mapping[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[Screen, ...]:
    if len(plan.segments) != 7:
        raise RenderError("The Step 8 static renderer requires the seven-segment base plan.")
    unit = "°F" if snapshot.units == "imperial" else "°C"
    wind_unit = "mph" if snapshot.units == "imperial" else "km/h"
    today = snapshot.daily[0] if snapshot.daily else None
    tomorrow = snapshot.daily[1] if len(snapshot.daily) > 1 else None
    location = snapshot.location.display_name
    current = snapshot.current
    appearance, season = seasonal_appearance(settings, now or snapshot.current.observed_at)
    palette = appearance.palette
    channel_name = appearance.channel_name.upper()
    current_selection = current_theme(snapshot)
    hourly_selection = hourly_theme(snapshot.hourly[:6], snapshot.units, snapshot.alerts)
    night_period = next((period for period in snapshot.hourly if not period.is_day), None)
    night_selection = ThemeSelection(
        classify_condition(
            night_period.condition_code if night_period else None,
            night_period.wind_speed if night_period else None,
            units=snapshot.units,
            severe_alert=bool(
                night_period
                and has_active_severe_alert(snapshot.alerts, night_period.starts_at)
            ),
        ),
        "night",
    )

    screens = [
        Screen(
            channel_name,
            "LOCAL WEATHER • AROUND THE CLOCK",
            (
                ScreenLine(location, 320, 216, 25, palette.primary, True, 500, "center"),
                ScreenLine(
                    "YOUR TWO-MINUTE FORECAST STARTS NOW", 320, 274, 17,
                    palette.secondary, False, 500, "center"
                ),
            ),
            BACKGROUND_COLORS[0],
            palette.accent,
            ThemeSelection("default", current_selection.variant),
            layout="station",
            palette=palette,
            appearance=appearance,
            season=season,
        ),
        Screen(
            "CURRENT CONDITIONS",
            location.upper(),
            (
                ScreenLine(_temperature(current.temperature, unit), 48, 132, 70, palette.primary, True, 260),
                ScreenLine(current.condition_text.upper(), 48, 228, 27, palette.condition, True, 260),
                ScreenLine(f"FEELS LIKE  {_temperature(current.apparent_temperature, unit)}", 365, 132, 20, palette.primary, False, 222),
                ScreenLine(f"HUMIDITY   {_number(current.humidity, '%')}", 365, 176, 20, palette.primary, False, 222),
                ScreenLine(f"WIND       {_number(current.wind_speed, ' ' + wind_unit, 1)}", 365, 220, 20, palette.primary, False, 222),
                ScreenLine(f"PRESSURE   {_number(current.pressure, ' hPa', 0)}", 365, 264, 20, palette.primary, False, 222),
                ScreenLine(f"OBSERVED {_clock_time(current.observed_at)}", 44, 373, 17, palette.secondary, False, 540),
            ),
            BACKGROUND_COLORS[1],
            palette.condition,
            current_selection,
            badge=_temperature_badge((current.temperature,), unit),
            layout="current",
            palette=palette,
            appearance=appearance,
            season=season,
        ),
        _daily_screen(
            "LOCAL FORECAST", "TODAY", location, today, unit, BACKGROUND_COLORS[2],
            daily_theme(today, alerts=snapshot.alerts), appearance, season
        ),
        _hourly_screen(snapshot, unit, wind_unit, hourly_selection, appearance, season),
        _tonight_tomorrow_screen(snapshot, tomorrow, unit, night_selection, appearance, season),
        _extended_screen(snapshot, unit, appearance, season),
        Screen(
            channel_name,
            "THE LOCAL FORECAST CONTINUES",
            (
                ScreenLine(location, 320, 205, 25, palette.primary, True, 500, "center"),
                ScreenLine("WEATHER DATA BY OPEN-METEO", 320, 262, 17, palette.secondary, False, 500, "center"),
                ScreenLine("STAY TUNED • NEXT UPDATE SOON", 320, 309, 18, palette.condition, True, 500, "center"),
            ),
            BACKGROUND_COLORS[6],
            palette.accent,
            ThemeSelection("default", current_selection.variant),
            layout="station",
            palette=palette,
            appearance=appearance,
            season=season,
        ),
    ]
    return tuple(screens)


def _daily_screen(
    title: str,
    label: str,
    location: str,
    forecast: DailyForecast | None,
    unit: str,
    background: str,
    theme: ThemeSelection,
    appearance: Appearance,
    season: str | None,
) -> Screen:
    palette = appearance.palette
    if forecast is None:
        lines = (ScreenLine("FORECAST UNAVAILABLE", 48, 196, 30, "0xffe1a6", True, 540),)
    else:
        lines = (
            ScreenLine(condition_text(forecast.condition_code).upper(), 48, 131, 30, palette.condition, True, 540),
            ScreenLine("HIGH", 116, 218, 17, palette.secondary, True, 120, "center"),
            ScreenLine(_temperature(forecast.high_temperature, unit), 116, 253, 45, palette.primary, True, 130, "center"),
            ScreenLine("LOW", 312, 218, 17, palette.secondary, True, 120, "center"),
            ScreenLine(_temperature(forecast.low_temperature, unit), 312, 253, 45, palette.primary, True, 130, "center"),
            ScreenLine("RAIN", 508, 218, 17, palette.secondary, True, 120, "center"),
            ScreenLine(_number(forecast.precipitation_probability, "%"), 508, 253, 45, palette.primary, True, 130, "center"),
            ScreenLine(_sun_times(forecast), 44, 373, 17, palette.secondary, False, 540),
        )
    temperatures = (
        (forecast.high_temperature, forecast.low_temperature) if forecast else ()
    )
    return Screen(
        title,
        f"{label} • {location.upper()}",
        lines,
        background,
        palette.condition,
        theme,
        badge=_temperature_badge(temperatures, unit),
        layout="daily",
        palette=palette,
        appearance=appearance,
        season=season,
    )


def _hourly_screen(
    snapshot: NormalizedWeatherSnapshot,
    unit: str,
    wind_unit: str,
    theme: ThemeSelection,
    appearance: Appearance,
    season: str | None,
) -> Screen:
    palette = appearance.palette
    lines: list[ScreenLine] = [
        ScreenLine("TIME", 44, 112, 16, palette.secondary, True, 110),
        ScreenLine("TEMP", 190, 112, 16, palette.secondary, True, 110),
        ScreenLine("RAIN", 338, 112, 16, palette.secondary, True, 100),
        ScreenLine("WIND", 470, 112, 16, palette.secondary, True, 126),
    ]
    for row, period in enumerate(snapshot.hourly[:6]):
        y = 151 + row * 48
        lines.extend(
            (
                ScreenLine(_clock_time(period.starts_at, include_minutes=False), 44, y, 21, palette.primary, True, 110),
                ScreenLine(_temperature(period.temperature, unit), 190, y, 21, palette.primary, False, 110),
                ScreenLine(_number(period.precipitation_probability, "%"), 338, y, 21, palette.primary, False, 100),
                ScreenLine(_number(period.wind_speed, " " + wind_unit, 1), 470, y, 19, palette.primary, False, 126),
            )
        )
    return Screen(
        "HOURLY OUTLOOK",
        snapshot.location.display_name.upper(),
        tuple(lines),
        BACKGROUND_COLORS[3],
        palette.accent,
        theme,
        badge=_temperature_badge(
            (period.temperature for period in snapshot.hourly[:6]), unit
        ),
        layout="hourly",
        palette=palette,
        appearance=appearance,
        season=season,
    )


def _tonight_tomorrow_screen(
    snapshot: NormalizedWeatherSnapshot,
    tomorrow: DailyForecast | None,
    unit: str,
    theme: ThemeSelection,
    appearance: Appearance,
    season: str | None,
) -> Screen:
    palette = appearance.palette
    night = next((period for period in snapshot.hourly if not period.is_day), None)
    lines = [
        ScreenLine("TONIGHT", 48, 124, 20, palette.secondary, True, 230),
        ScreenLine(
            condition_text(night.condition_code).upper() if night else "FORECAST UNAVAILABLE",
            48,
            164,
            25,
            palette.primary,
            True,
            230,
        ),
        ScreenLine(
            f"TEMP  {_temperature(night.temperature if night else None, unit)}",
            48,
            215,
            30,
            palette.condition,
            True,
            230,
        ),
        ScreenLine("TOMORROW", 352, 124, 20, palette.secondary, True, 230),
    ]
    if tomorrow:
        lines.extend(
            (
                ScreenLine(condition_text(tomorrow.condition_code).upper(), 352, 164, 23, palette.primary, True, 230),
                ScreenLine(
                    f"HIGH {_temperature(tomorrow.high_temperature, unit)}  LOW {_temperature(tomorrow.low_temperature, unit)}",
                    352,
                    215,
                    24,
                    palette.condition,
                    True,
                    230,
                ),
                ScreenLine(
                    f"RAIN CHANCE {_number(tomorrow.precipitation_probability, '%')}",
                    352,
                    265,
                    19,
                    palette.primary,
                    False,
                    230,
                ),
            )
        )
    return Screen(
        "TONIGHT / TOMORROW",
        snapshot.location.display_name.upper(),
        tuple(lines),
        BACKGROUND_COLORS[4],
        palette.condition,
        theme,
        badge=_temperature_badge(
            (
                night.temperature if night else None,
                tomorrow.high_temperature if tomorrow else None,
                tomorrow.low_temperature if tomorrow else None,
            ),
            unit,
        ),
        layout="tonight",
        palette=palette,
        appearance=appearance,
        season=season,
    )


def _extended_screen(
    snapshot: NormalizedWeatherSnapshot,
    unit: str,
    appearance: Appearance,
    season: str | None,
) -> Screen:
    palette = appearance.palette
    lines: list[ScreenLine] = []
    for column, forecast in enumerate(snapshot.daily[:5]):
        x = 20 + column * 120
        lines.extend(
            (
                ScreenLine(forecast.date.strftime("%a").upper(), x + 60, 119, 19, palette.secondary, True, 104, "center"),
                ScreenLine(condition_text(forecast.condition_code).upper(), x + 60, 166, 15, palette.primary, True, 104, "center"),
                ScreenLine("HIGH", x + 60, 221, 12, palette.muted, True, 104, "center"),
                ScreenLine(_temperature(forecast.high_temperature, unit), x + 60, 242, 31, palette.primary, True, 104, "center"),
                ScreenLine("LOW", x + 60, 290, 12, palette.muted, True, 104, "center"),
                ScreenLine(_temperature(forecast.low_temperature, unit), x + 60, 311, 24, palette.secondary, False, 104, "center"),
                ScreenLine(_number(forecast.precipitation_probability, "%"), x + 60, 365, 18, palette.accent, True, 104, "center"),
            )
        )
    return Screen(
        "EXTENDED FORECAST",
        "HIGH • LOW • RAIN CHANCE",
        tuple(lines),
        BACKGROUND_COLORS[5],
        palette.accent,
        ThemeSelection("default", "day"),
        badge=_temperature_badge(
            (
                value
                for forecast in snapshot.daily[:5]
                for value in (forecast.high_temperature, forecast.low_temperature)
            ),
            unit,
        ),
        layout="extended",
        palette=palette,
        appearance=appearance,
        season=season,
    )


def resolve_screen_backgrounds(
    screens: Sequence[Screen], media_root: Path, generation_id: str
) -> tuple[Screen, ...]:
    catalog = build_background_catalog(media_root)
    season = screens[0].season if screens else None
    holiday_overlay = select_holiday_overlay(media_root, season, generation_id)
    return tuple(
        replace(
            screen,
            background_asset=select_background(
                media_root,
                screen.theme,
                f"{generation_id}:{index}:{screen.title}",
                catalog,
            ),
            holiday_overlay=holiday_overlay,
        )
        for index, screen in enumerate(screens)
    )


def _render_segment(
    output: Path,
    cue: SegmentCue,
    screen: Screen,
    directory: Path,
    index: int,
    runner: CommandRunner,
) -> None:
    regular, bold = _font_paths()
    preparation_filters = _background_preparation_filters(screen)
    filters = _interface_filters(screen)
    palette = screen.palette
    appearance = screen.appearance or appearance_settings(None)
    title_width = 410 if screen.badge else 570
    title_lines = (
        ScreenLine(screen.title, 28, 14, 28, palette.primary, True, title_width),
        ScreenLine(screen.subtitle, 612, 49, 15, palette.secondary, False, 570, "right"),
    )
    badge_lines = (
        (ScreenLine(screen.badge.text, 536, 22, 14, palette.primary, True, 126, "center"),)
        if screen.badge else ()
    )
    for line_index, line in enumerate((*title_lines, *badge_lines, *screen.lines)):
        fitted_text, fitted_size = fit_text(
            line.text,
            line.size,
            line.max_width,
            minimum_size=11,
            bold=line.bold,
            text_scale=appearance.text_scale,
        )
        text_path = directory / f"segment-{index:02d}-{line_index:02d}.txt"
        text_path.write_text(fitted_text, encoding="utf-8")
        font = bold if line.bold else regular
        x_expression = str(line.x)
        if line.align == "center":
            x_expression = "(w-text_w)/2" if line.x == 320 else f"{line.x}-text_w/2"
        elif line.align == "right":
            x_expression = f"{line.x}-text_w"
        filters.append(
            "drawtext="
            f"fontfile='{_filter_path(font)}':"
            f"textfile='{_filter_path(text_path)}':"
            "expansion=none:"
            f"fontcolor={line.color}:fontsize={fitted_size}:x={x_expression}:y={line.y}:"
            "borderw=1:bordercolor=black@0.72:shadowx=1:shadowy=1:shadowcolor=black@0.48"
        )
    filter_path = directory / f"segment-{index:02d}.filter"
    motion_background = bool(
        screen.background_asset
        and screen.background_asset.suffix.lower() in MOTION_EXTENSIONS
    )
    if motion_background:
        video_input = ["-stream_loop", "-1", "-i", str(screen.background_asset)]
    elif screen.background_asset is not None:
        video_input = [
            "-loop", "1", "-framerate", "30", "-t", str(cue.duration_seconds),
            "-i", str(screen.background_asset),
        ]
    else:
        video_input = [
            "-f", "lavfi", "-i",
            f"color=c={screen.background}:s=640x480:r=30:d={cue.duration_seconds}",
        ]
    overlay_input: list[str] = []
    audio_input_index = 1
    # Keep large graphs in files to avoid Windows command-line length limits.
    # FFmpeg 9 removed the old filter_script/filter_complex_script options.
    filter_option = "-/filter:v"
    video_map = "0:v:0"
    if screen.holiday_overlay is not None:
        overlay_input = [
            "-loop", "1", "-framerate", "30", "-t", str(cue.duration_seconds),
            "-i", str(screen.holiday_overlay),
        ]
        audio_input_index = 2
        prepared = ",".join(preparation_filters) if preparation_filters else "null"
        decorated = ",\n".join(filters)
        filter_path.write_text(
            f"[0:v]{prepared}[weather];\n"
            "[1:v]scale=640:480:flags=lanczos,format=rgba[holiday];\n"
            "[weather][holiday]overlay=0:0:format=auto[seasonal];\n"
            f"[seasonal]{decorated}[vout]",
            encoding="utf-8",
        )
        filter_option = "-/filter_complex"
        video_map = "[vout]"
    else:
        filter_path.write_text(
            ",\n".join((*preparation_filters, *filters)), encoding="utf-8"
        )
    tuning = [] if motion_background else ["-tune", "stillimage"]
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *video_input,
        *overlay_input,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        filter_option, str(filter_path),
        "-map", video_map, "-map", f"{audio_input_index}:a:0", "-t", str(cue.duration_seconds),
        "-c:v", "libx264", "-preset", "ultrafast", *tuning,
        "-pix_fmt", "yuv420p", "-r", "30", "-g", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(output),
    ]
    _run(command, runner, "segment render")


def _background_filters(screen: Screen) -> list[str]:
    return [*_background_preparation_filters(screen), *_interface_filters(screen)]


def _background_preparation_filters(screen: Screen) -> list[str]:
    filters: list[str] = []
    if screen.background_asset is not None:
        filters.extend(
            (
                "scale=640:480:force_original_aspect_ratio=increase",
                "crop=640:480",
                "fps=30",
                "setsar=1",
            )
        )
    return filters


def _interface_filters(screen: Screen) -> list[str]:
    filters: list[str] = []
    palette = screen.palette
    appearance = screen.appearance or appearance_settings(None)
    filters.extend(
        (
            f"drawbox=x=0:y=0:w=iw:h=82:color={palette.header}@0.96:t=fill",
            f"drawbox=x=0:y=80:w=iw:h=4:color={screen.accent}:t=fill",
        )
    )
    for x, y, width, height, alternate in _screen_panels(
        screen.layout, appearance.safe_margin
    ):
        color = palette.panel_alternate if alternate else palette.panel
        filters.extend(
            (
                f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                f"color={color}@{appearance.panel_opacity:.2f}:t=fill",
                f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                f"color={screen.accent}@{appearance.panel_border_opacity:.2f}:t=2",
            )
        )
    if screen.badge is not None:
        filters.append(
            f"drawbox=x=468:y=15:w=136:h=34:color={screen.badge.color}@0.92:t=fill"
        )
    return filters


def _screen_panels(
    layout: str, safe_margin: int
) -> tuple[tuple[int, int, int, int, bool], ...]:
    margin = max(20, min(40, safe_margin))
    full_width = 640 - margin * 2
    if layout == "station":
        return ((64, 151, 512, 205, False),)
    if layout == "current":
        return (
            (margin, 104, 300, 232, False),
            (344, 104, 640 - margin - 344, 232, True),
            (margin, 352, full_width, 58, False),
        )
    if layout == "daily":
        return (
            (margin, 104, full_width, 72, False),
            (margin, 204, 176, 124, False),
            (224, 204, 176, 124, True),
            (420, 204, 640 - margin - 420, 124, False),
            (margin, 352, full_width, 58, False),
        )
    if layout == "hourly":
        panels: list[tuple[int, int, int, int, bool]] = [
            (margin, 102, full_width, 306, False)
        ]
        panels.extend((margin + 4, 142 + row * 48, full_width - 8, 38, row % 2 == 1) for row in range(6))
        return tuple(panels)
    if layout == "tonight":
        return (
            (margin, 106, 280, 222, False),
            (332, 106, 640 - margin - 332, 222, True),
        )
    if layout == "extended":
        return tuple((24 + column * 120, 104, 112, 302, column % 2 == 1) for column in range(5))
    return ((margin, 104, full_width, 304, False),)


def _temperature_badge(
    temperatures: Iterable[float | None],
    unit: str,
) -> TemperatureBadge | None:
    values = tuple(value for value in temperatures if value is not None)
    if not values:
        return None
    heat_threshold = 100.0 if unit == "°F" else 38.0
    freeze_threshold = 32.0 if unit == "°F" else 0.0
    if max(values) >= heat_threshold:
        return TemperatureBadge("EXTREME HEAT", "0xd9480f")
    if min(values) <= freeze_threshold:
        return TemperatureBadge("FREEZING", "0x087f8c")
    return None


def _concatenate_segments(
    segments: Sequence[Path], output: Path, directory: Path, runner: CommandRunner
) -> None:
    concat = directory / "concat.txt"
    concat.write_text(
        "".join(f"file '{segment.name}'\n" for segment in segments), encoding="utf-8"
    )
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c", "copy", "-movflags", "+faststart", str(output),
        ],
        runner,
        "segment concatenation",
    )


def _apply_soundtrack(
    silent_video: Path,
    output: Path,
    music: MusicAsset | None,
    duration_seconds: float,
    settings: Mapping[str, object],
    runner: CommandRunner,
) -> str:
    if music is None:
        os.replace(silent_video, output)
        return "silence"
    input_count, stream_loop, filter_graph, strategy = soundtrack_filter(
        music.duration_seconds,
        duration_seconds,
        settings,
    )
    music_inputs: list[str] = []
    for index in range(input_count):
        if index == 0 and stream_loop:
            music_inputs.extend(["-stream_loop", "-1"])
        music_inputs.extend(["-i", str(music.path)])
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(silent_video),
            *music_inputs,
            "-filter_complex", filter_graph,
            "-map", "0:v:0", "-map", "[aout]",
            "-t", f"{duration_seconds:.3f}",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(output),
        ],
        runner,
        "music normalization and mixing",
    )
    silent_video.unlink(missing_ok=True)
    return strategy


def _probe_video(video: Path, runner: CommandRunner) -> Mapping[str, Any]:
    completed = _run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(video),
        ],
        runner,
        "video validation",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError("FFprobe returned invalid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise RenderError("FFprobe did not return an object.")
    return payload


def _validate_seeking(video: Path, duration_seconds: float, runner: CommandRunner) -> None:
    samples = (0.5, duration_seconds / 2, duration_seconds - 0.5)
    for offset in samples:
        _run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}",
                "-i", str(video), "-frames:v", "1", "-f", "null", "-",
            ],
            runner,
            f"representative seek at {offset:.1f} seconds",
        )


def _validate_probe(probe: Mapping[str, Any], expected_duration: float) -> float:
    streams = probe.get("streams")
    format_data = probe.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, Mapping):
        raise RenderError("Rendered video is missing FFprobe stream data.")
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not isinstance(video, Mapping) or not isinstance(audio, Mapping):
        raise RenderError("Rendered channel must contain video and audio streams.")
    if (video.get("width"), video.get("height")) != (640, 480):
        raise RenderError("Rendered video must be exactly 640x480.")
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise RenderError("Rendered video must use H.264 with yuv420p pixels.")
    frame_rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    try:
        frames_per_second = float(Fraction(str(frame_rate)))
    except (ValueError, ZeroDivisionError) as exc:
        raise RenderError("Rendered video has no valid frame rate.") from exc
    if abs(frames_per_second - 30.0) > 0.01:
        raise RenderError(f"Rendered video must be 30 fps, got {frames_per_second:g} fps.")
    if (
        audio.get("codec_name") != "aac"
        or int(audio.get("sample_rate", 0)) != 48000
        or int(audio.get("channels", 0)) != 2
    ):
        raise RenderError("Rendered audio must use AAC stereo at 48 kHz.")
    audio_duration = audio.get("duration")
    if audio_duration is not None:
        try:
            parsed_audio_duration = float(audio_duration)
        except (TypeError, ValueError) as exc:
            raise RenderError("Rendered audio has no valid duration.") from exc
        if parsed_audio_duration < expected_duration - 0.15:
            raise RenderError(
                f"Rendered audio ends at {parsed_audio_duration:.3f}s; "
                f"expected coverage through {expected_duration:.3f}s."
            )
    try:
        duration = float(format_data["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderError("Rendered video has no valid duration.") from exc
    if abs(duration - expected_duration) > 0.15:
        raise RenderError(
            f"Rendered duration is {duration:.3f}s; expected {expected_duration}s."
        )
    return duration


def validate_version_directory(
    version_directory: Path,
    *,
    runner: CommandRunner = subprocess.run,
    expected_generation_id: str | None = None,
) -> float:
    try:
        manifest = validate_version_manifest(
            version_directory,
            expected_generation_id=expected_generation_id,
        )
    except ManifestValidationError as exc:
        raise RenderError(f"Published version is invalid: {exc}") from exc
    probe = _probe_video(manifest.video_path, runner)
    probed_duration = _validate_probe(probe, manifest.timeline_duration_seconds)
    if abs(probed_duration - manifest.duration_seconds) > 0.01:
        raise RenderError(
            "Published manifest duration does not match the probed video duration."
        )
    if manifest.timeline_duration_seconds > probed_duration + 1e-6:
        raise RenderError("Published segment timeline extends beyond the video duration.")
    _validate_seeking(manifest.video_path, manifest.timeline_duration_seconds, runner)
    return probed_duration


def load_active_channel(paths: AppPaths) -> tuple[dict[str, Any], Path]:
    pointer_path = paths.output / "current.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"Unable to read active channel pointer: {exc}") from exc
    if not isinstance(pointer, dict) or pointer.get("schema_version") != POINTER_SCHEMA_VERSION:
        raise RenderError("Active channel pointer has an unsupported schema.")
    relative = pointer.get("manifest")
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RenderError("Active channel manifest path is unsafe.")
    manifest_path = (paths.output / relative).resolve()
    if paths.output.resolve() not in manifest_path.parents:
        raise RenderError("Active channel manifest escapes the output directory.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"Unable to read active channel manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise RenderError("Active channel manifest has an unsupported schema.")
    return manifest, manifest_path


def _manifest(
    snapshot: NormalizedWeatherSnapshot,
    plan: ForecastPlan,
    screens: Sequence[Screen],
    settings: Mapping[str, Any],
    generation_id: str,
    generated_at: datetime,
    duration: float,
) -> dict[str, Any]:
    channel = settings["channel"]
    render_minutes = int(settings.get("refresh", {}).get("render_minutes", 30))
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "channel": {
            "slug": str(channel.get("slug", "weather")),
            "display_name": str(channel.get("display_name", "RetroWeather")),
        },
        "generation_id": generation_id,
        "generated_at": generated_at.isoformat(),
        "valid_until": (generated_at + timedelta(minutes=render_minutes)).isoformat(),
        "timezone": snapshot.location.timezone,
        "video": VIDEO_NAME,
        "duration_seconds": round(duration, 3),
        "attribution": list(snapshot.metadata.attribution),
        "segments": [
            {
                "id": cue.identifier,
                "start_seconds": cue.start_seconds,
                "end_seconds": cue.end_seconds,
                "title": cue.title,
                "condition_theme": screen.theme.key,
                "background": screen.background_asset.name if screen.background_asset else None,
            }
            for cue, screen in zip(plan.segments, screens, strict=True)
        ],
    }


def _font_paths() -> tuple[Path, Path]:
    candidates = (
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    )
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            return regular, bold
    raise RenderError("No supported system font was found for forecast rendering.")


def _filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _temperature(value: float | None, unit: str) -> str:
    return "—" if value is None else f"{value:.0f}{unit}"


def _number(value: float | None, suffix: str, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def _sun_times(forecast: DailyForecast) -> str:
    rise = _clock_time(forecast.sunrise) if forecast.sunrise else "—"
    set_at = _clock_time(forecast.sunset) if forecast.sunset else "—"
    return f"SUNRISE {rise}   •   SUNSET {set_at}"


def _clock_time(value: datetime, include_minutes: bool = True) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}" if include_minutes else f"{hour} {suffix}"


def _run(command: Sequence[str], runner: CommandRunner, operation: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command), capture_output=True, text=True, check=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RenderError(f"{command[0]} is required for {operation}.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{operation.capitalize()} timed out.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RenderError(f"{operation.capitalize()} failed: {detail}") from exc


def _notify_progress(
    callback: ProgressCallback | None, stage: str, percent: int
) -> None:
    if callback is not None:
        callback(stage, max(0, min(100, int(percent))))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_json(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _render_lock(paths: AppPaths):
    paths.state.mkdir(parents=True, exist_ok=True)
    lock = paths.state / "render.lock"
    descriptor = _acquire_render_lock(lock, paths.versions)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                stream,
            )
            stream.flush()
        yield
    finally:
        lock.unlink(missing_ok=True)


def _acquire_render_lock(lock: Path, versions: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        return os.open(lock, flags)
    except FileExistsError:
        signature = _lock_signature(lock)
        if not _lock_is_abandoned(lock):
            raise RenderError("Another channel render is already in progress.")
        if signature is not None and _lock_signature(lock) != signature:
            raise RenderError("Another channel render changed the lock during recovery.")
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(lock, flags)
        except FileExistsError as exc:
            raise RenderError("Another channel render started during recovery.") from exc
        try:
            _remove_abandoned_builds(versions)
        except Exception:
            os.close(descriptor)
            lock.unlink(missing_ok=True)
            raise
        return descriptor


def _lock_signature(lock: Path) -> tuple[int, int, int] | None:
    try:
        stat = lock.stat()
    except FileNotFoundError:
        return None
    return (int(stat.st_ino), stat.st_mtime_ns, stat.st_size)


def _lock_is_abandoned(lock: Path) -> bool:
    try:
        age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - lock.stat().st_mtime)
        text = lock.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not text:
        return age_seconds >= 5
    try:
        raw = json.loads(text)
        pid = raw.get("pid") if isinstance(raw, dict) else int(text)
        pid = int(pid)
    except (json.JSONDecodeError, TypeError, ValueError):
        return age_seconds >= 5
    if pid <= 0:
        return age_seconds >= 5
    if age_seconds >= 6 * 60 * 60:
        return True
    return not _process_exists(pid)


def _process_exists(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_abandoned_builds(versions: Path) -> None:
    resolved_versions = versions.resolve()
    if not versions.is_dir():
        return
    for item in versions.iterdir():
        if not item.is_dir() or not item.name.startswith(".") or not item.name.endswith(".building"):
            continue
        resolved = item.resolve()
        if resolved_versions not in resolved.parents:
            raise RenderError(f"Refusing to remove build outside versions root: {item}")
        shutil.rmtree(resolved)


def _prune_versions(paths: AppPaths, settings: Mapping[str, Any], keep: str) -> None:
    count = int(settings.get("retention", {}).get("valid_versions", 3))
    versions = sorted(
        (item for item in paths.versions.iterdir() if item.is_dir() and not item.name.startswith(".")),
        key=lambda item: item.name,
        reverse=True,
    )
    retained = {item.name for item in versions[: max(1, count)]}
    retained.add(keep)
    for version in versions:
        if version.name not in retained:
            shutil.rmtree(version)
