from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


STILL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MOTION_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"})
SUPPORTED_EXTENSIONS = STILL_EXTENSIONS | MOTION_EXTENSIONS | AUDIO_EXTENSIONS

Probe = Callable[[Path], dict[str, Any]]
_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def scan_asset_library(media_root: Path, probe: Probe | None = None) -> dict[str, Any]:
    """Build a fresh directory inventory while reusing probes for unchanged files."""
    root = media_root.resolve()
    assets: list[dict[str, Any]] = []
    unsupported = 0
    if root.is_dir():
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                unsupported += 1
                continue
            assets.append(_inspect(path, root, probe))

    folders: dict[str, int] = {}
    for asset in assets:
        folder = str(Path(asset["relative_path"]).parent).replace("\\", "/")
        folders[folder] = folders.get(folder, 0) + 1
    summary = {
        "total": len(assets),
        "valid": sum(bool(item["valid"]) for item in assets),
        "invalid": sum(not item["valid"] for item in assets),
        "eligible": sum(bool(item["eligible"]) for item in assets),
        "usable_now": sum(bool(item["usable_now"]) for item in assets),
        "still_images": sum(item["media_type"] == "still" for item in assets),
        "motion_backgrounds": sum(item["media_type"] == "motion" for item in assets),
        "music": sum(item["media_type"] == "audio" for item in assets),
        "holiday_assets": sum(item["role"].startswith("holiday_") for item in assets),
        "unsupported_files": unsupported,
    }
    return {
        "schema_version": 1,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "media_root": str(root),
        "summary": summary,
        "folders": dict(sorted(folders.items())),
        "assets": assets,
    }


def _inspect(path: Path, root: Path, probe: Probe | None) -> dict[str, Any]:
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns)
    resolved = path.resolve()
    with _CACHE_LOCK:
        cached = _CACHE.get(resolved)
    if cached is not None and cached[:2] == signature:
        details = dict(cached[2])
    else:
        details = (probe or _probe_with_ffprobe)(path)
        with _CACHE_LOCK:
            _CACHE[resolved] = (signature[0], signature[1], dict(details))

    relative = path.relative_to(root).as_posix()
    media_type = _media_type(path.suffix.lower())
    role = _role(relative)
    folder_compatible = not (
        (relative.startswith("graphics/") and media_type == "audio")
        or (relative.startswith("music/") and media_type != "audio")
        or not (relative.startswith("graphics/") or relative.startswith("music/"))
    )
    valid = bool(details.get("valid")) and folder_compatible
    error = details.get("error")
    if not folder_compatible:
        error = "File type does not match its graphics or music folder."
    has_alpha = _has_alpha(details.get("pixel_format"))
    if role.startswith("holiday_") and media_type == "still" and valid and not has_alpha:
        valid = False
        error = "Holiday overlays must contain a transparent alpha channel."
    usable_now = valid and (
        (
            media_type in {"still", "motion"}
            and (
                role in {"condition_background", "alert_artwork"}
                or (role.startswith("holiday_") and role.endswith("_artwork"))
            )
        )
        or media_type == "audio"
    )
    return {
        "relative_path": relative,
        "name": path.name,
        "folder": str(Path(relative).parent).replace("\\", "/"),
        "role": role,
        "media_type": media_type,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "valid": valid,
        "eligible": valid and role != "template",
        "usable_now": usable_now,
        "width": details.get("width"),
        "height": details.get("height"),
        "duration_seconds": details.get("duration_seconds"),
        "codec": details.get("codec"),
        "format": details.get("format"),
        "pixel_format": details.get("pixel_format"),
        "has_alpha": has_alpha,
        "error": error,
    }


def _probe_with_ffprobe(path: Path, *, executable: str | None = None) -> dict[str, Any]:
    executable = executable or shutil.which("ffprobe")
    if executable is None:
        return {"valid": False, "error": "FFprobe is not installed or is not on PATH."}
    try:
        completed = subprocess.run(
            [
                executable,
                "-v", "error",
                "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,width,height,duration,pix_fmt",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"valid": False, "error": f"Unable to inspect file: {exc}"}
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()
        return {"valid": False, "error": message[-1] if message else "FFprobe rejected the file."}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"valid": False, "error": "FFprobe returned invalid metadata."}
    streams = payload.get("streams") if isinstance(payload, dict) else None
    streams = streams if isinstance(streams, list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    selected = audio if path.suffix.lower() in AUDIO_EXTENSIONS else video
    if not isinstance(selected, dict):
        return {"valid": False, "error": "The expected media stream is missing."}
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_float(selected.get("duration")) or _positive_float(format_info.get("duration"))
    width = _positive_int(selected.get("width"))
    height = _positive_int(selected.get("height"))
    is_still = path.suffix.lower() in STILL_EXTENSIONS
    valid = bool(width and height) if is_still else bool(duration and (audio is not None or (width and height)))
    return {
        "valid": valid,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "codec": selected.get("codec_name"),
        "format": format_info.get("format_name"),
        "pixel_format": selected.get("pix_fmt"),
        "error": None if valid else "Media has invalid dimensions or duration.",
    }


def _media_type(extension: str) -> str:
    if extension in STILL_EXTENSIONS:
        return "still"
    if extension in MOTION_EXTENSIONS:
        return "motion"
    return "audio"


def _role(relative: str) -> str:
    parts = relative.split("/")
    if parts[:2] == ["graphics", "templates"]:
        return "template"
    if parts[:2] == ["graphics", "station-ids"]:
        return "station_id"
    if parts[:2] == ["graphics", "conditions"]:
        return "condition_background"
    if parts[:2] == ["graphics", "alerts"]:
        return "alert_artwork"
    if len(parts) >= 3 and parts[:2] == ["graphics", "holidays"]:
        return f"holiday_{parts[2]}_artwork"
    if parts[:2] == ["music", "default"]:
        return "default_music"
    if parts[:2] == ["music", "conditions"]:
        return "condition_music"
    if parts[:2] == ["music", "holidays"] and len(parts) >= 3:
        return f"holiday_{parts[2]}_music"
    return "unassigned"


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(number, 3) if number > 0 else None


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _has_alpha(pixel_format: object) -> bool:
    value = str(pixel_format or "").lower()
    return value in {"rgba", "bgra", "argb", "abgr", "ya8", "ya16le", "ya16be"} or value.startswith(("yuva", "gbrap", "rgba", "bgra"))
