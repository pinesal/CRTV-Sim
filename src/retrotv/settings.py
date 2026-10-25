from __future__ import annotations


import copy
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from retrotv.credentials import configured_breeze_api_key, configured_openai_api_key
from retrotv.processing import PRESETS, resolution_preset
from retrotv.classification import resolve_channel_root, slugify
from retrotv.config import AppConfig
from retrotv.holiday_review import clue_settings, validate_clue_lists


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)
WEEKLY_COLLECTION_MEDIA_FOLDERS = (
    "episodes",
    "shorts",
    "movies",
    "blocks",
    "bumpers",
    "commercials",
    "promos",
    "station-ids",
    "filler",
    "up-next",
    "interstitials",
)
HOLIDAY_MEDIA_FOLDERS = tuple(
    folder for folder in WEEKLY_COLLECTION_MEDIA_FOLDERS
    if folder not in {"up-next", "interstitials"}
)
SHARED_DAYPART_MEDIA_FOLDERS = ("commercials", "bumpers", "promos", "filler")


def _field(
    path: str,
    label: str,
    group: str,
    kind: str = "number",
    **options: Any,
) -> dict[str, Any]:
    return {"path": path, "label": label, "group": group, "type": kind, **options}


SETTING_FIELDS = (
    _field("application.timezone", "Timezone", "System", "text", restart=True),
    _field("storage_roots.media", "Media library folder", "System", "path", restart=True),
    _field("storage_roots.originals_backup", "Originals backup folder", "System", "path", restart=True),
    _field("database_backups.enabled", "Create daily database backups", "System", "boolean"),
    _field("database_backups.directory", "Database backup folder", "System", "path", restart=True),
    _field("database_backups.retention_count", "Daily database backups to retain", "System", min=1, max=365),
    _field("internet_media.cache_directory", "Internet episode cache", "Internet media", "path", restart=True),
    _field("internet_media.cache_limit_gib", "Cache limit (GiB)", "Internet media", min=1, max=1000),
    _field("internet_media.minimum_free_gib", "Minimum free disk space (GiB)", "Internet media", min=1, max=1000),
    _field("internet_media.maximum_retries", "Download retry attempts", "Internet media", min=0, max=20),
    _field("library.long_commercial_minutes", "Commercial review threshold (minutes)", "Media review", min=0.5, max=60, step=0.1),
    _field("web.host", "Dashboard host", "System", "text", restart=True),
    _field("web.port", "Dashboard port", "System", min=1, max=65535, restart=True),
    _field("player.backend", "Player backend", "Playback", "select", options=["auto", "mpv", "ffplay"]),
    _field("player.mpv", "mpv executable", "Playback", "path", restart=True),
    _field("player.fullscreen", "Start full screen", "Playback", "boolean"),
    _field("player.foreground_on_start", "Bring player to front when TV starts", "Playback", "boolean"),
    _field("player.osd_color", "Channel-label color", "Playback", "color"),
    _field("player.osd_font_size", "Channel-label text size", "Playback", min=24, max=96),
    _field("player.osd_margin_x", "Channel-label left margin", "Playback", min=0, max=320),
    _field("player.osd_margin_y", "Channel-label top margin", "Playback", min=0, max=240),
    _field("player.skip_short_remaining_seconds", "Skip nearly-ended media (seconds)", "Playback", min=0, max=30, step=0.1),
    _field("application_layer.startup_channel", "Startup channel", "Automation", "channel"),
    _field("application_layer.start_tv_on_launch", "Start TV when CRTV Sim opens", "Automation", "boolean"),
    _field("application_layer.maintenance_interval_seconds", "Maintenance interval (seconds)", "Automation", min=60, max=86400),
    _field("application_layer.schedule_horizon_days", "Days to schedule", "Automation", min=1, max=30),
    _field("application_layer.analyze_new_media", "Analyze new video automatically", "Automation", "boolean"),
    _field("application_layer.auto_readiness_when_tv_idle", "Complete readiness work while TV is stopped", "Automation", "boolean"),
    _field("application_layer.auto_convert_approved_media", "Convert approved video automatically", "Automation", "boolean"),
    _field("application_layer.auto_analyze_pending_audio", "Analyze pending audio automatically", "Automation", "boolean"),
    _field("application_layer.auto_normalize_approved_audio", "Normalize approved audio automatically", "Automation", "boolean"),
    _field("application_layer.analyze_breakpoints", "Find act breaks automatically", "Automation", "boolean"),
    _field("application_layer.breakpoint_analysis_limit_per_maintenance", "Act-break analyses per maintenance", "Automation", min=0, max=1000),
    _field("maintenance_window.enabled", "Enable daily maintenance window", "Maintenance window", "boolean"),
    _field("maintenance_window.start", "Window starts", "Maintenance window", "time"),
    _field("maintenance_window.end", "Window ends", "Maintenance window", "time"),
    _field("database_housekeeping.enabled", "Enable daily database housekeeping", "Database housekeeping", "boolean"),
    _field("database_housekeeping.minimum_interval_hours", "Minimum interval (hours)", "Database housekeeping", min=1, max=720),
    _field("database_housekeeping.schedule_retention_days", "Schedule history retention (days)", "Database housekeeping", min=1, max=3650),
    _field("database_housekeeping.operation_retention_days", "Operational history retention (days)", "Database housekeeping", min=1, max=3650),
    _field("database_housekeeping.missing_asset_retention_days", "Missing media retention (days)", "Database housekeeping", min=1, max=3650),
    _field("duplicate_detection.hash_limit_per_maintenance", "Exact hashes per maintenance", "Automation", min=1, max=10000),
    _field("library.auto_register_channels", "Create channels from folders", "Channels", "boolean"),
    _field("library.auto_disable_missing_channels", "Disable channels whose folders disappear", "Channels", "boolean"),
    _field("library.auto_channel_number_start", "First automatic channel number", "Channels", min=1, max=999),
    _field("scheduling.clock_slot_minutes", "Normal clock slot (minutes)", "Scheduling", min=15, max=180),
    _field("scheduling.short_program_target_minutes", "Programming target per half-hour", "Scheduling", min=1, max=30),
    _field("scheduling.commercials_per_break_min", "Preferred minimum commercials per pod", "Scheduling", min=0, max=20),
    _field("scheduling.commercials_per_break_max", "Maximum commercials per pod", "Scheduling", min=0, max=20),
    _field("scheduling.gap_fill_commercials_max", "Maximum gap-filling commercials", "Scheduling", min=0, max=20),
    _field("scheduling.commercial_cooldown_seconds", "Preferred commercial repeat cooldown (seconds)", "Scheduling", min=0, max=604800),
    _field("scheduling.bumper_cooldown_seconds", "Bumper repeat cooldown (seconds)", "Scheduling", min=0, max=604800),
    _field("scheduling.opening_bumper_enabled", "Open blocks with a bumper", "Scheduling", "boolean"),
    _field("scheduling.midroll_enabled", "Insert mid-program commercial breaks", "Act breaks", "boolean"),
    _field("scheduling.midroll_min_media_minutes", "Minimum program length (minutes)", "Act breaks", min=1, max=240),
    _field("scheduling.midroll_search_window_minutes", "Preferred break search window (minutes)", "Act breaks", min=0.25, max=30, step=0.25, help="Strong natural cues may be selected outside this window. Held cards and fallback cuts stay within it; minimum segment spacing always applies."),
    _field("scheduling.midroll_min_segment_minutes", "Minimum segment length (minutes)", "Act breaks", min=1, max=60),
    _field("scheduling.midroll_max_pod_minutes", "Maximum episode-break ad pod (minutes)", "Act breaks", min=0.5, max=30, step=0.5),
    _field("scheduling.midroll_fallback", "When no natural break is found", "Act breaks", "select", options=["midpoint", "none"], help="Fallbacks try a complete 1.5-second quiet pause, then a scene transition without silence, then midpoint. Natural and held-card cues remain available when fallbacks are disabled."),
    _field("blocks.selection_probability", "Complete-block selection probability", "Complete blocks", min=0, max=1, step=0.05),
    _field("blocks.maximum_per_daypart", "Maximum complete blocks per daypart", "Complete blocks", min=0, max=20),
    _field("blocks.repeat_cooldown_days", "Complete-block repeat cooldown (days)", "Complete blocks", min=0, max=365),
    _field("blocks.fallback_when_no_programming", "Use complete blocks before filler", "Complete blocks", "boolean"),
    _field("blocks.fallback_relaxes_repeat_restrictions", "Relax block repeat restrictions before filler", "Complete blocks", "boolean"),
    _field("blocks.boundary_policy", "Complete-block daypart boundary", "Complete blocks", "select", options=["must_fit", "may_overrun"]),
    _field("blocks.long_daypart_threshold_hours", "Hours required for two block opportunities", "Complete blocks", min=1, max=24, step=0.5),
    _field("blocks.extended_daypart_threshold_hours", "Hours required for three block opportunities", "Complete blocks", min=1, max=24, step=0.5),
    _field("blocks.overlap_policy", "Occupied block opportunity", "Complete blocks", "select", options=["join_in_progress", "skip"]),
    _field("blocks.minimum_join_remaining_minutes", "Minimum block time remaining to join (minutes)", "Complete blocks", min=0, max=1440),
    _field("blocks.minimum_join_remaining_fraction", "Minimum block fraction remaining to join", "Complete blocks", min=0, max=1, step=0.05),
    _field("guide.channel_number", "Guide channel number", "Guide", min=1, max=999),
    _field("guide.scroll_pixels_per_second", "Guide scroll speed", "Guide", min=1, max=100, step=0.5),
    _field("guide.scroll_row_height", "Guide row height", "Guide", min=40, max=100),
    _field("guide.text_size_percent", "Guide text size (%)", "Guide", min=75, max=150, step=5),
    _field("guide.abbreviate_channel_labels", "Abbreviate long channel names", "Guide", "boolean"),
    _field("guide.show_pending_action_indicator", "Show pending-action [!]", "Guide", "boolean"),
    _field("guide.music_volume", "Guide music volume", "Guide", min=0, max=1, step=0.01),
    _field("guide.music_max_minutes", "Maximum Guide music length (minutes)", "Guide", min=1, max=60),
    _field("synthetic_channels.weather.enabled", "Enable Weather channel", "Weather channel", "boolean"),
    _field("synthetic_channels.weather.root", "Published channel folder", "Weather channel", "path", restart=True),
    _field("synthetic_channels.weather.display_name", "Channel display name", "Weather channel", "text"),
    _field("synthetic_channels.weather.stale_warning_minutes", "Warn when publication age exceeds (minutes)", "Weather channel", min=1, max=10080),
    _field("synthetic_channels.weather.manage_companion", "Start and supervise RetroWeather", "Weather channel", "boolean", restart=True),
    _field("synthetic_channels.weather.companion_home", "RetroWeather application folder", "Weather channel", "path", restart=True),
    _field("synthetic_channels.weather.companion_host", "RetroWeather host", "Weather channel", "text", restart=True),
    _field("synthetic_channels.weather.companion_port", "RetroWeather port", "Weather channel", min=1, max=65535, restart=True),
    _field("synthetic_channels.weather.companion_health_poll_seconds", "RetroWeather health poll (seconds)", "Weather channel", min=1, max=300),
    _field("synthetic_channels.weather.companion_startup_timeout_seconds", "RetroWeather startup timeout (seconds)", "Weather channel", min=2, max=300),
    _field("synthetic_channels.weather.companion_restart_initial_seconds", "Initial crash-restart delay (seconds)", "Weather channel", min=1, max=300),
    _field("synthetic_channels.weather.companion_restart_max_seconds", "Maximum crash-restart delay (seconds)", "Weather channel", min=1, max=3600),
    _field("guide.greetings.morning", "Morning greeting", "Guide greetings", "text"),
    _field("guide.greetings.afternoon", "Afternoon greeting", "Guide greetings", "text"),
    _field("guide.greetings.evening", "Evening greeting", "Guide greetings", "text"),
    _field("guide.greetings.night", "Night greeting", "Guide greetings", "text"),
    _field("graphics.interstitial_duration_seconds", "Station-graphic duration (seconds)", "Station graphics", min=1, max=60),
    _field("graphics.cooldown_minutes", "Station-graphic cooldown (minutes)", "Station graphics", min=0, max=1440),
    _field("graphics.standalone_enabled", "Use standalone station graphics as filler", "Station graphics", "boolean"),
    _field("graphics.music_volume", "Interstitial music volume", "Station graphics", min=0, max=1, step=0.01),
    _field("graphics.up_next_music_volume", "Up-next music volume", "Station graphics", min=0, max=1, step=0.01),
    _field("announcements.tts_provider", "Announcer provider", "Announcer", "select", options=["openai", "none"]),
    _field("announcements.script_model", "Script model", "Announcer", "text"),
    _field("announcements.speech_model", "Speech model", "Announcer", "text"),
    _field("announcements.dwell_seconds", "Channel dwell before generation (seconds)", "Announcer", min=0, max=3600),
    _field("announcements.activity_window_minutes", "Human-activity window (minutes)", "Announcer", min=1, max=1440),
    _field("announcements.minimum_lead_minutes", "Minimum cloud lead time (minutes)", "Announcer", min=0, max=60, step=0.25),
    _field("announcements.maximum_lead_minutes", "Maximum cloud lead time (minutes)", "Announcer", min=0.25, max=120, step=0.25),
    _field("announcements.maximum_words", "Maximum generated words", "Announcer", min=5, max=200),
    _field("normalization.enabled", "Enable video normalization", "Video normalization", "boolean"),
    _field("normalization.resolution_preset", "Maximum resolution preset", "Video normalization", "select", options=[*PRESETS, "Custom"]),
    _field("normalization.aspect_policy", "Aspect correction policy", "Video normalization", "select", options=["retro_4_3", "preserve"]),
    _field("normalization.output_policy", "File handling (proxy keeps source video in place)", "Video normalization", "select", options=["replace_with_backup", "proxy"]),
    _field("normalization.canvas_width", "Maximum corrected width (Custom)", "Video normalization", min=160, max=3840),
    _field("normalization.canvas_height", "Maximum corrected height", "Video normalization", min=120, max=2160),
    _field("normalization.allow_upscale", "Upscale smaller video", "Video normalization", "boolean"),
    _field("normalization.video_crf", "Video quality CRF", "Video normalization", min=0, max=51),
    _field("normalization.video_preset", "Encoding preset", "Video normalization", "select", options=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]),
    _field("normalization.maximum_output_size_ratio", "Preferred maximum size ratio", "Video normalization", min=0.25, max=5, step=0.05),
    _field("normalization.accept_oversize_after_retries", "Keep corrected files when size target cannot be met", "Video normalization", "boolean"),
    _field("audio_normalization.enabled", "Enable audio loudness normalization", "Audio normalization", "boolean"),
    _field("commercial_tails.enabled", "Automatically trim commercial black tails (modifies files)", "Media review", "boolean"),
    _field("audio_normalization.target_lufs", "Target loudness (LUFS)", "Audio normalization", min=-40, max=-5, step=0.5),
    _field("audio_normalization.true_peak_dbtp", "True-peak ceiling (dBTP)", "Audio normalization", min=-12, max=0, step=0.1),
    _field("audio_normalization.loudness_tolerance_lu", "Loudness tolerance (LU)", "Audio normalization", min=0.1, max=10, step=0.1),
    _field("audio_normalization.analyze_during_maintenance", "Analyze audio during maintenance", "Audio normalization", "boolean"),
    _field("audio_normalization.analysis_limit_per_maintenance", "Audio analyses per maintenance", "Audio normalization", min=0, max=1000),
    _field("generated_cleanup.enabled", "Enable automatic file cleanup", "File cleanup", "boolean"),
    _field("generated_cleanup.run_during_maintenance", "Clean during maintenance", "File cleanup", "boolean"),
    _field("generated_cleanup.minimum_interval_hours", "Minimum cleanup interval (hours)", "File cleanup", min=1, max=720),
    _field("generated_cleanup.silent_retention_days", "Silent announcement retention (days)", "File cleanup", min=1, max=365),
    _field("generated_cleanup.narrated_retention_days", "Narrated announcement retention (days)", "File cleanup", min=1, max=365),
    _field("generated_cleanup.graphics_retention_days", "Generated graphics retention (days)", "File cleanup", min=1, max=365),
    _field("generated_cleanup.aspect_preview_retention_days", "Unapproved aspect preview retention (days)", "File cleanup", min=1, max=365),
    _field("generated_cleanup.delete_normalization_backups", "Delete expired normalization backups", "File cleanup", "boolean"),
    _field("generated_cleanup.normalization_backup_retention_days", "Normalization backup retention (days)", "File cleanup", min=1, max=3650),
)

FIELDS_BY_PATH = {field["path"]: field for field in SETTING_FIELDS}


def settings_override_path(config: AppConfig) -> Path:
    return config.profile_path.with_suffix(".settings.json")


def read_settings_overrides(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid settings override file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Settings override file must contain a JSON object")
    return payload


def _get_path(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(value)


def _delete_path(data: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    lineage: list[tuple[dict[str, Any], str]] = []
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        lineage.append((current, part))
        current = child
    current.pop(parts[-1], None)
    for parent, key in reversed(lineage):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)


def _store_override(
    overrides: dict[str, Any], baseline: dict[str, Any], path: str, value: Any
) -> None:
    if value == _get_path(baseline, path):
        _delete_path(overrides, path)
    else:
        _set_path(overrides, path, value)


def _merge_in_place(target: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if key not in {"holiday_filename_clues", "bumper_filename_clues"} and isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_in_place(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class SettingsService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.path = settings_override_path(config)

    def snapshot(self, channels: list[dict[str, Any]]) -> dict[str, Any]:
        effective = copy.deepcopy(self.config.data)
        _merge_in_place(effective, read_settings_overrides(self.path))
        windows = copy.deepcopy(effective.get('seasonal', {}).get('windows', {}))
        if isinstance(windows, list):
            windows = {item['slug']: {key: value for key, value in item.items() if key != 'slug'}
                       for item in windows}
        from retrotv.seasonal import weekly_schedule_mode
        with closing(sqlite3.connect(self.config.database_path.as_uri() + '?mode=ro', uri=True)
                     if self.config.database_path.is_file() else sqlite3.connect(':memory:')) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for slug, window in windows.items():
                window['schedule_mode'] = weekly_schedule_mode(
                    connection if {'calendar_rules', 'channel_schedule_preferences'} <= tables else None,
                    {'slug': slug, **window})
        startup_destinations = copy.deepcopy(channels)
        startup_destinations.append(
            {
                "slug": "__guide__",
                "display_name": "CRTV Sim Guide",
                "channel_number": int(effective.get("guide", {}).get("channel_number", 2)),
                "synthetic": True,
                "is_guide": True,
            }
        )
        return {
            "profile": self.config.profile_name,
            "profile_path": str(self.config.profile_path),
            "override_path": str(self.path),
            "fields": [dict(field, value=(resolution_preset(effective) if field["path"] == "normalization.resolution_preset"
                       else _get_path(effective, field["path"], True if field["path"] in
                       {"normalization.enabled", "audio_normalization.enabled", "commercial_tails.enabled"} else
                       "retro_4_3" if field["path"] == "normalization.aspect_policy" else None))) for field in SETTING_FIELDS],
            "channels": channels,
            "startup_destinations": startup_destinations,
            "channel_numbers": copy.deepcopy(effective.get("guide", {}).get("channel_numbers", {})),
            "dayparts": copy.deepcopy(effective.get("scheduling", {}).get("dayparts", [])),
            "holidays": copy.deepcopy(effective.get("seasonal", {}).get("holidays", {})),
            "breeze_key_configured": bool(configured_breeze_api_key()),
            "openai_key_configured": bool(configured_openai_api_key()),
            "filename_clues": clue_settings(AppConfig(self.config.profile_path, self.config.project_root, effective)),
            "windows": windows,
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Settings payload must be an object")
        overrides = read_settings_overrides(self.path)
        baseline = self.config.source_data or self.config.data
        live_override: dict[str, Any] = {}
        restart_paths: list[str] = []
        changed_live_paths: list[str] = []
        values = payload.get("values", {})
        if not isinstance(values, dict):
            raise ValueError("Settings values must be an object")
        values = dict(values)
        preset = values.pop("normalization.resolution_preset", None)
        if preset is not None:
            if preset not in {*PRESETS, "Custom"}:
                raise ValueError("Unknown resolution preset")
            if preset != "Custom":
                values["normalization.canvas_width"], values["normalization.canvas_height"] = PRESETS[preset]
        for path, raw in values.items():
            field = FIELDS_BY_PATH.get(path)
            if field is None:
                raise ValueError(f"Unknown setting: {path}")
            value = self._coerce(field, raw)
            _store_override(overrides, baseline, path, value)
            if field.get("restart") and value != _get_path(self.config.data, path):
                restart_paths.append(path)
            if not field.get("restart"):
                if value != _get_path(self.config.data, path):
                    changed_live_paths.append(path)
                _set_path(live_override, path, value)

        proposed = copy.deepcopy(self.config.data)
        _merge_in_place(proposed, live_override)
        window = proposed.get("maintenance_window", {})
        if window.get("enabled") and window.get("start") == window.get("end"):
            raise ValueError("Maintenance window start and end times must differ")

        dayparts_changed = False
        if "dayparts" in payload:
            dayparts = self._validate_dayparts(payload["dayparts"])
            current_dayparts = self._validate_dayparts(
                self.config.data.get("scheduling", {}).get("dayparts", [])
            )
            dayparts_changed = dayparts != current_dayparts
            if dayparts_changed:
                _store_override(overrides, baseline, "scheduling.dayparts", dayparts)
                _set_path(live_override, "scheduling.dayparts", dayparts)
        if "channel_numbers" in payload:
            numbers = self._validate_channel_numbers(
                payload["channel_numbers"],
                int(_get_path(overrides, "guide.channel_number", _get_path(self.config.data, "guide.channel_number", 2))),
            )
            _store_override(overrides, baseline, "guide.channel_numbers", numbers)
            _set_path(live_override, "guide.channel_numbers", numbers)
        if "holidays" in payload:
            holidays = self._validate_holidays(payload["holidays"])
            _store_override(overrides, baseline, "seasonal.holidays", holidays)
            _set_path(live_override, "seasonal.holidays", holidays)
        if "filename_clues" in payload:
            raw = payload["filename_clues"]
            if not isinstance(raw, dict):
                raise ValueError("Filename clues must be an object")
            holidays = validate_clue_lists(raw.get("holidays", {}))
            bumpers = validate_clue_lists(raw.get("bumpers", {}), bumper=True)
            if set(bumpers) != {"break_start", "break_end"}:
                raise ValueError("Provide both bumper placement clue lists")
            for slug in _get_path(live_override, "seasonal.holidays", self.config.data.get("seasonal", {}).get("holidays", {})):
                holidays.setdefault(slug, [slug.replace("-", " ")])
            for key, value in (("holiday_filename_clues", holidays), ("bumper_filename_clues", bumpers)):
                _set_path(overrides, "library." + key, value)
                _set_path(live_override, "library." + key, value)
        elif "holidays" in payload:
            groups = clue_settings(self.config)["holidays"]
            for slug in holidays:
                groups.setdefault(slug, [slug.replace("-", " ")])
            _set_path(overrides, "library.holiday_filename_clues", groups)
            _set_path(live_override, "library.holiday_filename_clues", groups)
        weekly_directories_created: list[str] = []
        holiday_directories_created: list[str] = []
        daypart_directories_created: list[str] = []
        weekly_collection_roots: list[str] = []
        if "windows" in payload:
            windows = self._validate_windows(payload["windows"])
            _store_override(overrides, baseline, "seasonal.windows", windows)
            _set_path(live_override, "seasonal.windows", windows)
        if {"windows", "holidays"} & payload.keys() or dayparts_changed:
            effective_windows = _get_path(live_override, "seasonal.windows", self.config.data.get("seasonal", {}).get("windows", {}))
            effective_holidays = _get_path(live_override, "seasonal.holidays", self.config.data.get("seasonal", {}).get("holidays", {}))
            effective_dayparts = _get_path(live_override, "scheduling.dayparts", self.config.data.get("scheduling", {}).get("dayparts", []))
            weekly_directories_created, weekly_collection_roots = (
                self._ensure_weekly_collection_directories(overrides, effective_windows)
            )
            holiday_directories_created = self.ensure_holiday_directories(
                effective_windows, effective_holidays, effective_dayparts, overrides=overrides
            )
        if dayparts_changed:
            daypart_directories_created = self.ensure_daypart_directories(
                effective_dayparts, overrides=overrides
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(overrides, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        _merge_in_place(self.config.data, live_override)
        if dayparts_changed:
            self._refresh_daypart_eligibility()
        folder_message = ""
        if weekly_directories_created:
            collection_count = len(weekly_collection_roots)
            folder_message = (
                f" Created {len(weekly_directories_created)} missing media "
                f"folder{'s' if len(weekly_directories_created) != 1 else ''} "
                f"for {collection_count} weekly "
                f"collection{'s' if collection_count != 1 else ''}."
            )
        if holiday_directories_created:
            folder_message += f" Created {len(holiday_directories_created)} missing holiday folder(s)."
        if daypart_directories_created:
            folder_message += f" Created {len(daypart_directories_created)} missing daypart folder(s)."
        return {
            "ok": True,
            "override_path": str(self.path),
            "restart_required": bool(restart_paths),
            "restart_settings": restart_paths,
            "changed_live_settings": changed_live_paths,
            "weekly_directories_created": weekly_directories_created,
            "holiday_directories_created": holiday_directories_created,
            "daypart_directories_created": daypart_directories_created,
            "dayparts_changed": dayparts_changed,
            "weekly_collection_roots": weekly_collection_roots,
            "message": (
                "Settings saved." + folder_message
                + (
                    " Restart CRTV Sim to apply system path or server changes."
                    if restart_paths else
                    " Existing schedules remain available while maintenance rebuilds the changed dayparts."
                    if dayparts_changed else
                    " Changes that affect scheduling apply during the next maintenance run."
                )
            ),
        }

    def ensure_daypart_directories(
        self,
        dayparts: list[dict[str, Any]],
        *,
        overrides: dict[str, Any] | None = None,
    ) -> list[str]:
        """Create the ordinary drag-and-drop tree for every configured daypart.

        This deliberately does not create media files.  It gives a new daypart
        the same usable channel and template structure as the built-in ones.
        """
        raw_root = _get_path(
            overrides or {}, "storage_roots.media",
            self.config.data.get("storage_roots", {}).get("media"),
        )
        if not raw_root:
            raise ValueError("Media library folder is required")
        expanded = Path(os.path.expandvars(os.path.expanduser(str(raw_root))))
        media_root = expanded.resolve() if expanded.is_absolute() else (self.config.project_root / expanded).resolve()
        if not media_root.is_dir():
            raise ValueError(f"Media library folder is unavailable: {media_root}")
        scopes = [str(item.get("slug", "")).strip() for item in dayparts if isinstance(item, dict)]
        scopes = [scope for scope in scopes if scope]
        roots = [
            path for path in media_root.iterdir()
            if path.is_dir() and path.name.casefold() not in {"shared", "_guide"}
        ]
        template = media_root / "_channel_template"
        if template not in roots:
            roots.append(template)
        created: list[str] = []
        for root in roots:
            for scope in scopes:
                for folder in HOLIDAY_MEDIA_FOLDERS:
                    target = root / scope / folder
                    if target.exists():
                        if not target.is_dir():
                            raise ValueError(f"Daypart media folder path is not a directory: {target}")
                        continue
                    target.mkdir(parents=True)
                    created.append(str(target))
        for scope in ["all-day", *scopes]:
            for folder in SHARED_DAYPART_MEDIA_FOLDERS:
                target = media_root / "shared" / scope / folder
                if target.exists():
                    if not target.is_dir():
                        raise ValueError(f"Shared daypart media folder path is not a directory: {target}")
                    continue
                target.mkdir(parents=True)
                created.append(str(target))
        return created

    def _refresh_daypart_eligibility(self) -> None:
        """Reclassify paths only; changing a daypart never needs a re-probe."""
        from retrotv.classification import classify_path, slugify
        from retrotv.db import connect_database

        configured = {
            slugify(str(item.get("slug", "")))
            for item in self.config.data.get("scheduling", {}).get("dayparts", [])
            if isinstance(item, dict) and item.get("slug")
        } | {"all-day", "daytime"}
        holidays = {
            slugify(str(value))
            for value in self.config.data.get("seasonal", {}).get("holidays", {})
        }
        with connect_database(self.config.database_path) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
            ).fetchone() is None:
                return
            channels = {
                str(row["slug"]) for row in connection.execute("SELECT slug FROM channels")
            }
            rows = connection.execute(
                "SELECT id,source_relative_path FROM assets"
            ).fetchall()
            for row in rows:
                classified = classify_path(
                    str(row["source_relative_path"]), known_channels=channels,
                    known_holidays=holidays, known_dayparts=configured,
                )
                connection.execute("DELETE FROM asset_eligibility WHERE asset_id=?", (row["id"],))
                connection.executemany(
                    """INSERT INTO asset_eligibility(
                        asset_id,channel_slug,daypart_slug,eligibility_source,weight,enabled
                    ) VALUES (?,?,?,?,?,1)""",
                    [
                        (row["id"], item.channel_slug, item.daypart_slug, item.source, item.weight)
                        for item in classified.eligibility
                    ],
                )
            connection.commit()

    def _ensure_weekly_collection_directories(
        self,
        overrides: dict[str, Any],
        windows: dict[str, dict[str, Any]],
        *, existing_channels_only: bool = False,
    ) -> tuple[list[str], list[str]]:
        raw_media_root = _get_path(
            overrides,
            "storage_roots.media",
            _get_path(self.config.data, "storage_roots.media"),
        )
        if raw_media_root is None or not str(raw_media_root).strip():
            raise ValueError("Media library folder is required")
        expanded = Path(os.path.expandvars(os.path.expanduser(str(raw_media_root))))
        media_root = (
            expanded.resolve()
            if expanded.is_absolute()
            else (self.config.project_root / expanded).resolve()
        )
        if not media_root.is_dir():
            raise ValueError(f"Media library folder is unavailable: {media_root}")

        created: list[str] = []
        collection_roots: list[str] = []
        seen: set[tuple[str, str]] = set()
        pending_folders = self._pending_channel_folders()
        for window in windows.values():
            channel = window["channel"]
            collection = window["collection"]
            key = (channel, collection)
            if key in seen:
                continue
            seen.add(key)
            channel_root = resolve_channel_root(media_root, channel)
            if not channel_root.is_dir() and (existing_channels_only or slugify(channel) in pending_folders):
                continue
            collection_root = (
                channel_root / "specials" / "weekly" / collection
            ).resolve()
            try:
                collection_root.relative_to(media_root)
            except ValueError as exc:
                raise ValueError(
                    f"Weekly collection path escapes the media library: {channel}/{collection}"
                ) from exc
            collection_roots.append(str(collection_root))
            for folder_name in WEEKLY_COLLECTION_MEDIA_FOLDERS:
                target = collection_root / folder_name
                if target.exists():
                    if not target.is_dir():
                        raise ValueError(
                            f"Weekly media folder path is not a directory: {target}"
                        )
                    continue
                target.mkdir(parents=True, exist_ok=False)
                created.append(str(target))
        return created, collection_roots

    def ensure_holiday_directories(
        self,
        windows: dict[str, dict[str, Any]],
        holidays: dict[str, dict[str, Any]],
        dayparts: list[dict[str, Any]],
        *,
        overrides: dict[str, Any] | None = None,
        existing_channels_only: bool = False,
    ) -> list[str]:
        raw_root = _get_path(overrides or {}, "storage_roots.media", self.config.data.get("storage_roots", {}).get("media"))
        if not raw_root:
            raise ValueError("Media library folder is required")
        expanded = Path(os.path.expandvars(os.path.expanduser(str(raw_root))))
        media_root = expanded.resolve() if expanded.is_absolute() else (self.config.project_root / expanded).resolve()
        if not media_root.is_dir():
            raise ValueError(f"Media library folder is unavailable: {media_root}")
        channels = {str(item.get("channel", "")) for item in windows.values() if isinstance(item, dict)}
        channels.update(str(value) for value in self.config.data.get("library", {}).get("channels", []))
        channels.update(path.name for path in media_root.iterdir()
                        if path.is_dir() and path.name.casefold() not in {"shared", "_guide", "_channel_template"})
        channels.add("_channel_template")
        scopes = {"all-day", "daytime", *(str(item.get("slug")) for item in dayparts if isinstance(item, dict) and item.get("slug"))}
        created: list[str] = []
        holiday_slugs = set(holidays)
        holiday_slugs.update(str(value) for value in self.config.data.get("graphics", {}).get("holidays", []))
        pending_folders = self._pending_channel_folders()
        for channel in sorted(channels):
            if not channel:
                continue
            channel_root = (
                media_root / "_channel_template"
                if channel == "_channel_template"
                else resolve_channel_root(media_root, channel)
            )
            if channel != "_channel_template" and not channel_root.is_dir() and (existing_channels_only or slugify(channel) in pending_folders):
                continue
            collections = {str(item.get("collection", "")) for item in windows.values()
                           if isinstance(item, dict) and str(item.get("channel")) == channel}
            for holiday in sorted(holiday_slugs):
                base = channel_root / "specials" / "holidays" / holiday
                targets = [base / scope / folder
                           for scope in scopes for folder in HOLIDAY_MEDIA_FOLDERS]
                targets.extend(base / "specials" / "weekly" / collection / folder
                               for collection in collections if collection
                               for folder in WEEKLY_COLLECTION_MEDIA_FOLDERS)
                targets.extend(channel_root / "graphics" / "holidays" / holiday / purpose
                               for purpose in ("up-next", "interstitials"))
                targets.extend(channel_root / "graphics" / scope / "holidays" / holiday / purpose
                               for scope in scopes
                               for purpose in ("up-next", "interstitials"))
                for target in targets:
                    resolved = target.resolve()
                    if not resolved.is_relative_to(media_root):
                        raise ValueError(f"Holiday folder escapes the media library: {target}")
                    if target.exists():
                        if not target.is_dir():
                            raise ValueError(f"Holiday media folder path is not a directory: {target}")
                        continue
                    target.mkdir(parents=True)
                    created.append(str(target))
        for holiday in sorted(holiday_slugs):
            for scope in scopes:
                for folder in ("episodes", "movies", "commercials", "bumpers", "promos", "filler"):
                    target = media_root / "shared" / "specials" / "holidays" / holiday / scope / folder
                    if target.exists():
                        if not target.is_dir():
                            raise ValueError(f"Shared holiday media folder path is not a directory: {target}")
                        continue
                    target.mkdir(parents=True)
                    created.append(str(target))
        return created

    def _pending_channel_folders(self) -> set[str]:
        if not self.config.database_path.is_file():
            return set()
        # Settings can be used before first-run database migrations.
        with closing(sqlite3.connect(self.config.database_path.as_uri() + "?mode=ro", uri=True)) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_folder_state'").fetchone():
                return set()
            return {row[0] for row in connection.execute("""SELECT c.slug FROM channel_folder_state s
                JOIN channels c ON c.id=s.channel_id WHERE s.missing_since IS NOT NULL""")}

    @staticmethod
    def _coerce(field: dict[str, Any], raw: Any) -> Any:
        kind = field["type"]
        if kind == "boolean":
            if not isinstance(raw, bool):
                raise ValueError(f"{field['label']} must be true or false")
            return raw
        if kind == "number":
            if isinstance(raw, bool):
                raise ValueError(f"{field['label']} must be a number")
            try:
                numeric = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field['label']} must be a number") from exc
            if numeric < field.get("min", numeric) or numeric > field.get("max", numeric):
                raise ValueError(
                    f"{field['label']} must be between {field.get('min')} and {field.get('max')}"
                )
            step = field.get("step", 1)
            return int(numeric) if step == 1 and numeric.is_integer() else numeric
        if kind == "time":
            return SettingsService._time(raw, field["label"])
        value = str(raw).strip()
        if kind == "select" and value not in field["options"]:
            raise ValueError(f"Invalid value for {field['label']}")
        if kind == "color" and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError(f"{field['label']} must be a six-digit color")
        if not value and kind not in {"text", "path"}:
            raise ValueError(f"{field['label']} cannot be empty")
        return value

    @staticmethod
    def _validate_dayparts(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValueError("At least one daypart is required")
        result = []
        slugs = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Invalid daypart")
            slug = str(item.get("slug", "")).strip().casefold()
            if not SLUG_RE.fullmatch(slug) or slug in slugs:
                raise ValueError(f"Invalid or duplicate daypart slug: {slug}")
            start = SettingsService._time(item.get("start"), "daypart start")
            end = SettingsService._time(item.get("end"), "daypart end")
            policy = str(item.get("boundary_policy", "must_fit"))
            if policy not in {"must_fit", "may_overrun"}:
                raise ValueError("Invalid daypart boundary policy")
            result.append({"slug": slug, "start": start, "end": end, "boundary_policy": policy})
            slugs.add(slug)
        for index, item in enumerate(result):
            if item["end"] != result[(index + 1) % len(result)]["start"]:
                raise ValueError("Dayparts must be contiguous and cover the full 24-hour clock")
        return result

    @staticmethod
    def _validate_channel_numbers(raw: Any, guide_number: int) -> dict[str, int]:
        if not isinstance(raw, dict):
            raise ValueError("Channel numbers must be an object")
        result: dict[str, int] = {}
        used = {guide_number}
        for slug, value in raw.items():
            key = str(slug).strip().casefold()
            if not SLUG_RE.fullmatch(key):
                raise ValueError(f"Invalid channel slug: {key}")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid channel number for {key}") from exc
            if not 1 <= number <= 999 or number in used:
                raise ValueError(f"Channel number {number} is invalid or already in use")
            result[key] = number
            used.add(number)
        return result

    @staticmethod
    def _validate_holidays(raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict):
            raise ValueError("Holidays must be an object")
        result = {}
        for slug, item in raw.items():
            key = str(slug).strip().casefold()
            if not SLUG_RE.fullmatch(key) or not isinstance(item, dict):
                raise ValueError(f"Invalid holiday: {key}")
            start = SettingsService._month_day(item.get("start"), "holiday start")
            end = SettingsService._month_day(item.get("end"), "holiday end")
            mode = str(item.get("mode", "boost"))
            if mode not in {"decorate", "boost", "replace"}:
                raise ValueError(f"Invalid holiday mode for {key}")
            weights = item.get("weights", {})
            if not isinstance(weights, dict):
                raise ValueError(f"Invalid weights for {key}")
            clean_weights = {}
            for media_type in ("commercial", "bumper", "promo", "episode", "movie"):
                value = float(weights.get(media_type, 1.0))
                if not 0 <= value <= 100:
                    raise ValueError(f"Invalid {media_type} weight for {key}")
                clean_weights[media_type] = value
            channels = item.get("shared_movie_channels", [])
            if isinstance(channels, str):
                channels = [part.strip() for part in channels.split(",") if part.strip()]
            result[key] = {
                "start": start,
                "end": end,
                "mode": mode,
                "priority": int(item.get("priority", 200)),
                "maximum_per_break": max(0, int(item.get("maximum_per_break", 2))),
                "cooldown_seconds": max(0, int(item.get("cooldown_seconds", 3600))),
                "shared_movie_channels": [str(value).strip().casefold() for value in channels],
                "weights": clean_weights,
            }
        return result

    @staticmethod
    def _validate_windows(raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict):
            raise ValueError("Weekly windows must be an object")
        result = {}
        for slug, item in raw.items():
            key = str(slug).strip().casefold()
            if not SLUG_RE.fullmatch(key) or not isinstance(item, dict):
                raise ValueError(f"Invalid weekly block: {key}")
            weekdays = item.get("weekdays", [])
            if isinstance(weekdays, str):
                weekdays = [part.strip().casefold() for part in weekdays.split(",") if part.strip()]
            if not weekdays or any(day not in WEEKDAYS for day in weekdays):
                raise ValueError(f"Invalid weekdays for {key}")
            channel = str(item.get("channel", "")).strip().casefold()
            collection = str(item.get("collection", key)).strip().casefold()
            if not SLUG_RE.fullmatch(channel) or not SLUG_RE.fullmatch(collection):
                raise ValueError(f"Invalid channel or collection for {key}")
            result[key] = {
                "channel": channel,
                "weekdays": list(dict.fromkeys(weekdays)),
                "start": SettingsService._time(item.get("start"), "weekly block start"),
                "end": SettingsService._time(item.get("end"), "weekly block end"),
                "collection": collection,
                "guide_label": str(item.get("guide_label", key)).strip(),
                "priority": int(item.get("priority", 300)),
                "enabled": bool(item.get("enabled", False)),
            }
            if 'schedule_mode' in item:
                if item['schedule_mode'] not in {'recurring', 'random'}:
                    raise ValueError(f'Invalid weekly schedule mode for {key}')
                result[key]['schedule_mode'] = item['schedule_mode']
        return result

    @staticmethod
    def _time(value: Any, label: str) -> str:
        text = str(value or "").strip()
        try:
            datetime.strptime(text, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {text}") from exc
        return text

    @staticmethod
    def _month_day(value: Any, label: str) -> str:
        text = str(value or "").strip()
        try:
            datetime.strptime(f"2000-{text}", "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {text}") from exc
        return text

