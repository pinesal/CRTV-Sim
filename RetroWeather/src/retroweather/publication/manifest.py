from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from retroweather.models import NormalizedWeatherSnapshot, SnapshotValidationError


MANIFEST_SCHEMA_VERSION = 1
SNAPSHOT_NAME = "normalized-weather.json"
DURATION_TOLERANCE_SECONDS = 0.15


class ManifestValidationError(ValueError):
    """Raised when a published weather-channel manifest is unsafe or invalid."""


@dataclass(frozen=True)
class ValidatedManifest:
    payload: dict[str, Any]
    video_path: Path
    snapshot_path: Path
    duration_seconds: float
    timeline_duration_seconds: float


def validate_version_manifest(
    version_directory: Path,
    *,
    expected_generation_id: str | None = None,
) -> ValidatedManifest:
    manifest_path = version_directory / "channel.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestValidationError(f"Unable to read channel manifest: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            f"Channel manifest is invalid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    payload = _object(raw, "manifest")
    _keys(
        payload,
        "manifest",
        {
            "schema_version", "channel", "generation_id", "generated_at", "valid_until",
            "timezone", "video", "duration_seconds", "attribution", "segments",
        },
    )
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError("manifest.schema_version is unsupported.")

    channel = _object(payload["channel"], "manifest.channel")
    _keys(channel, "manifest.channel", {"slug", "display_name"})
    _text(channel["slug"], "manifest.channel.slug")
    _text(channel["display_name"], "manifest.channel.display_name")

    generation_id = _text(payload["generation_id"], "manifest.generation_id")
    if expected_generation_id is not None and generation_id != expected_generation_id:
        raise ManifestValidationError(
            f"manifest.generation_id is {generation_id!r}; expected {expected_generation_id!r}."
        )
    generated_at = _timestamp(payload["generated_at"], "manifest.generated_at")
    valid_until = _timestamp(payload["valid_until"], "manifest.valid_until")
    if valid_until <= generated_at:
        raise ManifestValidationError("manifest.valid_until must be later than generated_at.")
    timezone_name = _text(payload["timezone"], "manifest.timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ManifestValidationError("manifest.timezone must be a known IANA timezone.") from exc

    duration = _positive_number(payload["duration_seconds"], "manifest.duration_seconds")
    video_path = _relative_file(
        version_directory, payload["video"], "manifest.video"
    )

    attribution = payload["attribution"]
    if not isinstance(attribution, list) or not attribution:
        raise ManifestValidationError("manifest.attribution must be a non-empty array.")
    for index, item in enumerate(attribution):
        _text(item, f"manifest.attribution[{index}]")

    segments = payload["segments"]
    if not isinstance(segments, list) or not segments:
        raise ManifestValidationError("manifest.segments must be a non-empty array.")
    cursor = 0.0
    identifiers: set[str] = set()
    for index, raw_segment in enumerate(segments):
        path = f"manifest.segments[{index}]"
        segment = _object(raw_segment, path)
        _keys(
            segment,
            path,
            {
                "id", "start_seconds", "end_seconds", "title", "condition_theme",
                "background",
            },
        )
        identifier = _text(segment["id"], f"{path}.id")
        if identifier in identifiers:
            raise ManifestValidationError(f"{path}.id is duplicated.")
        identifiers.add(identifier)
        start = _nonnegative_number(segment["start_seconds"], f"{path}.start_seconds")
        end = _positive_number(segment["end_seconds"], f"{path}.end_seconds")
        if not math.isclose(start, cursor, abs_tol=1e-6):
            raise ManifestValidationError(
                f"{path}.start_seconds must equal the previous end ({cursor:g})."
            )
        if end <= start:
            raise ManifestValidationError(f"{path}.end_seconds must be later than its start.")
        if end > duration + DURATION_TOLERANCE_SECONDS:
            raise ManifestValidationError(f"{path} extends beyond the video duration.")
        _text(segment["title"], f"{path}.title")
        _text(segment["condition_theme"], f"{path}.condition_theme")
        background = segment["background"]
        if background is not None:
            name = _text(background, f"{path}.background")
            if Path(name).name != name:
                raise ManifestValidationError(f"{path}.background must be a filename only.")
        cursor = end
    if abs(duration - cursor) > DURATION_TOLERANCE_SECONDS:
        raise ManifestValidationError(
            f"Manifest duration ({duration:.3f}s) and timeline ({cursor:.3f}s) disagree."
        )

    snapshot_path = version_directory / SNAPSHOT_NAME
    if not snapshot_path.is_file():
        raise ManifestValidationError(f"Published snapshot is missing: {SNAPSHOT_NAME}.")
    try:
        snapshot = NormalizedWeatherSnapshot.load(snapshot_path)
    except (OSError, SnapshotValidationError) as exc:
        raise ManifestValidationError(f"Published snapshot is invalid: {exc}") from exc
    if snapshot.location.timezone != timezone_name:
        raise ManifestValidationError(
            "Published snapshot timezone does not match the channel manifest."
        )

    return ValidatedManifest(
        payload=dict(payload),
        video_path=video_path,
        snapshot_path=snapshot_path,
        duration_seconds=duration,
        timeline_duration_seconds=cursor,
    )


def _relative_file(root: Path, raw: Any, path: str) -> Path:
    relative = _text(raw, path)
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ManifestValidationError(f"{path} must be a safe relative path.")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    if resolved_root not in resolved.parents:
        raise ManifestValidationError(f"{path} escapes the version directory.")
    if not resolved.is_file():
        raise ManifestValidationError(f"{path} references a missing file: {relative}.")
    return resolved


def _object(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ManifestValidationError(f"{path} must be an object.")
    return raw


def _keys(raw: Mapping[str, Any], path: str, expected: set[str]) -> None:
    missing = expected - raw.keys()
    unknown = raw.keys() - expected
    if missing:
        raise ManifestValidationError(f"{path} is missing: {', '.join(sorted(missing))}.")
    if unknown:
        raise ManifestValidationError(f"{path} has unknown fields: {', '.join(sorted(unknown))}.")


def _text(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestValidationError(f"{path} must be non-empty text.")
    return raw


def _number(raw: Any, path: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ManifestValidationError(f"{path} must be a finite number.")
    result = float(raw)
    if not math.isfinite(result):
        raise ManifestValidationError(f"{path} must be a finite number.")
    return result


def _positive_number(raw: Any, path: str) -> float:
    result = _number(raw, path)
    if result <= 0:
        raise ManifestValidationError(f"{path} must be greater than zero.")
    return result


def _nonnegative_number(raw: Any, path: str) -> float:
    result = _number(raw, path)
    if result < 0:
        raise ManifestValidationError(f"{path} must be zero or greater.")
    return result


def _timestamp(raw: Any, path: str) -> datetime:
    text = _text(raw, path)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestValidationError(f"{path} must be an ISO 8601 timestamp.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ManifestValidationError(f"{path} must include a UTC offset.")
    return result
