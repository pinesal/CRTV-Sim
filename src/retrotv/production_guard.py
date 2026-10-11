from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable

from retrotv.config import AppConfig


MARKER_NAME = ".retrotv-production.json"
MARKER_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProductionConflict:
    configured_path: Path
    volume_root: Path
    marker_path: Path


def writable_paths(config: AppConfig) -> list[Path]:
    paths = [config.database_path]
    for logical_name in (
        "media",
        "state",
        "normalized",
        "previews",
        "originals_backup",
    ):
        if logical_name in config.data.get("storage_roots", {}):
            paths.append(config.storage_root(logical_name))
    cache = config.data.get("internet_media", {}).get("cache_directory")
    if cache:
        candidate = Path(os.path.expandvars(os.path.expanduser(str(cache))))
        if candidate.is_absolute():
            paths.append(candidate)
        elif "state" in config.data.get("storage_roots", {}):
            paths.append((config.storage_root("state") / candidate).resolve())
        else:
            paths.append((config.project_root / candidate).resolve())
    unique: dict[str, Path] = {}
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        unique[os.path.normcase(str(resolved))] = resolved
    return list(unique.values())


def volume_root(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    anchor = resolved.anchor
    if not anchor:
        raise ValueError(f"Path has no volume root: {resolved}")
    return Path(anchor)


def marker_path(path: Path) -> Path:
    return volume_root(path) / MARKER_NAME


def _is_production_marker(path: Path) -> bool:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(document, dict)
        and document.get("schema_version") == MARKER_SCHEMA_VERSION
        and document.get("product") == "RetroTV"
        and document.get("environment") == "production"
    )


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mark_production_volumes(
    config: AppConfig, *, portable_root: Path
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    locations: dict[str, Path] = {}
    # The portable folder is user-writable even when Windows denies creating a
    # marker at the root of C:. Development configurations that point into this
    # installation can still discover this fallback marker by walking parents.
    portable_marker = portable_root.expanduser().resolve(strict=False) / MARKER_NAME
    locations[os.path.normcase(str(portable_marker))] = portable_marker
    for path in [portable_root, *writable_paths(config)]:
        root = volume_root(path)
        target = root / MARKER_NAME
        locations[os.path.normcase(str(target))] = target
    for target in locations.values():
        try:
            if target.is_file():
                if _is_production_marker(target):
                    continue
                raise ValueError("an unrelated or invalid marker already exists")
            _write_json_atomic(
                target,
                {
                    "schema_version": MARKER_SCHEMA_VERSION,
                    "product": "RetroTV",
                    "environment": "production",
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "portable_root": str(portable_root.resolve(strict=False)),
                },
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(
                {"volume": str(target.parent), "marker": str(target), "error": str(exc)}
            )
    return failures


def production_conflicts(config: AppConfig) -> list[ProductionConflict]:
    if "development" not in config.profile_name.casefold():
        return []
    conflicts: list[ProductionConflict] = []
    for path in writable_paths(config):
        root = volume_root(path)
        resolved = path.expanduser().resolve(strict=False)
        # Check the configured location and each parent. This finds both the
        # preferred drive-root marker and the portable-folder fallback.
        candidates = [resolved, *resolved.parents]
        for location in candidates:
            target = location / MARKER_NAME
            if _is_production_marker(target):
                conflicts.append(ProductionConflict(path, root, target))
                break
    return conflicts


def format_conflicts(conflicts: Iterable[ProductionConflict]) -> str:
    paths = "\n".join(f"  • {item.configured_path}" for item in conflicts)
    return (
        "This development build is configured to write to a drive marked as "
        "CRTV Sim production storage:\n\n"
        f"{paths}\n\n"
        "Continuing can change production databases or media. Continue for this session?"
    )


def confirm_development_access(conflicts: list[ProductionConflict]) -> bool:
    if not conflicts:
        return True
    message = format_conflicts(conflicts)
    if os.name == "nt":
        try:
            import ctypes

            # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
            return int(
                ctypes.windll.user32.MessageBoxW(
                    0, message, "CRTV Sim Development Warning", 0x00000004 | 0x30 | 0x00010000
                )
            ) == 6
        except (AttributeError, OSError):
            pass
    if not os.isatty(0):
        return False
    return input(message + " [y/N] ").strip().casefold() in {"y", "yes"}
