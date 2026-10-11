from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SETTINGS_SCHEMA_VERSION = 1


class ConfigError(RuntimeError):
    """Raised when portable RetroWeather settings are missing or invalid."""


DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": SETTINGS_SCHEMA_VERSION,
    "application": {"host": "127.0.0.1", "port": 8775},
    "channel": {
        "slug": "weather",
        "display_name": "RetroWeather",
        "duration_seconds": 120,
        "segments": [
            {"id": "station-id", "title": "Station ID", "duration_seconds": 5},
            {
                "id": "current-conditions",
                "title": "Current Conditions",
                "duration_seconds": 12.5,
            },
            {
                "id": "local-forecast",
                "title": "Local Forecast",
                "duration_seconds": 20,
            },
            {
                "id": "hourly-outlook",
                "title": "Hourly Outlook",
                "duration_seconds": 25,
            },
            {
                "id": "tonight-tomorrow",
                "title": "Tonight / Tomorrow",
                "duration_seconds": 17.5,
            },
            {
                "id": "extended-forecast",
                "title": "Extended Forecast",
                "duration_seconds": 27.5,
            },
            {
                "id": "return-to-station-id",
                "title": "Return to Station ID",
                "duration_seconds": 12.5,
            },
        ],
    },
    "units": {"system": "imperial"},
    "music": {
        "enabled": True,
        "volume": 0.28,
        "target_lufs": -18.0,
        "fade_in_seconds": 1.5,
        "fade_out_seconds": 2.0,
        "crossfade_seconds": 1.0,
    },
    "appearance": {
        "panel_opacity": 0.58,
        "panel_border_opacity": 0.46,
        "safe_margin": 28,
        "text_scale": 1.0,
        "palette": {
            "header": "0x071827",
            "panel": "0x071827",
            "panel_alternate": "0x102c44",
            "primary": "0xf8fbff",
            "secondary": "0xb8d8ec",
            "muted": "0x82a9c2",
            "accent": "0x59c7e8",
            "condition": "0x82edb0",
        },
    },
    "location": None,
    "refresh": {
        "automatic": True,
        "data_minutes": 15,
        "render_minutes": 30,
        "provider_timeout_seconds": 20,
        "stale_after_minutes": 90,
        "scheduler_poll_seconds": 5,
        "retry_initial_seconds": 60,
        "retry_max_seconds": 900,
    },
    "retention": {"valid_versions": 3},
    "paths": {
        "cache": "data/cache",
        "state": "data/state",
        "media": "retroweather-media",
        "output": "synthetic-channels/weather",
    },
}


MEDIA_DIRECTORIES = (
    "graphics/templates",
    "graphics/station-ids",
    "graphics/conditions/default/day",
    "graphics/conditions/default/night",
    "graphics/conditions/sunny/day",
    "graphics/conditions/sunny/night",
    "graphics/conditions/partly-cloudy/day",
    "graphics/conditions/partly-cloudy/night",
    "graphics/conditions/cloudy/day",
    "graphics/conditions/cloudy/night",
    "graphics/conditions/rainy/day",
    "graphics/conditions/rainy/night",
    "graphics/conditions/stormy/day",
    "graphics/conditions/stormy/night",
    "graphics/conditions/snowy/day",
    "graphics/conditions/snowy/night",
    "graphics/conditions/foggy/day",
    "graphics/conditions/foggy/night",
    "graphics/conditions/windy/day",
    "graphics/conditions/windy/night",
    "graphics/alerts",
    "graphics/holidays/halloween/overlays",
    "graphics/holidays/halloween/countdown",
    "graphics/holidays/christmas/overlays",
    "graphics/holidays/christmas/countdown",
    "graphics/holidays/christmas/santa-tracker",
    "graphics/holidays/christmas/greetings",
    "music/default",
    "music/conditions/rainy",
    "music/conditions/stormy",
    "music/conditions/snowy",
    "music/conditions/foggy",
    "music/holidays/halloween",
    "music/holidays/christmas",
)


@dataclass(frozen=True)
class AppPaths:
    home: Path

    @classmethod
    def discover(
        cls,
        home: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppPaths":
        environment = os.environ if environ is None else environ
        selected = home or environment.get("RETROWEATHER_HOME") or Path.cwd()
        return cls(Path(selected).expanduser().resolve())

    @property
    def settings(self) -> Path:
        return self.home / "config" / "settings.json"

    @property
    def cache(self) -> Path:
        return self.home / "data" / "cache"

    @property
    def normalized_cache(self) -> Path:
        return self.cache / "normalized"

    @property
    def raw_cache(self) -> Path:
        return self.cache / "raw"

    @property
    def state(self) -> Path:
        return self.home / "data" / "state"

    @property
    def media(self) -> Path:
        return self.home / "retroweather-media"

    @property
    def output(self) -> Path:
        return self.home / "synthetic-channels" / "weather"

    @property
    def versions(self) -> Path:
        return self.output / "versions"

    @property
    def latest_snapshot(self) -> Path:
        return self.normalized_cache / "latest.json"

    @property
    def latest_raw_forecast(self) -> Path:
        return self.raw_cache / "open-meteo-latest.json"

    @property
    def fetch_status(self) -> Path:
        return self.state / "fetch-status.json"

    @property
    def render_status(self) -> Path:
        return self.state / "render-status.json"

    @property
    def scheduler_status(self) -> Path:
        return self.state / "scheduler-status.json"


@dataclass(frozen=True)
class InitializationResult:
    home: Path
    settings: Path
    settings_created: bool
    directories_created: int


def initialize(paths: AppPaths) -> InitializationResult:
    directories = (
        paths.settings.parent,
        paths.raw_cache,
        paths.normalized_cache,
        paths.state,
        paths.versions,
        *(paths.media / relative for relative in MEDIA_DIRECTORIES),
    )
    created = 0
    for directory in directories:
        if not directory.exists():
            created += 1
        directory.mkdir(parents=True, exist_ok=True)

    settings_created = not paths.settings.exists()
    if settings_created:
        _atomic_write_json(paths.settings, copy.deepcopy(DEFAULT_SETTINGS))

    return InitializationResult(
        home=paths.home,
        settings=paths.settings,
        settings_created=settings_created,
        directories_created=created,
    )


def load_settings(paths: AppPaths) -> dict[str, Any]:
    if not paths.settings.is_file():
        raise ConfigError(
            f"RetroWeather is not initialized at {paths.home}. Run `retroweather init`."
        )
    try:
        payload = json.loads(paths.settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to read settings: {exc}") from exc

    if not isinstance(payload, dict):
        raise ConfigError("Settings must contain a JSON object.")
    if payload.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported settings schema: {payload.get('schema_version')!r}"
        )
    application = payload.get("application")
    if not isinstance(application, dict):
        raise ConfigError("Settings are missing the application section.")
    if not isinstance(application.get("host"), str):
        raise ConfigError("application.host must be text.")
    port = application.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ConfigError("application.port must be an integer from 0 through 65535.")
    calendar = paths.home / "config/crtvsim-holidays.json"
    if calendar.is_file():
        try:
            shared = json.loads(calendar.read_text(encoding="utf-8"))
            if shared.get("schema_version") != 1 or not isinstance(shared.get("holidays"), dict):
                raise ValueError("Invalid shared holiday calendar")
            payload["crtvsim_holidays"] = shared["holidays"]
        except (OSError, ValueError, AttributeError) as exc:
            raise ConfigError(f"Unable to read CRTV Sim holiday calendar: {exc}") from exc
    return payload


def save_settings(paths: AppPaths, payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        raise ConfigError(
            f"Cannot save unsupported settings schema: {payload.get('schema_version')!r}"
        )
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    # The TV-owned calendar is loaded from its sidecar, not copied into local
    # settings when the Weather dashboard saves unrelated preferences.
    _atomic_write_json(paths.settings, {k: v for k, v in payload.items() if k != "crtvsim_holidays"})


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
