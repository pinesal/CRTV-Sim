from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from retrotv.scheduling import resolve_timezone


SYNTHETIC_SCHEMA_VERSION = 1
DURATION_TOLERANCE_SECONDS = 0.15


class SyntheticChannelError(ValueError):
    """Raised when a published synthetic channel cannot be trusted."""


@dataclass(frozen=True)
class SyntheticChannelManifest:
    channel_root: Path
    manifest_path: Path
    video_path: Path
    generation_id: str
    generated_at: datetime
    valid_until: datetime
    duration_seconds: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class SyntheticChannelStatus:
    ready: bool
    generation_id: str | None
    schema_version: int | None
    age_seconds: float | None
    stale: bool
    using_last_known_good: bool
    manifest_path: Path | None
    video_path: Path | None
    valid_until: datetime | None
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "generation_id": self.generation_id,
            "schema_version": self.schema_version,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "using_last_known_good": self.using_last_known_good,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "video_path": str(self.video_path) if self.video_path else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class SyntheticPlaybackPosition:
    """A wall-clock position in one accepted synthetic channel generation."""

    manifest: SyntheticChannelManifest
    segment_id: str
    segment_title: str
    segment_start_seconds: float
    segment_end_seconds: float
    offset_seconds: float
    starts_at: datetime
    ends_at: datetime

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, (self.ends_at - self.starts_at).total_seconds() - (
            self.offset_seconds - self.segment_start_seconds
        ))


@dataclass(frozen=True)
class SyntheticChannelDefinition:
    slug: str
    display_name: str
    enabled: bool
    channel_number: int | None
    channel_root: Path
    stale_warning_seconds: float
    dashboard_url: str


_READER_REGISTRY: dict[tuple[str, Path], "SyntheticChannelReader"] = {}
_READER_REGISTRY_LOCK = threading.Lock()


def weather_channel_definition(config) -> SyntheticChannelDefinition:
    settings = config.data.get("synthetic_channels", {}).get("weather", {})
    raw_root = str(
        settings.get(
            "root", "./RetroWeather/synthetic-channels/weather"
        )
    )
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_root)))
    root = expanded if expanded.is_absolute() else config.project_root / expanded
    configured_numbers = config.data.get("guide", {}).get("channel_numbers", {})
    raw_number = configured_numbers.get(
        "weather", settings.get("channel_number", 42)
    )
    channel_number = int(raw_number) if raw_number is not None else None
    host = str(settings.get("companion_host", "127.0.0.1")).strip()
    dashboard_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    port = int(settings.get("companion_port", 8775))
    dashboard_url = f"http://{dashboard_host}:{port}/"
    return SyntheticChannelDefinition(
        slug="weather",
        display_name=str(settings.get("display_name", "RetroWeather")).strip()
        or "RetroWeather",
        enabled=bool(settings.get("enabled", False)),
        channel_number=channel_number,
        channel_root=root.resolve(),
        stale_warning_seconds=max(
            0.0, float(settings.get("stale_warning_minutes", 60)) * 60
        ),
        dashboard_url=dashboard_url,
    )


def weather_channel_reader(config) -> "SyntheticChannelReader":
    definition = weather_channel_definition(config)
    key = (definition.slug, definition.channel_root)
    with _READER_REGISTRY_LOCK:
        reader = _READER_REGISTRY.get(key)
        if reader is None:
            reader = SyntheticChannelReader(
                definition.channel_root, expected_slug=definition.slug
            )
            _READER_REGISTRY[key] = reader
        return reader


def weather_channel_available(config, reader=None) -> bool:
    """Only offer Weather for tuning after a playable publication exists.

    Stale but valid last-known-good publications remain usable during outages.
    Enabling the companion is separate from finishing its location/channel setup.
    """
    return bool(
        weather_channel_definition(config).enabled
        and (reader or weather_channel_reader(config)).refresh().ready
    )


class SyntheticChannelReader:
    """Read an atomically published channel without indexing its media."""

    def __init__(self, channel_root: str | Path, *, expected_slug: str | None = None):
        self.channel_root = Path(channel_root).expanduser().resolve()
        self.expected_slug = expected_slug
        self._last_good: SyntheticChannelManifest | None = None
        self._lock = threading.RLock()

    @property
    def last_good(self) -> SyntheticChannelManifest | None:
        return self._last_good

    def refresh(self, *, now: datetime | None = None) -> SyntheticChannelStatus:
        with self._lock:
            observed_at = _aware_now(now)
            try:
                accepted = self._read_current()
            except (OSError, json.JSONDecodeError, SyntheticChannelError) as exc:
                if self._last_good is not None:
                    try:
                        video = self._last_good.video_path
                        usable = video.is_file() and video.stat().st_size > 0
                    except OSError:
                        usable = False
                    if not usable:
                        self._last_good = None
                return self._status(
                    self._last_good,
                    observed_at,
                    using_last_known_good=self._last_good is not None,
                    error=str(exc),
                )
            self._last_good = accepted
            return self._status(
                accepted,
                observed_at,
                using_last_known_good=False,
                error=None,
            )

    def playback_position(
        self, *, now: datetime | None = None
    ) -> SyntheticPlaybackPosition:
        """Resolve the shared virtual-broadcast position for the current clock."""

        with self._lock:
            observed_at = _aware_now(now)
            status = self.refresh(now=observed_at)
            manifest = self._last_good
            if not status.ready or manifest is None:
                raise SyntheticChannelError(
                    status.error or "No validated synthetic channel is available."
                )

            duration = manifest.duration_seconds
            offset = observed_at.timestamp() % duration
            segments = manifest.payload["segments"]
            selected: Mapping[str, Any] | None = None
            effective_end = duration
            for index, raw_segment in enumerate(segments):
                start = float(raw_segment["start_seconds"])
                end = float(raw_segment["end_seconds"])
                if start <= offset < min(end, duration):
                    selected = raw_segment
                    effective_end = min(end, duration)
                    break
                if index == len(segments) - 1 and start <= offset < duration:
                    # Encoded files commonly have a few trailing milliseconds
                    # beyond the authored cue timeline. Treat that validated
                    # tolerance as part of the final segment rather than
                    # creating an unlisted sliver at the loop boundary.
                    selected = raw_segment
                    effective_end = duration
                    break
            if selected is None:
                raise SyntheticChannelError(
                    f"No manifest segment covers playback offset {offset:.3f}s."
                )

            segment_start = float(selected["start_seconds"])
            segment_end = max(segment_start, effective_end)
            starts_at = observed_at - timedelta(seconds=offset - segment_start)
            ends_at = observed_at + timedelta(seconds=segment_end - offset)
            return SyntheticPlaybackPosition(
                manifest=manifest,
                segment_id=str(selected["id"]),
                segment_title=str(selected["title"]),
                segment_start_seconds=segment_start,
                segment_end_seconds=segment_end,
                offset_seconds=offset,
                starts_at=starts_at,
                ends_at=ends_at,
            )

    def _read_current(self) -> SyntheticChannelManifest:
        pointer_path = self.channel_root / "current.json"
        pointer = _read_object(pointer_path, "current pointer")
        _exact_keys(
            pointer,
            "current pointer",
            {"schema_version", "generation_id", "manifest"},
        )
        if pointer["schema_version"] != SYNTHETIC_SCHEMA_VERSION:
            raise SyntheticChannelError("current.schema_version is unsupported.")
        generation_id = _text(pointer["generation_id"], "current.generation_id")
        manifest_path = _safe_file(
            self.channel_root,
            pointer["manifest"],
            "current.manifest",
        )
        return self._validate_manifest(manifest_path, generation_id)

    def _validate_manifest(
        self,
        manifest_path: Path,
        expected_generation_id: str,
    ) -> SyntheticChannelManifest:
        payload = _read_object(manifest_path, "channel manifest")
        _exact_keys(
            payload,
            "manifest",
            {
                "schema_version",
                "channel",
                "generation_id",
                "generated_at",
                "valid_until",
                "timezone",
                "video",
                "duration_seconds",
                "attribution",
                "segments",
            },
        )
        if payload["schema_version"] != SYNTHETIC_SCHEMA_VERSION:
            raise SyntheticChannelError("manifest.schema_version is unsupported.")

        channel = _object(payload["channel"], "manifest.channel")
        _exact_keys(channel, "manifest.channel", {"slug", "display_name"})
        slug = _text(channel["slug"], "manifest.channel.slug")
        _text(channel["display_name"], "manifest.channel.display_name")
        if self.expected_slug is not None and slug != self.expected_slug:
            raise SyntheticChannelError(
                f"manifest.channel.slug is {slug!r}; expected {self.expected_slug!r}."
            )

        generation_id = _text(payload["generation_id"], "manifest.generation_id")
        if generation_id != expected_generation_id:
            raise SyntheticChannelError(
                f"manifest.generation_id is {generation_id!r}; "
                f"expected {expected_generation_id!r}."
            )
        generated_at = _timestamp(payload["generated_at"], "manifest.generated_at")
        valid_until = _timestamp(payload["valid_until"], "manifest.valid_until")
        if valid_until <= generated_at:
            raise SyntheticChannelError(
                "manifest.valid_until must be later than generated_at."
            )
        timezone_name = _text(payload["timezone"], "manifest.timezone")
        try:
            resolve_timezone(timezone_name)
        except ValueError as exc:
            raise SyntheticChannelError(
                "manifest.timezone must be a known IANA timezone."
            ) from exc

        duration = _positive_number(
            payload["duration_seconds"], "manifest.duration_seconds"
        )
        video_path = _safe_file(
            manifest_path.parent,
            payload["video"],
            "manifest.video",
        )

        attribution = payload["attribution"]
        if not isinstance(attribution, list) or not attribution:
            raise SyntheticChannelError(
                "manifest.attribution must be a non-empty array."
            )
        for index, item in enumerate(attribution):
            _text(item, f"manifest.attribution[{index}]")

        segments = payload["segments"]
        if not isinstance(segments, list) or not segments:
            raise SyntheticChannelError("manifest.segments must be a non-empty array.")
        cursor = 0.0
        identifiers: set[str] = set()
        for index, raw_segment in enumerate(segments):
            label = f"manifest.segments[{index}]"
            segment = _object(raw_segment, label)
            _exact_keys(
                segment,
                label,
                {
                    "id",
                    "start_seconds",
                    "end_seconds",
                    "title",
                    "condition_theme",
                    "background",
                },
            )
            identifier = _text(segment["id"], f"{label}.id")
            if identifier in identifiers:
                raise SyntheticChannelError(f"{label}.id is duplicated.")
            identifiers.add(identifier)
            start = _nonnegative_number(
                segment["start_seconds"], f"{label}.start_seconds"
            )
            end = _positive_number(segment["end_seconds"], f"{label}.end_seconds")
            if not math.isclose(start, cursor, abs_tol=1e-6):
                raise SyntheticChannelError(
                    f"{label}.start_seconds must equal the previous end ({cursor:g})."
                )
            if end <= start:
                raise SyntheticChannelError(
                    f"{label}.end_seconds must be later than its start."
                )
            if end > duration + DURATION_TOLERANCE_SECONDS:
                raise SyntheticChannelError(f"{label} extends beyond the video duration.")
            _text(segment["title"], f"{label}.title")
            _text(segment["condition_theme"], f"{label}.condition_theme")
            background = segment["background"]
            if background is not None:
                name = _text(background, f"{label}.background")
                if Path(name).name != name or PureWindowsPath(name).name != name:
                    raise SyntheticChannelError(
                        f"{label}.background must be a filename only."
                    )
            cursor = end
        if abs(duration - cursor) > DURATION_TOLERANCE_SECONDS:
            raise SyntheticChannelError(
                f"Manifest duration ({duration:.3f}s) and timeline "
                f"({cursor:.3f}s) disagree."
            )

        return SyntheticChannelManifest(
            channel_root=self.channel_root,
            manifest_path=manifest_path,
            video_path=video_path,
            generation_id=generation_id,
            generated_at=generated_at,
            valid_until=valid_until,
            duration_seconds=duration,
            payload=dict(payload),
        )

    @staticmethod
    def _status(
        accepted: SyntheticChannelManifest | None,
        now: datetime,
        *,
        using_last_known_good: bool,
        error: str | None,
    ) -> SyntheticChannelStatus:
        if accepted is None:
            return SyntheticChannelStatus(
                ready=False,
                generation_id=None,
                schema_version=None,
                age_seconds=None,
                stale=False,
                using_last_known_good=False,
                manifest_path=None,
                video_path=None,
                valid_until=None,
                error=error,
            )
        age = max(0.0, (now - accepted.generated_at.astimezone(timezone.utc)).total_seconds())
        stale = now > accepted.valid_until.astimezone(timezone.utc)
        return SyntheticChannelStatus(
            ready=True,
            generation_id=accepted.generation_id,
            schema_version=SYNTHETIC_SCHEMA_VERSION,
            age_seconds=round(age, 3),
            stale=stale,
            using_last_known_good=using_last_known_good,
            manifest_path=accepted.manifest_path,
            video_path=accepted.video_path,
            valid_until=accepted.valid_until,
            error=error,
        )


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyntheticChannelError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise SyntheticChannelError(f"Unable to read {label}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SyntheticChannelError(
            f"{label} is invalid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    return _object(raw, label)


def _object(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise SyntheticChannelError(f"{label} must be an object.")
    return raw


def _exact_keys(raw: Mapping[str, Any], label: str, expected: set[str]) -> None:
    missing = expected - raw.keys()
    unknown = raw.keys() - expected
    if missing:
        raise SyntheticChannelError(
            f"{label} is missing: {', '.join(sorted(missing))}."
        )
    if unknown:
        raise SyntheticChannelError(
            f"{label} has unknown fields: {', '.join(sorted(unknown))}."
        )


def _text(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SyntheticChannelError(f"{label} must be non-empty text.")
    return raw


def _number(raw: Any, label: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SyntheticChannelError(f"{label} must be a finite number.")
    result = float(raw)
    if not math.isfinite(result):
        raise SyntheticChannelError(f"{label} must be a finite number.")
    return result


def _positive_number(raw: Any, label: str) -> float:
    result = _number(raw, label)
    if result <= 0:
        raise SyntheticChannelError(f"{label} must be greater than zero.")
    return result


def _nonnegative_number(raw: Any, label: str) -> float:
    result = _number(raw, label)
    if result < 0:
        raise SyntheticChannelError(f"{label} must be zero or greater.")
    return result


def _timestamp(raw: Any, label: str) -> datetime:
    value = _text(raw, label)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyntheticChannelError(f"{label} must be an ISO 8601 timestamp.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SyntheticChannelError(f"{label} must include a UTC offset.")
    return result


def _aware_now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("now must include a UTC offset")
    return result.astimezone(timezone.utc)


def _safe_file(root: Path, raw: Any, label: str) -> Path:
    relative = _text(raw, label)
    candidate = Path(relative)
    windows_candidate = PureWindowsPath(relative)
    normalized_parts = relative.replace("\\", "/").split("/")
    if (
        candidate.is_absolute()
        or windows_candidate.is_absolute()
        or bool(windows_candidate.drive)
        or ".." in normalized_parts
    ):
        raise SyntheticChannelError(f"{label} must be a safe relative path.")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SyntheticChannelError(f"{label} escapes its publication directory.") from exc
    if not resolved.is_file():
        raise SyntheticChannelError(f"{label} references a missing file: {relative}.")
    return resolved
