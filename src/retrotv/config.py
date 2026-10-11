from __future__ import annotations

import copy
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a CRTV Sim profile is missing or invalid."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key not in {"holiday_filename_clues", "bumper_filename_clues"}
            and key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def find_project_root(start: Path) -> Path:
    candidate = start.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return candidate


@dataclass(frozen=True)
class AppConfig:
    profile_path: Path
    project_root: Path
    data: dict[str, Any]
    source_data: dict[str, Any] | None = None

    @property
    def profile_name(self) -> str:
        return str(self.data.get("profile", "development"))

    @property
    def database_path(self) -> Path:
        raw = str(self.data["application"]["database_path"])
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        return path if path.is_absolute() else (self.project_root / path).resolve()

    def storage_root(self, logical_name: str) -> Path:
        roots = self.data.get("storage_roots", {})
        if logical_name not in roots:
            raise ConfigError(f"Missing storage root '{logical_name}'")
        raw = str(roots[logical_name])
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        return path if path.is_absolute() else (self.project_root / path).resolve()

    @property
    def ffprobe(self) -> str:
        portable_root = os.environ.get("RETROTV_PORTABLE_ROOT")
        if portable_root:
            bundled = Path(portable_root) / "tools" / "ffmpeg" / "ffprobe.exe"
            if bundled.is_file():
                return os.fspath(bundled.resolve())
        return str(self.data.get("tools", {}).get("ffprobe", "ffprobe"))

    @property
    def ffmpeg(self) -> str:
        portable_root = os.environ.get("RETROTV_PORTABLE_ROOT")
        if portable_root:
            bundled = Path(portable_root) / "tools" / "ffmpeg" / "ffmpeg.exe"
            if bundled.is_file():
                return os.fspath(bundled.resolve())
        return str(self.data.get("tools", {}).get("ffmpeg", "ffmpeg"))


def load_config(profile_path: str | Path | None = None) -> AppConfig:
    if profile_path is None:
        project_root = find_project_root(Path.cwd())
        local = project_root / "config" / "development.windows.local.toml"
        example = project_root / "config" / "development.windows.example.toml"
        profile = local if local.is_file() else example
    else:
        profile = Path(profile_path).expanduser().resolve()
        project_root = find_project_root(profile)

    profile_data = _read_toml(profile)
    extends = profile_data.pop("extends", None)
    if extends:
        base_path = (profile.parent / str(extends)).resolve()
        data = _deep_merge(_read_toml(base_path), profile_data)
    else:
        data = profile_data

    source_data = copy.deepcopy(data)
    # The Dashboard owns a profile-specific JSON overlay so ordinary settings
    # can be edited without rewriting hand-maintained TOML profiles.
    settings_path = profile.with_suffix(".settings.json")
    if settings_path.is_file():
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid settings override file {settings_path}: {exc}") from exc
        if not isinstance(settings_data, dict):
            raise ConfigError(f"Settings override file must contain an object: {settings_path}")
        data = _deep_merge(data, settings_data)

    required_sections = ("application", "storage_roots")
    missing = [section for section in required_sections if section not in data]
    if missing:
        raise ConfigError(
            "Missing configuration sections: " + ", ".join(sorted(missing))
        )

    config = AppConfig(
        profile_path=profile,
        project_root=project_root,
        data=data,
        source_data=source_data,
    )
    if "media" not in data["storage_roots"]:
        raise ConfigError("Configuration requires storage_roots.media")
    return config
