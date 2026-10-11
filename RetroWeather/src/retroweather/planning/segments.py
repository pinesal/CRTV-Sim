from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from retroweather.models import NormalizedWeatherSnapshot


PLAN_SCHEMA_VERSION = 1

DEFAULT_SEGMENTS: tuple[tuple[str, str, float], ...] = (
    ("station-id", "Station ID", 5),
    ("current-conditions", "Current Conditions", 12.5),
    ("local-forecast", "Local Forecast", 20),
    ("hourly-outlook", "Hourly Outlook", 25),
    ("tonight-tomorrow", "Tonight / Tomorrow", 17.5),
    ("extended-forecast", "Extended Forecast", 27.5),
    ("return-to-station-id", "Return to Station ID", 12.5),
)


class PlanError(ValueError):
    """Raised when channel settings cannot produce a valid segment plan."""


@dataclass(frozen=True)
class SegmentCue:
    identifier: str
    title: str
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "title": self.title,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class ForecastPlan:
    location: str
    timezone: str
    snapshot_fetched_at: str
    duration_seconds: float
    segments: tuple[SegmentCue, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise PlanError("A forecast plan must contain at least one segment.")
        cursor = 0.0
        identifiers: set[str] = set()
        for segment in self.segments:
            if segment.identifier in identifiers:
                raise PlanError(f"Duplicate segment id: {segment.identifier}")
            identifiers.add(segment.identifier)
            if segment.start_seconds != cursor:
                raise PlanError("Segment cues must be contiguous and begin at zero.")
            if segment.end_seconds <= segment.start_seconds:
                raise PlanError(f"Segment {segment.identifier} must have positive duration.")
            cursor = segment.end_seconds
        if not math.isclose(cursor, self.duration_seconds, abs_tol=1e-9):
            raise PlanError(
                f"Segment timeline is {cursor} seconds but channel duration is "
                f"{self.duration_seconds} seconds."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "location": self.location,
            "timezone": self.timezone,
            "snapshot_fetched_at": self.snapshot_fetched_at,
            "duration_seconds": self.duration_seconds,
            "segments": [segment.to_dict() for segment in self.segments],
        }


def build_forecast_plan(
    snapshot: NormalizedWeatherSnapshot,
    settings: Mapping[str, Any],
) -> ForecastPlan:
    channel = settings.get("channel")
    if not isinstance(channel, Mapping):
        raise PlanError("Settings are missing the channel section.")
    duration = channel.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise PlanError("channel.duration_seconds must be a positive finite number.")

    configured = channel.get("segments")
    definitions: Sequence[Any]
    if configured is None:
        definitions = tuple(
            {"id": identifier, "title": title, "duration_seconds": seconds}
            for identifier, title, seconds in DEFAULT_SEGMENTS
        )
    elif isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        definitions = configured
    else:
        raise PlanError("channel.segments must be an array.")

    cues: list[SegmentCue] = []
    cursor = 0.0
    for index, raw in enumerate(definitions):
        if not isinstance(raw, Mapping):
            raise PlanError(f"channel.segments[{index}] must be an object.")
        identifier = raw.get("id")
        title = raw.get("title")
        seconds = raw.get("duration_seconds")
        if not isinstance(identifier, str) or not identifier.strip():
            raise PlanError(f"channel.segments[{index}].id must be non-empty text.")
        if not isinstance(title, str) or not title.strip():
            raise PlanError(f"channel.segments[{index}].title must be non-empty text.")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise PlanError(
                f"channel.segments[{index}].duration_seconds must be a positive "
                "finite number."
            )
        cues.append(SegmentCue(identifier.strip(), title.strip(), cursor, cursor + seconds))
        cursor += seconds

    return ForecastPlan(
        location=snapshot.location.display_name,
        timezone=snapshot.location.timezone,
        snapshot_fetched_at=snapshot.metadata.fetched_at.isoformat(),
        duration_seconds=duration,
        segments=tuple(cues),
    )
