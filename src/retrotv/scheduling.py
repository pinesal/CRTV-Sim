from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from retrotv.config import AppConfig
from retrotv.db import utc_now
from retrotv.blocks import BlockService
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.bumper_cues import effective_bumper_cue, series_matches, configured_bumper_clues
from retrotv.graphics import GraphicsService
from retrotv.recurring_scopes import ScopePlacement, allocate_recurring_scopes
from retrotv.seasonal import RuleContext, SeasonalRuleEngine


PLACEMENT_CTE = """
WITH placements AS (
    SELECT a.id AS asset_id,a.display_title,a.original_filename,a.series_title,
           a.media_type,a.source_relative_path,a.collection_type,
           a.collection_slug,a.holiday_slug,a.holiday_weekly_slug,
           a.channel_slug AS placement_channel,
           e.channel_slug AS eligibility_channel,e.daypart_slug AS eligibility_daypart,
            e.weight,NULL AS shadow_id,a.source_relative_path AS placement_relative_path,
            a.daypart_slug AS placement_scope
    FROM assets a JOIN asset_eligibility e ON e.asset_id=a.id AND e.enabled=1
    WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed')
      AND a.review_status='ready' AND a.enabled_for_scheduling=1
      AND a.integrity_status IN ('ready','not_scanned')
    UNION ALL
    SELECT a.id AS asset_id,a.display_title,a.original_filename,
           COALESCE(s.series_title,a.series_title),a.media_type,
           a.source_relative_path,s.collection_type,s.collection_slug,
           s.holiday_slug,s.holiday_weekly_slug,s.channel_slug AS placement_channel,
           e.channel_slug AS eligibility_channel,e.daypart_slug AS eligibility_daypart,
            e.weight,s.id AS shadow_id,s.shadow_relative_path AS placement_relative_path,
            s.daypart_slug AS placement_scope
    FROM shadow_files s
    JOIN assets a ON a.id=s.target_asset_id
    JOIN shadow_eligibility e ON e.shadow_id=s.id AND e.enabled=1
    WHERE s.status='ready' AND a.review_status='ready'
      AND a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed')
      AND a.enabled_for_scheduling=1
      AND a.integrity_status IN ('ready','not_scanned')
    UNION ALL
    SELECT a.id,a.display_title,a.original_filename,a.series_title,a.media_type,
           a.source_relative_path,NULL,NULL,NULL,NULL,x.channel_slug,
           e.channel_slug,e.daypart_slug,e.weight,NULL,
           x.channel_slug || '/' || x.scope || '/episodes/' || a.series_title || '/' || a.source_relative_path,
           x.scope
    FROM assets a JOIN storage_roots r ON r.id=a.source_root_id
    JOIN external_sources es ON es.root_id=r.id
    JOIN external_assignments x ON x.root_id=r.id AND x.enabled=1
    JOIN asset_eligibility e ON e.asset_id=a.id AND e.eligibility_source='external:' || x.id AND e.enabled=1
    WHERE r.source_kind='external' AND r.enabled=1 AND es.availability='available'
      AND a.review_status='ready' AND a.enabled_for_scheduling=1
      AND a.integrity_status IN ('ready','not_scanned')
)
"""


@dataclass(frozen=True)
class Daypart:
    slug: str
    start: time
    end: time
    boundary_policy: str = "must_fit"


@dataclass(frozen=True)
class Segment:
    slug: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class BlockOpportunity:
    starts_at: datetime
    window_ends_at: datetime
    daypart_slug: str
    window_slug: str
    window_type: str


@dataclass(frozen=True)
class Candidate:
    asset_id: int
    title: str
    series: str
    media_type: str
    duration_ms: int
    source_relative_path: str
    collection_type: str | None = None
    collection_slug: str | None = None
    weight: float = 1.0
    rule_slug: str | None = None
    cooldown_seconds: int | None = None
    boundary_policy: str | None = None
    probability_override: float | None = None
    bumper_override_position: str | None = None
    bumper_override_series: str | None = None
    placement_scope: str | None = None
    placement_origin: str = "asset"
    recurring_holiday_override: bool = False
    series_folder_name: str | None = None


def _candidate_bumper_cue(item: Candidate, config=None):
    return effective_bumper_cue(
        item.source_relative_path,
        item.bumper_override_position,
        item.bumper_override_series, configured_bumper_clues(config),
    )


def _series_folder_name(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    parts = relative_path.replace("\\", "/").split("/")
    for index, part in enumerate(parts[:-1]):
        if part.casefold() != "episodes" or index + 1 >= len(parts) - 1:
            continue
        folder = parts[index + 1]
        if Path(folder).suffix.casefold() in {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}:
            continue
        return folder
    return None


@dataclass(frozen=True)
class TimelineItem:
    asset_id: int | None
    starts_at: datetime
    ends_at: datetime
    media_start_ms: int | None
    media_end_ms: int | None
    entry_type: str
    title: str
    daypart_slug: str
    sequence_group: int | None
    sequence_position: int
    is_program: bool
    programming_owner: str = "base"
    guide_label: str | None = None
    explanation_json: str = "{}"
    protected_block: bool = False
    guide_listing_title: str | None = None
    programming_block_id: int | None = None
    graphic_asset_id: int | None = None


@dataclass(frozen=True)
class BlockSelection:
    candidate: Candidate
    media_start_ms: int
    nominal_start: datetime


@dataclass(frozen=True)
class GenerationResult:
    generation_id: int
    channel_slug: str
    schedule_date: date
    entry_count: int
    program_count: int
    continuity_seconds: float
    validation_status: str
    validation_detail: str


def _parse_time(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid daypart time: {value!r}") from exc


def _natural_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def _episode_key(value: str) -> tuple[Any, ...]:
    lowered = value.casefold()
    patterns = (
        r"\bs(\d{1,3})[\s._-]*e(\d{1,4})\b",
        r"\bseason[\s._-]*(\d{1,3})[\s._-]*episode[\s._-]*(\d{1,4})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return (0, int(match.group(1)), int(match.group(2)), _natural_key(value))
    match = re.search(r"\bepisode[\s._-]*(\d{1,4})\b", lowered)
    if match:
        return (0, 0, int(match.group(1)), _natural_key(value))
    return (1, 0, 0, _natural_key(value))


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


class _PacificTime(tzinfo):
    """Dependency-free modern US Pacific rules for Windows development."""

    _standard = timedelta(hours=-8)

    @staticmethod
    def _transition(year: int, month: int, target_weekday: int, occurrence: int) -> datetime:
        value = datetime(year, month, 1, 2)
        days = (target_weekday - value.weekday()) % 7 + (occurrence - 1) * 7
        return value + timedelta(days=days)

    def dst(self, value: datetime | None) -> timedelta:
        if value is None:
            return timedelta(0)
        naive = value.replace(tzinfo=None)
        start = self._transition(value.year, 3, 6, 2)  # second Sunday in March
        end = self._transition(value.year, 11, 6, 1)  # first Sunday in November
        return timedelta(hours=1) if start <= naive < end else timedelta(0)

    def utcoffset(self, value: datetime | None) -> timedelta:
        return self._standard + self.dst(value)

    def tzname(self, value: datetime | None) -> str:
        return "PDT" if self.dst(value) else "PST"


def resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "America/Los_Angeles":
            return _PacificTime()
        if name in {"UTC", "Etc/UTC"}:
            from datetime import UTC

            return UTC
        raise ValueError(
            f"Timezone data for {name!r} is unavailable. Install system tzdata "
            "or use a supported platform profile."
        )


class ScheduleBuilder:
    def __init__(self, connection, config: AppConfig):
        self.connection = connection
        self.config = config
        settings = config.data.get("scheduling", {})
        self.settings = settings
        self.timezone = resolve_timezone(
            str(
                settings.get(
                    "timezone",
                    config.data.get("application", {}).get("timezone", "UTC"),
                )
            )
        )
        raw_dayparts = settings.get("dayparts") or [
            {"slug": "morning", "start": "06:00", "end": "12:00"},
            {"slug": "afternoon", "start": "12:00", "end": "17:00"},
            {"slug": "evening", "start": "17:00", "end": "22:00"},
            {"slug": "night", "start": "22:00", "end": "06:00"},
        ]
        self.dayparts = [
            Daypart(
                str(item["slug"]),
                _parse_time(str(item["start"])),
                _parse_time(str(item["end"])),
                str(item.get("boundary_policy", "must_fit")),
            )
            for item in raw_dayparts
        ]
        self.schedule_version = int(settings.get("schedule_version", 1))
        self.commercial_min = int(settings.get("commercials_per_break_min", 2))
        self.commercial_max = int(settings.get("commercials_per_break_max", 3))
        self.gap_fill_commercial_max = max(
            0, int(settings.get("gap_fill_commercials_max", 3))
        )
        self.commercial_cooldown = int(
            settings.get("commercial_cooldown_seconds", 10800)
        )
        self.bumper_cooldown = int(settings.get("bumper_cooldown_seconds", 5400))
        self.opening_bumper_enabled = bool(
            settings.get("opening_bumper_enabled", True)
        )
        self.midroll_enabled = bool(settings.get("midroll_enabled", True))
        self.midroll_min_media_ms = round(
            float(settings.get("midroll_min_media_minutes", 15)) * 60_000
        )
        self.midroll_min_segment_ms = round(
            float(settings.get("midroll_min_segment_minutes", 5)) * 60_000
        )
        self.midroll_max_pod_ms = max(
            30_000,
            round(float(settings.get("midroll_max_pod_minutes", 5)) * 60_000),
        )
        self.midroll_media_types = {
            str(value)
            for value in settings.get("midroll_media_types", ["episode", "movie"])
        }
        self.midroll_media_types.add("movie")
        self.midroll_fallback = str(
            settings.get("midroll_fallback", "midpoint")
        ).casefold()
        self.clock_slot_minutes = max(
            15, int(settings.get("clock_slot_minutes", 30))
        )
        self.short_slot_minutes = max(
            5, int(settings.get("short_slot_minutes", 15))
        )
        self.same_series_pairing = bool(
            settings.get("same_series_pairing", True)
        )
        # The old pairing settings remain fallbacks for profiles created before
        # duration-based programming packages were introduced.
        self.short_program_target_ms = max(
            1,
            round(
                float(
                    settings.get(
                        "short_program_target_minutes",
                        settings.get("short_episode_max_minutes", 20),
                    )
                )
                * 60_000
            ),
        )
        self.short_program_slot_ms = max(
            self.short_program_target_ms,
            round(
                float(
                    settings.get(
                        "short_program_slot_minutes",
                        settings.get("pairing_slot_minutes", self.clock_slot_minutes),
                    )
                )
                * 60_000
            ),
        )
        self.sequence_template = [
            str(value)
            for value in settings.get(
                "sequence_template",
                ["bumper", "commercial_break", "bumper", "episode"],
            )
        ]
        allowed_sequence_items = {"bumper", "promo", "commercial_break", "episode"}
        if (
            not self.sequence_template
            or self.sequence_template[-1] != "episode"
            or self.sequence_template.count("episode") != 1
            or any(item not in allowed_sequence_items for item in self.sequence_template)
        ):
            raise ValueError(
                "Phase 3 sequence_template must contain one final 'episode' and "
                "may otherwise contain bumper, promo, or commercial_break"
            )
        self.rules = SeasonalRuleEngine(self.connection, self.config, self.timezone)
        block_settings = config.data.get("blocks", {})
        self.block_probability = min(
            1.0, max(0.0, float(block_settings.get("selection_probability", 0.30)))
        )
        self.block_max_per_daypart = max(
            0, int(block_settings.get("maximum_per_daypart", 3))
        )
        self.block_repeat_days = max(
            0, int(block_settings.get("repeat_cooldown_days", 30))
        )
        self.block_fallback_when_no_programming = bool(
            block_settings.get("fallback_when_no_programming", True)
        )
        self.block_fallback_relaxes_repeats = bool(
            block_settings.get("fallback_relaxes_repeat_restrictions", True)
        )
        self.block_boundary_policy = str(
            block_settings.get("boundary_policy", "may_overrun")
        )
        self.block_long_daypart_hours = max(
            0.0, float(block_settings.get("long_daypart_threshold_hours", 8))
        )
        self.block_extended_daypart_hours = max(
            self.block_long_daypart_hours,
            float(block_settings.get("extended_daypart_threshold_hours", 12)),
        )
        self.block_overlap_policy = str(
            block_settings.get("overlap_policy", "join_in_progress")
        ).casefold()
        self.block_minimum_join_remaining_ms = max(
            0,
            round(
                float(block_settings.get("minimum_join_remaining_minutes", 60))
                * 60_000
            ),
        )
        self.block_minimum_join_remaining_fraction = min(
            1.0,
            max(
                0.0,
                float(
                    block_settings.get("minimum_join_remaining_fraction", 0.50)
                ),
            ),
        )
        self.blocks = BlockService(self.connection)
        self.breakpoints = BreakpointAnalyzer(self.connection, self.config)
        self._fingerprint_cache: dict[str, str] = {}
        self._recurring_grid_cache: dict[
            tuple[int, date, str], dict[tuple[str, int], str]
        ] = {}
        self._validate_daypart_configuration()

    def ensure_channel(
        self, slug: str, *, display_name: str | None = None, number: int | None = None
    ) -> int:
        self.connection.execute(
            """
            INSERT INTO channels(slug, display_name, channel_number, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(slug) DO UPDATE SET
                display_name = excluded.display_name,
                channel_number = COALESCE(excluded.channel_number, channel_number),
                enabled = 1
            """,
            (slug, display_name or slug.replace("-", " ").title(), number),
        )
        row = self.connection.execute(
            "SELECT id FROM channels WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Unable to create channel {slug}")
        self.connection.commit()
        return int(row["id"])

    def _active_daypart(self, value: time) -> Daypart:
        matches = []
        for daypart in self.dayparts:
            if daypart.start < daypart.end:
                active = daypart.start <= value < daypart.end
            else:
                active = value >= daypart.start or value < daypart.end
            if active:
                matches.append(daypart)
        if len(matches) != 1:
            raise ValueError(
                f"Time {value.isoformat(timespec='minutes')} belongs to "
                f"{len(matches)} dayparts"
            )
        return matches[0]

    def _validate_daypart_configuration(self) -> None:
        # Checking each minute catches overlaps and uncovered ranges, including wraps.
        for minute in range(24 * 60):
            self._active_daypart(time(minute // 60, minute % 60))

    def _opportunities_for_window(
        self,
        starts_at: datetime,
        ends_at: datetime,
        *,
        daypart_slug: str,
        window_slug: str,
        window_type: str,
    ) -> list[BlockOpportunity]:
        if self.block_max_per_daypart <= 0 or ends_at <= starts_at:
            return []
        duration_hours = (ends_at - starts_at).total_seconds() / 3600
        count = 1
        if duration_hours >= self.block_extended_daypart_hours:
            count = 3
        elif duration_hours >= self.block_long_daypart_hours:
            count = 2
        count = min(count, self.block_max_per_daypart)
        duration = ends_at - starts_at
        return [
            BlockOpportunity(
                starts_at + duration * index / count,
                ends_at,
                daypart_slug,
                window_slug,
                window_type,
            )
            for index in range(count)
        ]

    def _block_opportunities_for_date(
        self, channel_slug: str, schedule_date: date
    ) -> dict[datetime, BlockOpportunity]:
        day_start = datetime.combine(schedule_date, time.min, self.timezone)
        day_end = datetime.combine(
            schedule_date + timedelta(days=1), time.min, self.timezone
        )
        opportunities: dict[datetime, BlockOpportunity] = {}

        # Include windows that started yesterday so a cross-midnight daypart's
        # midpoint opportunity (for example 1 AM in an 8 PM-6 AM night) is kept.
        for daypart in self.dayparts:
            for start_date in (schedule_date - timedelta(days=1), schedule_date):
                starts_at = datetime.combine(start_date, daypart.start, self.timezone)
                end_date = (
                    start_date + timedelta(days=1)
                    if daypart.end <= daypart.start
                    else start_date
                )
                ends_at = datetime.combine(end_date, daypart.end, self.timezone)
                if ends_at <= day_start or starts_at >= day_end:
                    continue
                for opportunity in self._opportunities_for_window(
                    starts_at,
                    ends_at,
                    daypart_slug=daypart.slug,
                    window_slug=daypart.slug,
                    window_type="daypart",
                ):
                    if day_start <= opportunity.starts_at < day_end:
                        opportunities[opportunity.starts_at] = opportunity

        # A weekly programming block is its own governing window. When its
        # opening coincides with a daypart opportunity, the more specific
        # weekly opportunity takes precedence.
        for window in self.rules.windows:
            if not window.enabled or window.channel_slug != channel_slug:
                continue
            for start_date in (schedule_date - timedelta(days=1), schedule_date):
                if start_date.weekday() not in window.weekdays:
                    continue
                starts_at, ends_at = window.interval_for_start_date(
                    start_date, self.timezone
                )
                if ends_at <= day_start or starts_at >= day_end:
                    continue
                daypart_slug = self._active_daypart(
                    starts_at.timetz().replace(tzinfo=None)
                ).slug
                for opportunity in self._opportunities_for_window(
                    starts_at,
                    ends_at,
                    daypart_slug=daypart_slug,
                    window_slug=window.slug,
                    window_type="weekly",
                ):
                    if not (day_start <= opportunity.starts_at < day_end):
                        continue
                    active = self.rules.active_owner(
                        channel_slug, opportunity.starts_at
                    )
                    if active is not None and active.slug == window.slug:
                        opportunities[opportunity.starts_at] = opportunity

        # Once a weekly owner is active, its own governing window controls
        # complete-block starts. Do not inherit an unrelated midpoint from the
        # surrounding daypart merely because it falls inside that special.
        for starts_at, opportunity in list(opportunities.items()):
            if opportunity.window_type != "daypart":
                continue
            if self.rules.active_owner(channel_slug, starts_at) is not None:
                del opportunities[starts_at]
        return opportunities

    def segments_for_date(
        self,
        schedule_date: date,
        channel_slug: str | None = None,
        *,
        include_block_opportunities: bool = False,
    ) -> list[Segment]:
        day_start = datetime.combine(schedule_date, time.min, self.timezone)
        day_end = datetime.combine(schedule_date + timedelta(days=1), time.min, self.timezone)
        boundaries = {time.min}
        boundaries.update(daypart.start for daypart in self.dayparts)
        boundaries.update(daypart.end for daypart in self.dayparts)
        points = [
            datetime.combine(schedule_date, boundary, self.timezone)
            for boundary in sorted(boundaries)
        ]
        if channel_slug:
            points.extend(self.rules.boundaries_for_day(channel_slug, schedule_date))
            if include_block_opportunities:
                points.extend(
                    self._block_opportunities_for_date(
                        channel_slug, schedule_date
                    ).keys()
                )
            points.sort()
        if points[0] != day_start:
            points.insert(0, day_start)
        points.append(day_end)
        segments = []
        for starts_at, ends_at in zip(points, points[1:]):
            if starts_at == ends_at:
                continue
            daypart = self._active_daypart(starts_at.timetz().replace(tzinfo=None))
            segments.append(Segment(daypart.slug, starts_at, ends_at))
        return segments

    def _configuration_fingerprint(self, channel_slug: str) -> str:
        cached = self._fingerprint_cache.get(channel_slug)
        if cached is not None:
            return cached
        library_rows = self.connection.execute(
            """
            SELECT a.id, a.modified_ns, a.media_type, a.channel_slug,
                   a.daypart_slug, a.collection_type, a.collection_slug,
                   a.holiday_slug, a.holiday_weekly_slug,
                   a.review_status, a.enabled_for_scheduling,
                   a.metadata_status, a.integrity_status, m.duration_seconds,
                   e.channel_slug AS eligibility_channel,
                   e.daypart_slug AS eligibility_daypart, e.enabled,
                   e.weight, bp.enabled AS block_enabled,
                    bp.boundary_policy, bp.probability_override,
                    bco.position AS bumper_override_position,
                    bco.series_title AS bumper_override_series
            FROM assets a
            LEFT JOIN asset_media_info m ON m.asset_id = a.id
            LEFT JOIN asset_eligibility e ON e.asset_id = a.id
             LEFT JOIN block_profiles bp ON bp.asset_id = a.id
             LEFT JOIN bumper_cue_overrides bco ON bco.asset_id = a.id
            WHERE a.channel_slug = ?
               OR e.channel_slug = ?
               OR e.channel_slug = '*'
            ORDER BY a.id, e.channel_slug, e.daypart_slug
            """,
            (channel_slug, channel_slug),
        ).fetchall()
        graphic_rows = self.connection.execute(
            """
            SELECT ga.id, ga.relative_path, ga.purpose, ga.holiday_slug,
                   ga.daypart_slug, ga.weekly_collection_slug,
                   ga.source_kind, ga.file_size, ga.modified_ns,
                   ga.validation_status, ga.provenance
            FROM graphic_assets ga
            JOIN channels c ON c.id=ga.channel_id
            WHERE c.slug=?
            ORDER BY ga.id
            """,
            (channel_slug,),
        ).fetchall()
        graphic_audio_rows = self.connection.execute(
            """
            SELECT gaa.id,gaa.relative_path,gaa.purpose,gaa.holiday_slug,
                   gaa.daypart_slug,gaa.weekly_collection_slug,
                   gaa.file_size,gaa.modified_ns,gaa.validation_status
            FROM graphic_audio_assets gaa
            JOIN channels c ON c.id=gaa.channel_id
            WHERE c.slug=? ORDER BY gaa.id
            """,
            (channel_slug,),
        ).fetchall()
        breakpoint_rows = self.connection.execute(
            """
            SELECT b.asset_id,b.source_signature,b.status,b.selected_offset_ms,
                   b.selected_method,b.confidence
            FROM asset_break_analyses b JOIN assets a ON a.id=b.asset_id
            WHERE a.channel_slug=?
               OR EXISTS (
                    SELECT 1 FROM asset_eligibility e
                    WHERE e.asset_id=a.id AND e.channel_slug IN (?, '*')
               )
            ORDER BY b.asset_id
            """,
            (channel_slug, channel_slug),
        ).fetchall()
        selected_break_rows = self.connection.execute(
            """SELECT c.asset_id,c.offset_ms,c.method,c.score,c.signals_json
               FROM asset_break_candidates c JOIN assets a ON a.id=c.asset_id
               WHERE c.selected=1 AND (a.channel_slug=? OR EXISTS (
                   SELECT 1 FROM asset_eligibility e WHERE e.asset_id=a.id
                   AND e.channel_slug IN (?, '*')))
               ORDER BY c.asset_id,c.offset_ms""",
            (channel_slug, channel_slug),
        ).fetchall()
        shadow_rows = self.connection.execute(
            """
            SELECT s.id,s.shadow_relative_path,s.target_asset_id,
                   s.target_relative_path,s.target_sha256,s.media_type,
                   s.channel_slug,s.daypart_slug,s.collection_type,
                   s.collection_slug,s.holiday_slug,s.holiday_weekly_slug,s.series_title,
                   s.file_size,s.modified_ns,s.status,e.channel_slug,
                   e.daypart_slug,e.weight,e.enabled
            FROM shadow_files s
            JOIN shadow_eligibility e ON e.shadow_id=s.id
            WHERE s.channel_slug=? OR e.channel_slug IN (?, '*')
            ORDER BY s.id,e.channel_slug,e.daypart_slug
            """,
            (channel_slug, channel_slug),
        ).fetchall()
        promo_settings_rows = self.connection.execute(
            """SELECT p.collection_slug,p.promo_enabled
               FROM announcement_weekly_profiles p
               JOIN channels c ON c.id=p.channel_id
               WHERE c.slug=? ORDER BY p.collection_slug""",
            (channel_slug,),
        ).fetchall()
        payload = {
            "channel": channel_slug,
            "version": self.schedule_version,
            "promo_pool_revision": 2,
            "shared_holiday_episode_revision": 1,
            "dayparts": [
                {
                    "slug": item.slug,
                    "start": item.start.isoformat(),
                    "end": item.end.isoformat(),
                    "boundary": item.boundary_policy,
                }
                for item in self.dayparts
            ],
            "commercial_min": self.commercial_min,
            "commercial_max": self.commercial_max,
            "gap_fill_commercial_max": self.gap_fill_commercial_max,
            "clock_slot_minutes": self.clock_slot_minutes,
            "short_slot_minutes": self.short_slot_minutes,
            "same_series_pairing": self.same_series_pairing,
            "short_program_target_ms": self.short_program_target_ms,
            "short_program_slot_ms": self.short_program_slot_ms,
            "sequence_template": self.sequence_template,
            "opening_bumper_enabled": self.opening_bumper_enabled,
            "midroll_enabled": self.midroll_enabled,
            "midroll_min_media_ms": self.midroll_min_media_ms,
            "midroll_min_segment_ms": self.midroll_min_segment_ms,
            "midroll_max_pod_ms": self.midroll_max_pod_ms,
            "midroll_media_types": sorted(self.midroll_media_types),
            "midroll_fallback": self.midroll_fallback,
            "seasonal": self.config.data.get("seasonal", {}),
            "blocks": self.config.data.get("blocks", {}),
            "announcement_duration": self.config.data.get("announcements", {}).get(
                "opportunity_duration_seconds", 30
            ),
            "graphics_settings": {
                key: self.config.data.get("graphics", {}).get(key)
                for key in (
                    "holidays", "interstitial_duration_seconds",
                    "cooldown_minutes", "standalone_enabled",
                )
            },
            "graphics": [tuple(row) for row in graphic_rows],
            "graphics_audio": [tuple(row) for row in graphic_audio_rows],
            "breakpoints": [tuple(row) for row in breakpoint_rows],
            "selected_breaks": [tuple(row) for row in selected_break_rows],
            "library": [tuple(row) for row in library_rows],
            "shadows": [tuple(row) for row in shadow_rows],
            "weekly_promo_settings": [tuple(row) for row in promo_settings_rows],
        }
        external_rows = self.connection.execute(
            'SELECT r.id,r.enabled,r.absolute_path,s.series_title,s.availability,e.id,e.scope,e.enabled FROM storage_roots r JOIN external_sources s ON s.root_id=r.id JOIN external_assignments e ON e.root_id=r.id WHERE e.channel_slug=? ORDER BY r.id,e.id',
            (channel_slug,),
        ).fetchall()
        if external_rows:
            payload['external_revision'] = 1
            payload['external_sources'] = [tuple(row) for row in external_rows]
            payload['external_assets'] = [tuple(row) for row in self.connection.execute(
                'SELECT a.id,a.source_relative_path,a.display_title FROM assets a WHERE a.source_root_id IN (SELECT root_id FROM external_assignments WHERE channel_slug=?) ORDER BY a.id',
                (channel_slug,),
            )]
        preference = self.connection.execute(
            """
            SELECT recurring_enabled, unique_weekends, presentation_format
            FROM channel_schedule_preferences csp
            JOIN channels c ON c.id=csp.channel_id
            WHERE c.slug=?
            """,
            (channel_slug,),
        ).fetchone()
        if preference is not None:
            payload["recurring_schedule"] = tuple(preference)
            # Bump only participating channel schedules when the recurring
            # template algorithm changes.
            if bool(preference["recurring_enabled"]):
                payload["recurring_grid_revision"] = 12
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._fingerprint_cache[channel_slug] = fingerprint
        return fingerprint

    def _seed(
        self,
        channel_slug: str,
        schedule_date: date,
        reroll_token: str | None = None,
    ) -> str:
        seed = (
            f"{channel_slug}:{schedule_date.isoformat()}:"
            f"v{self.schedule_version}:{self._configuration_fingerprint(channel_slug)}"
        )
        if reroll_token:
            seed += f":reroll:{reroll_token}"
        return seed

    def _candidates(
        self, channel_slug: str, daypart_slug: str, media_types: Iterable[str]
    ) -> list[Candidate]:
        types = tuple(media_types)
        placeholders = ",".join("?" for _ in types)
        rows = self.connection.execute(
            PLACEMENT_CTE + f"""
            SELECT p.asset_id AS id,p.display_title,p.original_filename,p.series_title,
                    p.media_type,p.source_relative_path,p.placement_relative_path,m.duration_seconds,
                    p.placement_scope,p.shadow_id,
                    bp.general_title, bp.boundary_policy, bp.probability_override,
                    bco.position AS bumper_override_position,
                    bco.series_title AS bumper_override_series
            FROM placements p
            JOIN asset_media_info m ON m.asset_id=p.asset_id
             LEFT JOIN block_profiles bp ON bp.asset_id=p.asset_id
             LEFT JOIN bumper_cue_overrides bco ON bco.asset_id=p.asset_id
            WHERE p.collection_type IS NULL
              AND p.holiday_slug IS NULL
              AND p.media_type IN ({placeholders})
              AND (p.media_type != 'complete_block' OR COALESCE(bp.enabled, 1) = 1)
              AND (p.eligibility_channel = ? OR p.eligibility_channel = '*')
              AND (
                    p.eligibility_daypart = ?
                    OR p.eligibility_daypart = 'all-day'
                    OR p.eligibility_daypart IS NULL
                  )
             ORDER BY CASE WHEN p.placement_scope=? THEN 0 ELSE 1 END,
                      p.shadow_id IS NOT NULL,p.series_title,p.placement_relative_path
            """,
            (*types, channel_slug, daypart_slug, daypart_slug),
        ).fetchall()
        result: list[Candidate] = []
        seen: set[int] = set()
        for row in rows:
            asset_id = int(row["id"])
            if asset_id in seen:
                continue
            seen.add(asset_id)
            result.append(Candidate(
                int(row["id"]),
                row["general_title"]
                or row["display_title"]
                or row["original_filename"],
                row["series_title"] or row["display_title"] or row["original_filename"],
                row["media_type"],
                max(1, round(float(row["duration_seconds"]) * 1000)),
                row["source_relative_path"],
                boundary_policy=row["boundary_policy"],
                probability_override=row["probability_override"],
                bumper_override_position=row["bumper_override_position"],
                bumper_override_series=row["bumper_override_series"],
                placement_scope=row["placement_scope"],
                placement_origin="shadow" if row["shadow_id"] is not None else "asset",
                series_folder_name=_series_folder_name(row["placement_relative_path"]),
            ))
        return result

    def _special_candidates(
        self,
        channel_slug: str,
        daypart_slug: str,
        media_types: Iterable[str],
        *,
        collection_type: str,
        collection_slug: str,
        rule_slug: str,
        weights: dict[str, float] | None = None,
        cooldown_seconds: int | None = None,
        allow_shared_movies: bool = False,
        holiday_weekly_slug: str | None = None,
    ) -> list[Candidate]:
        types = tuple(media_types)
        placeholders = ",".join("?" for _ in types)
        rows = self.connection.execute(
            PLACEMENT_CTE + f"""
            SELECT p.asset_id AS id,p.display_title,p.original_filename,p.series_title,
                   p.media_type,p.source_relative_path,p.placement_relative_path,p.collection_type,
                   p.collection_slug,p.placement_channel AS channel_slug,m.duration_seconds,
                    bp.general_title, bp.boundary_policy, bp.probability_override,
                    bco.position AS bumper_override_position,
                    bco.series_title AS bumper_override_series
            FROM placements p
            JOIN asset_media_info m ON m.asset_id=p.asset_id
             LEFT JOIN block_profiles bp ON bp.asset_id=p.asset_id
             LEFT JOIN bumper_cue_overrides bco ON bco.asset_id=p.asset_id
            WHERE p.collection_type = ?
              AND p.collection_slug = ?
              AND (? != 'holiday' OR p.holiday_weekly_slug IS ?)
              AND p.media_type IN ({placeholders})
              AND (p.media_type != 'complete_block' OR COALESCE(bp.enabled, 1) = 1)
              AND (p.eligibility_channel = ? OR p.eligibility_channel = '*')
              AND (
                    p.eligibility_daypart = ?
                    OR p.eligibility_daypart = 'all-day'
                    OR p.eligibility_daypart IS NULL
                  )
              AND (
                    p.placement_channel = ?
                    OR (
                        p.placement_channel IS NULL
                        AND (
                            p.media_type IN ('episode', 'commercial', 'bumper', 'promo',
                                             'station_id', 'short', 'filler')
                            OR (p.media_type = 'movie' AND ?)
                        )
                    )
                  )
            ORDER BY p.shadow_id IS NOT NULL,p.series_title,p.placement_relative_path
            """,
            (
                collection_type,
                collection_slug,
                collection_type,
                holiday_weekly_slug,
                *types,
                channel_slug,
                daypart_slug,
                channel_slug,
                int(allow_shared_movies),
            ),
        ).fetchall()
        media_weights = weights or {}
        result: list[Candidate] = []
        seen: set[int] = set()
        for row in rows:
            asset_id = int(row["id"])
            if asset_id in seen:
                continue
            seen.add(asset_id)
            result.append(Candidate(
                int(row["id"]),
                row["general_title"]
                or row["display_title"]
                or row["original_filename"],
                row["series_title"] or row["display_title"] or row["original_filename"],
                row["media_type"],
                max(1, round(float(row["duration_seconds"]) * 1000)),
                row["source_relative_path"],
                row["collection_type"],
                row["collection_slug"],
                max(0.01, float(media_weights.get(row["media_type"], 1.0))),
                rule_slug,
                cooldown_seconds,
                row["boundary_policy"],
                row["probability_override"],
                row["bumper_override_position"],
                row["bumper_override_series"],
                series_folder_name=_series_folder_name(row["placement_relative_path"]),
            ))
        return result

    def _previous_programs(
        self, channel_id: int, before: datetime
    ) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT COALESCE(a.series_title, a.display_title, a.original_filename)
                       AS series_key,
                   se.asset_id
            FROM schedule_entries se
            JOIN schedule_generations sg ON sg.id = se.generation_id
            JOIN assets a ON a.id = se.asset_id
            WHERE se.channel_id = ?
              AND se.is_program = 1
              AND sg.status IN ('active', 'superseded')
              AND se.ends_at <= ?
            ORDER BY se.ends_at DESC, sg.id DESC
            """,
            (channel_id, _iso(before)),
        ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            result.setdefault(str(row["series_key"]), int(row["asset_id"]))
        checkpoints = self.connection.execute(
            """SELECT context_key,asset_id FROM schedule_history_checkpoints
               WHERE channel_id=? AND context_type='series'
                 AND asset_id IS NOT NULL AND last_used_at <= ?
               ORDER BY last_used_at DESC""",
            (channel_id, _iso(before)),
        ).fetchall()
        for row in checkpoints:
            result.setdefault(str(row["context_key"]), int(row["asset_id"]))
        return result

    def schedule_preferences(self, channel_slug: str) -> dict[str, bool | str]:
        """Return the persisted recurring-lineup and presentation settings."""
        row = self.connection.execute(
            """
            SELECT recurring_enabled, unique_weekends, presentation_format
            FROM channel_schedule_preferences csp
            JOIN channels c ON c.id=csp.channel_id
            WHERE c.slug=?
            """,
            (channel_slug,),
        ).fetchone()
        if row is None:
            return {
                "recurring_enabled": False,
                "unique_weekends": False,
                "presentation_format": "standard",
            }
        recurring = bool(row["recurring_enabled"])
        return {
            "recurring_enabled": recurring,
            "unique_weekends": recurring and bool(row["unique_weekends"]),
            "presentation_format": str(row["presentation_format"] or "standard"),
        }

    def update_schedule_preferences(
        self,
        channel_slug: str,
        *,
        recurring_enabled: bool,
        unique_weekends: bool,
        presentation_format: str | None = None,
    ) -> dict[str, bool | str]:
        """Save a channel's recurring-lineup and presentation behavior.

        Unique weekends has no meaning unless recurrence is enabled, so it is
        always stored disabled when recurrence is off.
        """
        channel_id = self.ensure_channel(channel_slug)
        recurring = bool(recurring_enabled)
        weekends = recurring and bool(unique_weekends)
        if presentation_format is None:
            presentation = str(
                self.schedule_preferences(channel_slug).get(
                    "presentation_format", "standard"
                )
            )
        else:
            presentation = str(presentation_format).strip().casefold()
        if presentation not in {"standard", "content_only"}:
            raise ValueError("Presentation format must be standard or content_only")
        self.connection.execute(
            """
            INSERT INTO channel_schedule_preferences(
                channel_id, recurring_enabled, unique_weekends, presentation_format, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                recurring_enabled=excluded.recurring_enabled,
                unique_weekends=excluded.unique_weekends,
                presentation_format=excluded.presentation_format,
                updated_at=excluded.updated_at
            """,
            (channel_id, int(recurring), int(weekends), presentation, utc_now()),
        )
        self.connection.commit()
        self._fingerprint_cache.pop(channel_slug, None)
        self._recurring_grid_cache.clear()
        return {
            "recurring_enabled": recurring,
            "unique_weekends": weekends,
            "presentation_format": presentation,
        }

    @staticmethod
    def _recurring_template_kind(schedule_date: date, unique_weekends: bool) -> str:
        if not unique_weekends:
            return "daily"
        if schedule_date.weekday() == 5:
            return "saturday"
        if schedule_date.weekday() == 6:
            return "sunday"
        return "weekday"

    @staticmethod
    def _date_matches_recurring_template(source: date, template_kind: str) -> bool:
        if template_kind == "daily":
            return True
        if template_kind == "weekday":
            return source.weekday() < 5
        if template_kind == "saturday":
            return source.weekday() == 5
        return source.weekday() == 6

    def _recurring_grid_for_date(
        self,
        channel_id: int,
        schedule_date: date,
        *,
        unique_weekends: bool,
        template_kind: str | None = None,
    ) -> dict[tuple[str, int], str]:
        """Build a template from prior ordinary programming for this channel.

        Normal recurrence uses the latest base entry from any prior day.  With
        unique weekends enabled, weekdays share a template while Saturday and
        Sunday each use only their own prior ordinary schedules.  Weekly and
        holiday owners are excluded so SNICK and similar specials cannot alter
        a channel's normal lineup.
        """
        template_kind = template_kind or self._recurring_template_kind(
            schedule_date, unique_weekends
        )
        cache_key = (channel_id, schedule_date, template_kind)
        cached = self._recurring_grid_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = self.connection.execute(
            """
            WITH prior_days AS (
                SELECT schedule_date, MAX(id) AS generation_id
                FROM schedule_generations
                WHERE channel_id=?
                  AND schedule_date < ?
                  AND status IN ('active', 'superseded')
                GROUP BY schedule_date
            )
            SELECT sg.schedule_date, se.starts_at, se.daypart_slug,
                   COALESCE(a.series_title, a.display_title, a.original_filename)
                       AS series_key
            FROM prior_days pd
            JOIN schedule_generations sg ON sg.id=pd.generation_id
            JOIN schedule_entries se ON se.generation_id=sg.id
            JOIN assets a ON a.id=se.asset_id
            WHERE se.is_program=1
              -- Complete blocks are temporary programming overrides. They must
              -- never become a channel's inherited recurring-series template.
              AND a.media_type != 'complete_block'
              AND (a.collection_type IS NULL OR a.collection_type != 'holiday')
              AND COALESCE(json_extract(se.explanation_json,
                  '$.selection.collection_type'), '') != 'holiday'
              AND COALESCE(se.programming_owner, 'base')='base'
            ORDER BY sg.schedule_date DESC, se.starts_at, se.sequence_position
            """,
            (channel_id, schedule_date.isoformat()),
        ).fetchall()
        grid: dict[tuple[str, int], str] = {}
        for row in rows:
            source_date = date.fromisoformat(str(row["schedule_date"]))
            if not self._date_matches_recurring_template(source_date, template_kind):
                continue
            starts_at = datetime.fromisoformat(str(row["starts_at"]))
            minutes = starts_at.hour * 60 + starts_at.minute
            slot = (minutes // self.clock_slot_minutes) * self.clock_slot_minutes
            key = (str(row["daypart_slug"]), slot)
            grid.setdefault(key, str(row["series_key"]))
        self._recurring_grid_cache[cache_key] = grid
        return grid

    @staticmethod
    def _empty_recurring_state() -> dict[str, object]:
        return {
            "version": 1,
            "slots": {},
            "borrowed_weekend_slots": {},
            "queues": {},
            "progress": {},
        }

    @staticmethod
    def _recurring_state_hash(state: dict[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _normalize_recurring_state_templates(
        state: dict[str, object], unique_weekends: bool
    ) -> dict[str, object]:
        slots = state.setdefault("slots", {})
        assert isinstance(slots, dict)
        borrowed = state.setdefault("borrowed_weekend_slots", {})
        assert isinstance(borrowed, dict)
        if unique_weekends and not any(str(key).startswith("weekday:") for key in slots):
            for key, value in list(slots.items()):
                if str(key).startswith("daily:"):
                    slots[str(key).replace("daily:", "weekday:", 1)] = dict(value)
        if unique_weekends:
            for key in [key for key in slots if str(key).startswith("daily:")]:
                slots.pop(key, None)
        elif not unique_weekends and not any(str(key).startswith("daily:") for key in slots):
            for key, value in list(slots.items()):
                if str(key).startswith("weekday:"):
                    slots[str(key).replace("weekday:", "daily:", 1)] = dict(value)
        if not unique_weekends:
            for key in [
                key for key in slots
                if str(key).startswith("saturday:") or str(key).startswith("sunday:")
            ]:
                slots.pop(key, None)
            borrowed.clear()
        for seasonal in state.get("holiday_lineups", {}).values():
            normalized = ScheduleBuilder._normalize_recurring_state_templates(
                {"slots": seasonal.get("slots", {})}, unique_weekends)
            allowed = {"weekday", "saturday", "sunday"} if unique_weekends else {"daily"}
            seasonal["slots"] = {key: value for key, value in normalized["slots"].items()
                                 if key.split(":", 1)[0] in allowed}
            for field in ("queues", "spans"):
                values = seasonal.setdefault(field, {})
                source, target = ("daily", "weekday") if unique_weekends else ("weekday", "daily")
                if target not in values and source in values:
                    values[target] = values[source]
                seasonal[field] = {key: value for key, value in values.items() if key in allowed}
        return state

    @staticmethod
    def _recurring_state_key(template: str, daypart: str) -> str:
        return f"{template}:{daypart}"

    def _eligible_base_series_assets(self, channel_slug: str) -> dict[str, set[int]]:
        return {
            series: set(asset_ids)
            for series, asset_ids in self._eligible_base_series_asset_order(channel_slug).items()
        }

    def _recurring_source_scopes(
        self, channel_slug: str, holiday_slug: str | None = None,
        allow_shared_movies: bool = False,
    ) -> list[ScopePlacement]:
        """Return folder scopes before the scanner's eligibility expansion."""
        configured = {item.slug for item in self.dayparts}
        rows = self.connection.execute(
            PLACEMENT_CTE + """
            SELECT COALESCE(p.series_title,p.display_title,p.original_filename) AS series,
                   p.placement_scope,p.eligibility_daypart
            FROM placements p
            JOIN asset_media_info m ON m.asset_id=p.asset_id
            WHERE ((? IS NULL AND p.collection_type IS NULL AND p.holiday_slug IS NULL)
                   OR (p.collection_type='holiday' AND p.collection_slug=?
                       AND p.holiday_weekly_slug IS NULL))
              AND p.media_type IN ('episode','short','movie')
              AND (p.eligibility_channel=? OR p.eligibility_channel='*')
              AND (? IS NULL OR p.placement_channel=? OR
                   (p.placement_channel IS NULL AND (p.media_type!='movie' OR ?)))
            """,
            (holiday_slug, holiday_slug, channel_slug, holiday_slug, channel_slug,
             int(allow_shared_movies)),
        ).fetchall()
        coverage: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            scope = str(row["placement_scope"] or row["eligibility_daypart"] or "all-day")
            target = row["eligibility_daypart"]
            if target is None or target == "all-day":
                dayparts = configured
            elif str(target) in configured:
                dayparts = {str(target)}
            else:
                continue
            if scope == "daytime":
                dayparts = dayparts & {"morning", "afternoon", "evening"}
            elif scope != "all-day":
                dayparts = dayparts & {scope}
            if dayparts:
                coverage.setdefault((str(row["series"]), scope), set()).update(dayparts)
        return [
            ScopePlacement(series, scope, frozenset(dayparts))
            for (series, scope), dayparts in sorted(coverage.items())
        ]

    def _eligible_base_series_asset_order(self, channel_slug: str) -> dict[str, list[int]]:
        result: dict[str, dict[int, str]] = {}
        seen: set[int] = set()
        for daypart in self.dayparts:
            for item in self._candidates(
                channel_slug, daypart.slug, ("episode", "short", "movie")
            ):
                if item.collection_type is not None or item.media_type == "complete_block":
                    continue
                if item.asset_id in seen:
                    continue
                seen.add(item.asset_id)
                result.setdefault(item.series, {})[item.asset_id] = item.source_relative_path
        return {
            series: [asset_id for asset_id, _path in sorted(
                paths.items(), key=lambda pair: _episode_key(pair[1])
            )]
            for series, paths in result.items()
        }

    def _recurring_state_before(
        self,
        channel_id: int,
        channel_slug: str,
        schedule_date: date,
        *,
        unique_weekends: bool,
    ) -> tuple[dict[str, object], str]:
        """Load projected lineup state preceding a date, or seed from old schedules."""
        scoped = any(item.broad for item in self._recurring_source_scopes(channel_slug))
        snapshot = self.connection.execute(
            """SELECT r.state_json,r.state_hash,r.schedule_date
               FROM recurring_lineup_snapshots r
               JOIN schedule_generations sg ON sg.id=r.generation_id
               WHERE r.channel_id=? AND r.schedule_date<? AND sg.status='active'
               ORDER BY r.schedule_date DESC LIMIT 1""",
            (channel_id, schedule_date.isoformat()),
        ).fetchone()
        checkpoint = self.connection.execute(
            """SELECT state_json,state_hash,checkpoint_date AS schedule_date
               FROM recurring_lineup_checkpoints
               WHERE channel_id=? AND checkpoint_date<?""",
            (channel_id, schedule_date.isoformat()),
        ).fetchone()
        if checkpoint is not None and (
            snapshot is None or str(checkpoint["schedule_date"]) > str(snapshot["schedule_date"])
        ):
            snapshot = checkpoint
        if snapshot is not None:
            try:
                state = json.loads(str(snapshot["state_json"]))
                if isinstance(state, dict) and int(state.get("version", 0)) in {1, 2}:
                    if scoped and int(state["version"]) == 1:
                        # Retain projected episode progress, but start the new
                        # cross-daypart lineup without inherited duplicate slots.
                        state = {
                            "version": 2, "slots": {}, "borrowed_weekend_slots": {},
                            "queues": {}, "progress": state.get("progress", {}),
                            "holiday_lineups": state.get("holiday_lineups", {}),
                        }
                    elif not scoped and int(state["version"]) == 2:
                        state = {
                            "version": 1, "slots": {}, "borrowed_weekend_slots": {},
                            "queues": {}, "progress": state.get("progress", {}),
                            "holiday_lineups": state.get("holiday_lineups", {}),
                        }
                    self._normalize_recurring_state_templates(state, unique_weekends)
                    return state, self._recurring_state_hash(state)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        state = self._empty_recurring_state()
        if scoped:
            state["version"] = 2
        patterns = (
            ("weekday", "saturday", "sunday") if unique_weekends else ("daily",)
        )
        if not scoped:
            for pattern in patterns:
                grid = self._recurring_grid_for_date(
                    channel_id,
                    schedule_date,
                    unique_weekends=unique_weekends,
                    template_kind=pattern,
                )
                for (daypart, slot), series in grid.items():
                    slots = state["slots"]
                    assert isinstance(slots, dict)
                    slots.setdefault(self._recurring_state_key(pattern, daypart), {})[
                        str(slot)
                    ] = series

        eligible = self._eligible_base_series_asset_order(channel_slug)
        previous = self._previous_programs(
            channel_id,
            datetime.combine(schedule_date, time.min, self.timezone),
        )
        progress = state["progress"]
        assert isinstance(progress, dict)
        for series, asset_id in previous.items():
            ids = eligible.get(series, [])
            if asset_id not in ids:
                continue
            # Existing schedules do not record a run boundary. Seed the known
            # sequential prefix through the most recent aired episode.
            progress[series] = {
                "last_asset_id": asset_id,
                "seen_asset_ids": ids[: ids.index(asset_id) + 1],
            }
        self._normalize_recurring_state_templates(state, unique_weekends)
        return state, self._recurring_state_hash(state)

    def _recurring_queue(self, state: dict[str, object], daypart: str) -> list[dict[str, object]]:
        queues = state.setdefault("queues", {})
        assert isinstance(queues, dict)
        queue = queues.setdefault(daypart, [])
        assert isinstance(queue, list)
        return queue

    def _recurring_slots(
        self, state: dict[str, object], template: str, daypart: str
    ) -> dict[str, str]:
        slots = state.setdefault("slots", {})
        assert isinstance(slots, dict)
        value = slots.setdefault(self._recurring_state_key(template, daypart), {})
        assert isinstance(value, dict)
        return value

    def _ordinary_recurring_clock_slots(
        self, channel_slug: str, schedule_date: date
    ) -> dict[str, set[str]]:
        """Clock slots fully available to ordinary programming on this date."""
        result: dict[str, set[str]] = {}
        slot_minutes = self.clock_slot_minutes
        for segment in self.segments_for_date(schedule_date, channel_slug):
            context = self.rules.context(channel_slug, segment.starts_at)
            if context.owner is not None:
                continue
            starts = segment.starts_at.hour * 60 + segment.starts_at.minute
            ends = (
                24 * 60 if segment.ends_at.date() > schedule_date
                else segment.ends_at.hour * 60 + segment.ends_at.minute
            )
            first = ((starts + slot_minutes - 1) // slot_minutes) * slot_minutes
            result.setdefault(segment.slug, set()).update(
                str(minute)
                for minute in range(first, ends, slot_minutes)
                if minute + slot_minutes <= ends
            )
        return result

    def _recurring_add_waiting(
        self,
        state: dict[str, object],
        daypart: str,
        series: Iterable[str],
        enqueued_at: str,
        eligible_assets: dict[str, set[int]] | None = None,
    ) -> None:
        eligible_series = set(series)
        assigned: set[str] = set()
        all_slots = state.get("slots", {})
        if isinstance(all_slots, dict):
            for key, slot_map in all_slots.items():
                if str(key).endswith(f":{daypart}") and isinstance(slot_map, dict):
                    assigned.update(str(value) for value in slot_map.values())
        queue = self._recurring_queue(state, daypart)
        progress = state.get("progress", {})
        if eligible_assets is not None:
            def still_waiting(entry: dict[str, object]) -> bool:
                name = str(entry.get("series", ""))
                if name not in eligible_series:
                    return False
                if name not in assigned:
                    return True
                row = progress.get(name, {}) if isinstance(progress, dict) else {}
                seen = (
                    {int(value) for value in row.get("seen_asset_ids", [])}
                    if isinstance(row, dict) else set()
                )
                current = eligible_assets.get(name, set())
                return bool(current) and current.issubset(seen)

            queue[:] = [
                entry for entry in queue
                if isinstance(entry, dict) and still_waiting(entry)
            ]
        waiting = {str(item.get("series")) for item in queue if isinstance(item, dict)}
        additions = sorted(
            name for name in eligible_series if name not in assigned and name not in waiting
        )
        queue.extend({"series": name, "enqueued_at": enqueued_at} for name in additions)

    def _recurring_prepare_slots(
        self,
        state: dict[str, object],
        template: str,
        daypart: str,
        groups: dict[str, list[Candidate]],
        schedule_date: date,
        *,
        unique_weekends: bool,
        all_eligible_assets: dict[str, set[int]],
        available_clock_slots: set[str] | None = None,
        weekend_available_slots: dict[str, set[str]] | None = None,
    ) -> None:
        """Fill genuinely open recurring slots from the per-daypart FIFO queue."""
        slots = self._recurring_slots(state, template, daypart)
        all_slots = state.get("slots", {})
        known_pattern = bool(slots)
        if isinstance(all_slots, dict):
            known_pattern = known_pattern or any(
                str(key).endswith(f":{daypart}") and bool(value)
                for key, value in all_slots.items()
            )
        borrowed = state.setdefault("borrowed_weekend_slots", {})
        assert isinstance(borrowed, dict)
        slot_key = self._recurring_state_key(template, daypart)
        borrowed_slots = {str(value) for value in borrowed.get(slot_key, [])}
        if known_pattern:
            base_series = {
                series for series, items in groups.items()
                if any(item.collection_type is None for item in items)
            }
            self._recurring_add_waiting(
                state, daypart, base_series, schedule_date.isoformat(),
                all_eligible_assets,
            )
        queue = self._recurring_queue(state, daypart)
        progress = state.setdefault("progress", {})
        assert isinstance(progress, dict)

        # A weekday show may fill an unused weekend slot until overflow
        # arrives. Track that borrowing so it does not become a permanent
        # weekend assignment and block later additions.
        if unique_weekends and template in {"saturday", "sunday"}:
            weekday_slots = self._recurring_slots(state, "weekday", daypart)
            capacity = [
                key for key in weekday_slots
                if (available_clock_slots is None or key in available_clock_slots)
                and (key not in slots or key in borrowed_slots)
            ]
            for key in capacity:
                replacement = self._recurring_pop_waiting(
                    queue, groups, progress, all_eligible_assets
                )
                if replacement is None:
                    fallback_series = weekday_slots.get(key)
                    if fallback_series in groups:
                        slots[key] = fallback_series
                        borrowed_slots.add(key)
                    else:
                        slots.pop(key, None)
                        borrowed_slots.discard(key)
                    continue
                slots[key] = replacement
                borrowed_slots.discard(key)
            if borrowed_slots:
                borrowed[slot_key] = sorted(borrowed_slots, key=int)
            else:
                borrowed.pop(slot_key, None)

        # Preserve the existing recurring promotion behavior for duplicate
        # weekday slots, but only after any available unique-weekend capacity
        # has had first access to the overflow queue.
        weekend_capacity_open = False
        if unique_weekends and template == "weekday":
            weekday_slots = self._recurring_slots(state, "weekday", daypart)
            for weekend in ("saturday", "sunday"):
                weekend_slots = self._recurring_slots(state, weekend, daypart)
                weekend_borrowed = {
                    str(value) for value in borrowed.get(
                        self._recurring_state_key(weekend, daypart), []
                    )
                }
                available = (
                    weekend_available_slots.get(weekend)
                    if weekend_available_slots is not None else None
                )
                if any(
                    (available is None or key in available)
                    and (key not in weekend_slots or key in weekend_borrowed)
                    for key in weekday_slots
                ):
                    weekend_capacity_open = True
                    break
        if known_pattern and not weekend_capacity_open and template in {"daily", "weekday"}:
            seen_series: set[str] = set()
            duplicate_slots = []
            for key, series in sorted(slots.items(), key=lambda pair: int(pair[0])):
                if not series:
                    continue
                if series in seen_series:
                    duplicate_slots.append(key)
                else:
                    seen_series.add(series)
            for key in duplicate_slots:
                replacement = self._recurring_pop_waiting(
                    queue, groups, progress, all_eligible_assets
                )
                if replacement is None:
                    break
                slots[key] = replacement

        # Once this pattern has a lineup, a newly open clock slot can use the
        # next waiting series. A first-ever pattern remains random and is
        # learned from the generated schedule below.
        if known_pattern:
            for key in sorted(slots, key=lambda value: int(value)):
                series = slots[key]
                if series in groups:
                    continue
                replacement = self._recurring_pop_waiting(
                    queue, groups, progress, all_eligible_assets
                )
                if replacement is not None:
                    slots[key] = replacement
                    if key in borrowed_slots:
                        borrowed_slots.discard(key)
                        if borrowed_slots:
                            borrowed[slot_key] = sorted(borrowed_slots, key=int)
                        else:
                            borrowed.pop(slot_key, None)

    @staticmethod
    def _eligible_base_series_assets_for_groups(
        groups: dict[str, list[Candidate]],
    ) -> dict[str, set[int]]:
        return {
            series: {
                item.asset_id
                for item in candidates
                if item.collection_type is None and item.media_type != "complete_block"
            }
            for series, candidates in groups.items()
        }

    @staticmethod
    def _recurring_pop_waiting(
        queue: list[dict[str, object]],
        groups: dict[str, list[Candidate]],
        progress: dict[str, object],
        eligible_assets: dict[str, set[int]],
    ) -> str | None:
        for index, item in enumerate(queue):
            series = str(item.get("series", ""))
            if series not in groups:
                continue
            progress_row = progress.get(series)
            if isinstance(progress_row, dict):
                seen = {int(value) for value in progress_row.get("seen_asset_ids", [])}
                # A completed series returning with no newly added episodes
                # begins a rerun. If new episodes exist, keep its cursor and
                # play those unseen files before starting over.
                if eligible_assets.get(series, set()).issubset(seen):
                    progress_row["seen_asset_ids"] = []
            queue.pop(index)
            return series
        return None

    def _recurring_series_for_slot(
        self,
        state: dict[str, object],
        template: str,
        daypart: str,
        cursor: datetime,
        schedule_date: date,
        groups: dict[str, list[Candidate]],
        *,
        all_eligible_assets: dict[str, set[int]],
    ) -> str | None:
        slots = self._recurring_slots(state, template, daypart)
        key = str(
            (cursor.hour * 60 + cursor.minute)
            // self.clock_slot_minutes
            * self.clock_slot_minutes
        )
        queue = self._recurring_queue(state, daypart)
        progress = state.setdefault("progress", {})
        assert isinstance(progress, dict)

        if key not in slots and slots:
            replacement = self._recurring_pop_waiting(
                queue, groups, progress, all_eligible_assets
            )
            if replacement is not None:
                slots[key] = replacement
        series = slots.get(key)
        if series and series not in groups:
            replacement = self._recurring_pop_waiting(
                queue, groups, progress, all_eligible_assets
            )
            if replacement is not None:
                slots[key] = replacement
                series = replacement
            else:
                slots[key] = ""
                series = None
        if series:
            row = progress.get(series)
            if isinstance(row, dict):
                seen = {int(value) for value in row.get("seen_asset_ids", [])}
                available = all_eligible_assets.get(series, set())
                if available and available.issubset(seen):
                    if not any(
                        isinstance(item, dict) and item.get("series") == series
                        for item in queue
                    ):
                        queue.append({"series": series, "enqueued_at": schedule_date.isoformat()})
                    replacement = self._recurring_pop_waiting(
                        queue, groups, progress, all_eligible_assets
                    )
                    if replacement is not None:
                        slots[key] = replacement
                        series = replacement
        return series

    def _holiday_recurring_plans(
        self, channel_slug: str, schedule_date: date, state: dict[str, object],
        template: str, clock_slots: dict[str, set[str]],
    ) -> list[dict[str, Any]]:
        """Allocate seasonal slots and a paced calendar of holiday episodes."""
        seasonal = state.setdefault("holiday_lineups", {})
        remaining = {part: set(keys) for part, keys in clock_slots.items()}
        result = []
        for rule in self.rules.active_holidays(schedule_date):
            if rule.mode == "decorate":
                continue
            saved = seasonal.setdefault(rule.slug, {
                "slots": {}, "queues": {}, "progress": {}, "calendar": {},
            })
            candidates = {
                part.slug: self._special_candidates(
                    channel_slug, part.slug, ("episode", "short", "movie"),
                    collection_type="holiday", collection_slug=rule.slug,
                    rule_slug=rule.slug, allow_shared_movies=rule.allows_shared_movie(channel_slug),
                ) for part in self.dayparts
            }
            catalog: dict[str, dict[int, Candidate]] = {}
            for pool in candidates.values():
                for item in pool:
                    catalog.setdefault(item.series, {})[item.asset_id] = item
            eligible = {series: set(assets) for series, assets in catalog.items()}
            progress = saved["progress"]
            # Completion uses the current eligible catalog. Keep historical
            # progress so temporarily unavailable files can return safely.
            previous = {series: int(row["last_asset_id"])
                        for series, row in progress.items() if row.get("last_asset_id") is not None}
            all_groups, all_pointers = self._series_state(
                [item for pool in catalog.values() for item in pool.values()], previous
            )
            season_start, season_end = self._holiday_season_bounds(rule, schedule_date)
            season_key = season_start.isoformat()
            completed = saved.setdefault("completed", {})
            if saved.get("season_key") != season_key:
                if saved.get("season_key") is not None:
                    for series, is_complete in list(completed.items()):
                        if is_complete and isinstance(progress.get(series), dict):
                            progress[series]["seen_asset_ids"] = []
                # Snapshots made by earlier versions repeatedly reset completed
                # pools. They have no seasonal calendar to resume reliably.
                elif "calendar" not in saved:
                    for row in progress.values():
                        if isinstance(row, dict):
                            row["seen_asset_ids"] = []
                saved["season_key"] = season_key
                saved["calendar"] = {}
                saved["completed"] = {}
                completed = saved["completed"]
            spans = {series: max(1, math.ceil(items[all_pointers[series]].duration_ms
                                             / (self.clock_slot_minutes * 60_000)))
                     for series, items in all_groups.items()}
            prior = {part.slug: saved["slots"].get(self._recurring_state_key(template, part.slug), {})
                     for part in self.dayparts}
            plan, waiting = allocate_recurring_scopes(
                self._recurring_source_scopes(channel_slug, rule.slug,
                                              rule.allows_shared_movie(channel_slug)),
                remaining, prior, saved["queues"].get(template, {}), progress,
                eligible, template, schedule_date.isoformat(), fill_repeats=False,
                slot_spans=spans, clock_slot_minutes=self.clock_slot_minutes,
                reset_completed_runs=False,
            )
            # A matching regular series keeps its normal clock slot. Seasonal
            # assets occupy that slot only on their due dates.
            base_slots = state.get("slots", {})
            if isinstance(base_slots, dict):
                base_folder_names: dict[str, str] = {}
                for daypart in self.dayparts:
                    for item in self._candidates(
                        channel_slug, daypart.slug, ("episode", "short", "movie")
                    ):
                        if item.series_folder_name:
                            base_folder_names.setdefault(item.series, item.series_folder_name)
                for part, assignments in plan.items():
                    holiday_by_folder = {
                        item.series_folder_name: item.series
                        for item in candidates.get(part, [])
                        if item.series_folder_name
                    }
                    for base_slot, base_series in base_slots.get(
                        self._recurring_state_key(template, part), {}
                    ).items():
                        folder_name = base_folder_names.get(str(base_series))
                        holiday_series = holiday_by_folder.get(folder_name)
                        if holiday_series is not None:
                            for old_slot in [slot for slot, series in assignments.items()
                                             if series == holiday_series and slot != str(base_slot)]:
                                assignments.pop(old_slot, None)
                            assignments[str(base_slot)] = holiday_series
            # Keep blocked stable assignments pending, so an event may displace
            # them for one date without rewriting their established times.
            for part, assignments in plan.items():
                stable = dict(assignments)
                for old_slot, series in prior.get(part, {}).items():
                    new_slot = next((key for key, value in assignments.items() if value == series), None)
                    if (series in eligible and old_slot not in remaining.get(part, set())
                            and old_slot not in stable):
                        stable.pop(new_slot, None)
                        stable[old_slot] = series
                saved["slots"][self._recurring_state_key(template, part)] = stable
                for slot, series in assignments.items():
                    remaining[part].difference_update(
                        str(int(slot) + offset * self.clock_slot_minutes)
                        for offset in range(spans[series])
                    )
            saved["queues"][template] = waiting
            saved.setdefault("spans", {})[template] = spans
            assigned_parts: dict[str, set[str]] = {}
            for part, assignments in plan.items():
                assigned_parts.setdefault(part, set()).update(
                    str(series) for series in assignments.values() if series
                )
            for part in self.dayparts:
                assignments = saved["slots"].get(self._recurring_state_key(template, part.slug), {})
                assigned_parts.setdefault(part.slug, set()).update(
                    str(series) for series in assignments.values() if series
                )
            self._prepare_holiday_calendar(
                saved, candidates, schedule_date, season_start, season_end,
                clock_slots, template, assigned_parts,
            )
            pools = {}
            for part, pool in candidates.items():
                groups, pointers = self._series_state(pool, previous)
                pools[part] = (groups, pointers)
            result.append({"slug": rule.slug, "saved": saved, "plan": plan,
                           "pools": pools, "eligible": eligible, "waiting": waiting,
                           "done": set(), "used": set(), "airings": [],
                           "series_today": set()})
        if result:
            shared_series_today: set[str] = set()
            for overlay in result:
                overlay["series_today"] = shared_series_today
        return result

    @staticmethod
    def _holiday_season_bounds(rule, value: date) -> tuple[date, date]:
        start_month, start_day = (int(part) for part in rule.start_month_day.split("-", 1))
        end_month, end_day = (int(part) for part in rule.end_month_day.split("-", 1))
        def season_date(year: int, month: int, day: int) -> date:
            try:
                return date(year, month, day)
            except ValueError:
                # An annual February 29 season boundary falls on February 28
                # in non-leap years.
                if month == 2 and day == 29:
                    return date(year, 2, 28)
                raise
        start_tuple, end_tuple = (start_month, start_day), (end_month, end_day)
        if start_tuple <= end_tuple:
            year = value.year
            start = season_date(year, start_month, start_day)
            end = season_date(year, end_month, end_day)
            return start, end
        # Annual windows crossing New Year belong to the year in which they
        # start, whether the active date is before or after January 1.
        if (value.month, value.day) >= start_tuple:
            start_year, end_year = value.year, value.year + 1
        else:
            start_year, end_year = value.year - 1, value.year
        start = season_date(start_year, start_month, start_day)
        end = season_date(end_year, end_month, end_day)
        return start, end

    @staticmethod
    def _evenly_spaced_dates(count: int, available: list[date]) -> list[date]:
        if count <= 0 or not available:
            return []
        count = min(count, len(available))
        if count == 1:
            return [available[len(available) // 2]]
        indexes = [round((index + 0.5) * len(available) / count - 0.5)
                   for index in range(count)]
        # Rounding can collide for very dense placements; fill deterministically.
        chosen = []
        for index in indexes:
            if index not in chosen:
                chosen.append(index)
        for index in range(len(available)):
            if len(chosen) >= count:
                break
            if index not in chosen:
                chosen.append(index)
        return [available[index] for index in sorted(chosen)]

    def _prepare_holiday_calendar(
        self, saved: dict[str, Any], candidates: dict[str, list[Candidate]],
        schedule_date: date, season_start: date, season_end: date,
        clock_slots: dict[str, set[str]], template: str,
        assigned_parts: dict[str, set[str]],
    ) -> None:
        calendar = saved.setdefault("calendar", {})
        progress = saved.setdefault("progress", {})
        completed = saved.setdefault("completed", {})
        by_series: dict[str, dict[int, set[str]]] = {}
        asset_folder_names: dict[tuple[str, int], str | None] = {}
        for part, pool in candidates.items():
            if not clock_slots.get(part) and not assigned_parts.get(part):
                continue
            for item in pool:
                if item.series not in assigned_parts.get(part, set()):
                    continue
                by_series.setdefault(item.series, {}).setdefault(item.asset_id, set()).add(part)
                asset_folder_names[(item.series, item.asset_id)] = item.series_folder_name

        season_dates = []
        current = max(schedule_date, season_start)
        while current <= season_end:
            season_dates.append(current)
            current += timedelta(days=1)

        daypart_order = {part.slug: index for index, part in enumerate(self.dayparts)}
        for series, eligible in by_series.items():
            row = progress.setdefault(series, {"last_asset_id": None, "seen_asset_ids": []})
            seen = {int(asset_id) for asset_id in row.get("seen_asset_ids", [])}
            asset_ids = set(eligible)
            completed[series] = bool(asset_ids and asset_ids.issubset(seen))
            existing = [item for item in calendar.get(series, [])
                        if int(item.get("asset_id", -1)) in asset_ids
                        and int(item.get("asset_id", -1)) not in seen]
            for item in existing:
                allowed = eligible.get(int(item["asset_id"]), set())
                if item.get("daypart") not in allowed and allowed:
                    item["daypart"] = min(allowed, key=lambda part: daypart_order.get(part, 999))
            # Carry blocked/overdue episodes forward one per date, without
            # stacking missed targets on the same day.
            for item in existing:
                if str(item.get("target_date", "")) < schedule_date.isoformat():
                    item["target_date"] = ""
            occupied = {str(item.get("target_date")) for item in existing
                        if str(item.get("target_date", "")) >= schedule_date.isoformat()}
            for item in existing:
                if item.get("target_date"):
                    continue
                next_date = next((candidate for candidate in season_dates
                                  if candidate.isoformat() not in occupied), None)
                if next_date is not None:
                    item["target_date"] = next_date.isoformat()
                    occupied.add(next_date.isoformat())
            planned_ids = {int(item["asset_id"]) for item in existing if item.get("target_date")}
            unplanned = sorted(
                asset_ids - seen - planned_ids,
                key=lambda asset_id: _episode_key(next(
                    candidate.source_relative_path
                    for pool in candidates.values() for candidate in pool
                    if candidate.asset_id == asset_id
                )),
            )
            free_dates = [candidate for candidate in season_dates
                          if candidate.isoformat() not in occupied]
            targets = self._evenly_spaced_dates(min(len(unplanned), len(free_dates)), free_dates)
            for asset_id, target_date in zip(unplanned, targets):
                allowed_parts = sorted(eligible[asset_id], key=lambda part: daypart_order.get(part, 999))
                counts = {part: sum(1 for item in existing
                                    if item.get("daypart") == part) for part in allowed_parts}
                part = min(allowed_parts, key=lambda candidate: (counts[candidate], daypart_order.get(candidate, 999)))
                existing.append({"asset_id": asset_id, "target_date": target_date.isoformat(),
                                 "daypart": part,
                                 "series_folder_name": asset_folder_names.get((series, asset_id))})
                occupied.add(target_date.isoformat())
            for item in existing:
                item.setdefault("series_folder_name",
                                asset_folder_names.get((series, int(item["asset_id"]))))
            calendar[series] = sorted(existing, key=lambda item: (item.get("target_date", ""),
                                                                  int(item.get("asset_id", 0))))

    def _select_holiday_recurring_program(
        self, overlays: list[dict[str, Any]], part: str, cursor: datetime,
        remaining_ms: int, schedule_date: date,
    ) -> tuple[Candidate | None, dict[str, Any] | None, int]:
        """Select a paced holiday episode when its seasonal date is due."""
        clock = cursor.hour * 60 + cursor.minute
        future = sorted(int(slot) for overlay in overlays
                        for slot in overlay["plan"].get(part, {})
                        if int(slot) > clock and (part, slot) not in overlay["done"])
        budget = min(remaining_ms, (future[0] - clock) * 60_000
                     - cursor.second * 1000 - cursor.microsecond // 1000) if future else remaining_ms
        for overlay in overlays:
            groups, pointers = overlay["pools"].get(part, ({}, {}))
            for slot, series in sorted(overlay["plan"].get(part, {}).items(), key=lambda item: int(item[0])):
                if int(slot) > clock or (part, slot) in overlay["done"]:
                    continue
                if series in overlay["series_today"]:
                    continue
                progress = overlay["saved"]["progress"].get(series, {})
                seen = {int(value) for value in progress.get("seen_asset_ids", [])}
                target = next((item for item in overlay["saved"].get("calendar", {}).get(series, [])
                               if item.get("daypart") == part
                               # No target date means no remaining seasonal date:
                               # keep the episode pending for the next occurrence.
                               and item.get("target_date")
                               and date.fromisoformat(str(item.get("target_date"))) <= schedule_date
                               and int(item.get("asset_id", -1)) not in seen), None)
                if target is None:
                    continue
                selected = next((item for item in groups.get(series, [])
                                 if item.asset_id == int(target["asset_id"])
                                 and item.duration_ms <= budget
                                 and item.asset_id not in overlay["used"]), None)
                if selected is None:
                    # A too-long episode or a fixed event can block this target.
                    # Leave it pending; the next generated date will defer it.
                    continue
                overlay["done"].add((part, slot))
                overlay["series_today"].add(series)
                overlay["current_target"] = target
                overlay["airings"].append({
                    "daypart": part, "slot": int(slot), "series": series,
                    "target_date": target.get("target_date"),
                    "asset_id": selected.asset_id, "actual_start": _iso(cursor),
                })
                return selected, overlay, budget
        return None, None, budget

    @staticmethod
    def _record_holiday_recurring_programs(overlay, programs):
        for item in programs:
            overlay["used"].add(item.asset_id)
            row = overlay["saved"]["progress"].setdefault(
                item.series, {"last_asset_id": None, "seen_asset_ids": []})
            row["last_asset_id"] = item.asset_id
            row["seen_asset_ids"] = sorted(set(row["seen_asset_ids"]) | {item.asset_id})
            eligible = {int(value) for value in overlay["eligible"].get(item.series, set())}
            overlay["saved"].setdefault("completed", {})[item.series] = bool(
                eligible and eligible.issubset(set(row["seen_asset_ids"]))
            )

    def _record_recurring_state(
        self,
        state: dict[str, object],
        timeline: list[TimelineItem],
        channel_slug: str,
        schedule_date: date,
        *,
        unique_weekends: bool,
        all_eligible_assets: dict[str, set[int]],
        scoped_waiting: dict[str, list[dict[str, object]]] | None = None,
        underlying_plan: dict[str, dict[str, str]] | None = None,
    ) -> None:
        asset_rows: dict[int, tuple[str, str | None, str]] = {}
        ids = sorted({int(item.asset_id) for item in timeline if item.asset_id is not None})
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = self.connection.execute(
                f"""SELECT id,COALESCE(series_title,display_title,original_filename) AS series_key,
                           collection_type,media_type
                    FROM assets WHERE id IN ({marks})""",
                ids,
            ).fetchall()
            asset_rows = {
                int(row["id"]): (
                    str(row["series_key"]),
                    row["collection_type"],
                    str(row["media_type"]),
                )
                for row in rows
            }

        progress = state.setdefault("progress", {})
        assert isinstance(progress, dict)
        template = self._recurring_template_kind(schedule_date, unique_weekends)
        scoped = state.get("version") == 2
        if scoped and underlying_plan is not None:
            # Holiday and full-block overrides must not erase the normal grid.
            slots = state.setdefault("slots", {})
            for part, assignments in underlying_plan.items():
                key = self._recurring_state_key(template, part)
                if assignments or key in slots:
                    slots[key] = dict(assignments)
        elif scoped:
            slots = state.setdefault("slots", {})
            assert isinstance(slots, dict)
            for key in list(slots):
                if str(key).startswith(f"{template}:"):
                    slots.pop(key)
        for item in timeline:
            if not item.is_program or item.asset_id is None:
                continue
            asset_id = int(item.asset_id)
            info = asset_rows.get(asset_id)
            if info is None:
                continue
            series, collection_type, media_type = info
            selection = json.loads(item.explanation_json or "{}").get("selection", {})
            series = str(selection.get("series", series))
            collection_type = selection.get("collection_type", collection_type)
            if (
                item.programming_owner not in (None, "base")
                or collection_type is not None
                or media_type == "complete_block"
            ):
                continue
            progress_row = progress.setdefault(
                series, {"last_asset_id": None, "seen_asset_ids": []}
            )
            assert isinstance(progress_row, dict)
            seen = {int(value) for value in progress_row.get("seen_asset_ids", [])}
            seen.add(asset_id)
            progress_row["last_asset_id"] = asset_id
            progress_row["seen_asset_ids"] = sorted(seen)

            # Learn newly generated clock slots without changing established
            # assignments. Multiple legacy placements remain recognizable.
            slots = self._recurring_slots(state, template, item.daypart_slug)
            clock_key = str(
                (item.starts_at.hour * 60 + item.starts_at.minute)
                // self.clock_slot_minutes
                * self.clock_slot_minutes
            )
            slots.setdefault(clock_key, series)

        if scoped:
            placements = self._recurring_source_scopes(channel_slug)
            if unique_weekends and template in {"saturday", "sunday"}:
                # A schedule first generated on a weekend still establishes a
                # weekday baseline. Subsequent weekend allocations can then
                # use the shows that missed that baseline before repeating it.
                all_slots = state.get("slots", {})
                assert isinstance(all_slots, dict)
                for key, assignments in list(all_slots.items()):
                    if not str(key).startswith(f"{template}:") or not assignments:
                        continue
                    part = str(key).split(":", 1)[1]
                    weekday = self._recurring_slots(state, "weekday", part)
                    if not weekday:
                        weekday.update(assignments)
            exact = {
                (item.series, part)
                for item in placements if not item.broad
                for part in item.dayparts
            }
            all_slots = state.get("slots", {})
            assert isinstance(all_slots, dict)
            assigned = {
                (str(series), str(key).split(":", 1)[1])
                for key, assignments in all_slots.items()
                if isinstance(assignments, dict) and ":" in str(key)
                for series in assignments.values() if series
            }
            previous_waiting = scoped_waiting or {}
            queues: dict[str, list[dict[str, object]]] = {}
            queued_series: set[str] = set()
            for placement in sorted(
                placements, key=lambda item: (item.broad, item.series, item.scope)
            ):
                if placement.series in queued_series:
                    continue
                compatible = sorted(
                    part for part in placement.dayparts
                    if not placement.broad or (placement.series, part) not in exact
                )
                if not compatible or any(
                    (placement.series, part) in assigned for part in compatible
                ):
                    continue
                prior = next(
                    (
                        (part, entry)
                        for part, entries in previous_waiting.items()
                        for entry in entries
                        if entry.get("series") == placement.series
                        and entry.get("scope") == placement.scope
                    ),
                    None,
                )
                daypart = (
                    prior[0] if prior and prior[0] in compatible
                    else min(compatible, key=lambda part: (len(queues.get(part, [])), part))
                )
                queues.setdefault(daypart, []).append({
                    "series": placement.series,
                    "scope": placement.scope,
                    "enqueued_at": (
                        str(prior[1].get("enqueued_at")) if prior
                        else schedule_date.isoformat()
                    ),
                })
                queued_series.add(placement.series)
            state["queues"] = queues
            return

        # An installation can first generate a schedule on a weekend. Seed
        # the weekday baseline from that first ordinary lineup so the next
        # weekday does not independently claim the same overflow series.
        if unique_weekends and template in {"saturday", "sunday"}:
            all_slots = state.get("slots", {})
            borrowed = state.setdefault("borrowed_weekend_slots", {})
            assert isinstance(all_slots, dict) and isinstance(borrowed, dict)
            for state_key, assignments in list(all_slots.items()):
                if not str(state_key).startswith(f"{template}:") or not assignments:
                    continue
                daypart = str(state_key).split(":", 1)[1]
                weekday_slots = self._recurring_slots(state, "weekday", daypart)
                if weekday_slots:
                    continue
                weekday_slots.update(assignments)
                borrowed[state_key] = sorted(assignments, key=int)

        # Newly discovered series enter their daypart queue only if no recurring
        # pattern already gives them a slot.
        for daypart in self.dayparts:
            candidates = self._candidates(
                channel_slug, daypart.slug, ("episode", "short", "movie")
            )
            eligible_series = {
                item.series for item in candidates if item.collection_type is None
            }
            self._recurring_add_waiting(
                state, daypart.slug, eligible_series, schedule_date.isoformat(),
                all_eligible_assets,
            )

        # Completion is based on scheduled airings across the channel, not on
        # viewing. Append a finished series behind those already waiting; its
        # existing slots transfer when their next recurring airing is planned.
        completed = []
        for series, asset_ids in all_eligible_assets.items():
            row = progress.get(series)
            if not isinstance(row, dict) or not asset_ids:
                continue
            seen = {int(value) for value in row.get("seen_asset_ids", [])}
            if asset_ids.issubset(seen):
                completed.append(series)
        slots = state.get("slots", {})
        queues = state.get("queues", {})
        if isinstance(slots, dict) and isinstance(queues, dict):
            for series in sorted(completed):
                for state_key, assignments in slots.items():
                    if not isinstance(assignments, dict):
                        continue
                    daypart = str(state_key).split(":", 1)[-1]
                    matched = [key for key, value in assignments.items() if value == series]
                    if not matched:
                        continue
                    queue = self._recurring_queue(state, daypart)
                    if not any(
                        isinstance(entry, dict) and entry.get("series") == series
                        for entry in queue
                    ):
                        queue.append({"series": series, "enqueued_at": schedule_date.isoformat()})

    def recurring_lineup(self, channel_slug: str) -> dict[str, object]:
        """Return read-only recurring assignments and FIFO queues for Dashboard."""
        channel = self.connection.execute(
            "SELECT id FROM channels WHERE slug=?", (channel_slug,)
        ).fetchone()
        if channel is None:
            return {"enabled": False, "unique_weekends": False, "patterns": {}, "waiting": {}}
        channel_id = int(channel["id"])
        preferences = self.schedule_preferences(channel_slug)
        if not preferences["recurring_enabled"]:
            return {"enabled": False, "unique_weekends": False, "patterns": {}, "waiting": {}}
        now = datetime.now(self.timezone).date()
        eligible_assets = self._eligible_base_series_assets(channel_slug)
        state, _ = self._recurring_state_before(
            channel_id, channel_slug, now + timedelta(days=1),
            unique_weekends=bool(preferences["unique_weekends"]),
        )
        if state.get("version") != 2:
            for daypart in self.dayparts:
                candidates = self._candidates(
                    channel_slug, daypart.slug, ("episode", "short", "movie")
                )
                self._recurring_add_waiting(
                    state,
                    daypart.slug,
                    {item.series for item in candidates if item.collection_type is None},
                    now.isoformat(),
                    eligible_assets,
                )
        patterns: dict[str, dict[str, list[dict[str, object]]]] = {}
        borrowed = state.get("borrowed_weekend_slots", {})
        for key, assignments in state.get("slots", {}).items():
            template, daypart = str(key).split(":", 1)
            borrowed_keys = (
                {str(value) for value in borrowed.get(key, [])}
                if isinstance(borrowed, dict) else set()
            )
            patterns.setdefault(template, {})[daypart] = [
                {
                    "slot": int(slot),
                    "series": series or "Open slot",
                    "weekday_fallback": str(slot) in borrowed_keys,
                }
                for slot, series in sorted(
                    assignments.items(), key=lambda pair: int(pair[0])
                )
            ]
        waiting = {
            daypart: [str(item.get("series")) for item in queue if isinstance(item, dict)]
            for daypart, queue in state.get("queues", {}).items()
        }
        active = []
        fallback = json.loads(json.dumps(patterns))
        for rule in self.rules.active_holidays(now):
            if rule.mode == "decorate":
                continue
            saved = state.get("holiday_lineups", {}).get(rule.slug, {})
            holiday_patterns = {}
            for key, assignments in saved.get("slots", {}).items():
                template, part = key.split(":", 1)
                holiday_patterns.setdefault(template, {})[part] = [
                    {"slot": int(slot), "series": series}
                    for slot, series in sorted(assignments.items(), key=lambda item: int(item[0]))
                ]
            if saved.get("last_date") == now.isoformat():
                template = self._recurring_template_kind(now, bool(preferences["unique_weekends"]))
                for airing in saved.get("airings", []):
                    if not airing.get("actual_end"):
                        continue
                    start = datetime.fromisoformat(airing["actual_start"])
                    end = datetime.fromisoformat(airing["actual_end"])
                    first = (start.hour * 60 + start.minute) // self.clock_slot_minutes * self.clock_slot_minutes
                    last = 1440 if end.date() > now else end.hour * 60 + end.minute
                    part = airing["daypart"]
                    if part in fallback.get(template, {}):
                        fallback[template][part] = [row for row in fallback[template][part]
                                                   if not first <= row["slot"] < last]
            upcoming = [
                {"series": series, **item}
                for series, entries in saved.get("calendar", {}).items()
                for item in entries
                if str(item.get("target_date", "")) >= now.isoformat()
                and int(item.get("asset_id", -1)) not in {
                    int(value) for value in saved.get("progress", {}).get(series, {}).get("seen_asset_ids", [])
                }
            ]
            active.append({"slug": rule.slug, "patterns": holiday_patterns,
                           "waiting": {template: {part: [row["series"] for row in queue]
                                        for part, queue in queues.items()}
                                       for template, queues in saved.get("queues", {}).items()},
                           "last_date": saved.get("last_date"),
                           "airings": saved.get("airings", []),
                           "upcoming_airings": sorted(upcoming, key=lambda item: (
                               item.get("target_date", ""), item.get("daypart", ""), item.get("series", ""))),
                           "deferred": saved.get("deferred", [])})
        resumes = None
        if active:
            for offset in range(1, 367):
                target = now + timedelta(days=offset)
                if not any(rule.mode != "decorate" for rule in self.rules.active_holidays(target)):
                    resumes = target.isoformat()
                    break
        return {
            "enabled": True,
            "unique_weekends": bool(preferences["unique_weekends"]),
            "patterns": patterns,
            "waiting": waiting,
            "holidays": active,
            "regular_fallback_patterns": fallback,
            "normal_resumes_on": resumes,
        }

    def _recurring_grid_preference(
        self,
        channel_id: int,
        channel_slug: str,
        schedule_date: date,
        daypart_slug: str,
        cursor: datetime,
        context: RuleContext,
    ) -> str | None:
        preferences = self.schedule_preferences(channel_slug)
        if not preferences["recurring_enabled"]:
            return None
        # A weekly special or holiday has its own identity and media rules.  It
        # must remain fully authoritative over the base-channel grid.
        if context.owner is not None:
            return None
        minutes = cursor.hour * 60 + cursor.minute
        slot = (minutes // self.clock_slot_minutes) * self.clock_slot_minutes
        return self._recurring_grid_for_date(
            channel_id,
            schedule_date,
            unique_weekends=preferences["unique_weekends"],
        ).get(
            (daypart_slug, slot)
        )

    def _recurring_promotion_preference(
        self,
        channel_id: int,
        channel_slug: str,
        schedule_date: date,
        daypart_slug: str,
        cursor: datetime,
        context: RuleContext,
        groups: dict[str, list[Candidate]],
    ) -> str | None:
        """Permanently promote one missing series into a duplicate core slot.

        A recurring grid deliberately keeps its established series placement.
        Without an explicit escape hatch, though, a newly imported series could
        never enter a full daypart. When the inherited grid repeats a series,
        one later duplicate slot becomes a permanent promotion slot for an
        unrepresented series. This preserves at least one existing placement
        for every represented show while making room for the new one. If every
        slot is already unique, no show is displaced; the Dashboard instead
        reports the series as unplaced so the owner can decide what to move.
        """
        preferences = self.schedule_preferences(channel_slug)
        if not preferences["recurring_enabled"]:
            return None
        if context.owner is not None or context.holidays:
            return None
        grid = self._recurring_grid_for_date(
            channel_id,
            schedule_date,
            unique_weekends=preferences["unique_weekends"],
        )
        slots_by_series: dict[str, list[int]] = {}
        for (grid_daypart, slot), series in grid.items():
            if grid_daypart == daypart_slug:
                slots_by_series.setdefault(series, []).append(slot)
        if not slots_by_series:
            # The first generated pattern should remain genuinely random.
            return None
        represented = set(slots_by_series)
        overflow = sorted(series for series in groups if series not in represented)
        if not overflow:
            return None
        # Keep the first placement of each series and make only later repeated
        # slots available. A series with three slots can therefore make room
        # for two new series over successive schedule generations.
        promotion_slots = sorted(
            slot
            for slots in slots_by_series.values()
            for slot in sorted(slots)[1:]
        )
        if not promotion_slots:
            return None
        minutes = cursor.hour * 60 + cursor.minute
        current_slot = (minutes // self.clock_slot_minutes) * self.clock_slot_minutes
        token = (
            f"{channel_id}:{daypart_slug}:"
            f"{','.join(sorted(represented))}:{','.join(overflow)}"
        )
        promotion_value = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:16], 16)
        promotion_slot = promotion_slots[promotion_value % len(promotion_slots)]
        if current_slot != promotion_slot:
            return None
        return overflow[(promotion_value // len(promotion_slots)) % len(overflow)]

    @staticmethod
    def _series_state(
        candidates: list[Candidate], previous: dict[str, int]
    ) -> tuple[dict[str, list[Candidate]], dict[str, int]]:
        groups: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            groups.setdefault(candidate.series, []).append(candidate)
        pointers: dict[str, int] = {}
        for series, items in groups.items():
            items.sort(key=lambda item: _episode_key(item.source_relative_path))
            last_id = previous.get(series)
            if last_id is None:
                pointers[series] = 0
                continue
            index = next(
                (idx for idx, item in enumerate(items) if item.asset_id == last_id),
                -1,
            )
            pointers[series] = (index + 1) % len(items)
        return groups, pointers

    @staticmethod
    def _select_program(
        groups: dict[str, list[Candidate]],
        pointers: dict[str, int],
        series_rotation: list[str],
        rotation_index: int,
        remaining_ms: int,
        used_assets: set[int],
        preferred_series: str | None = None,
    ) -> tuple[Candidate | None, int]:
        if not series_rotation:
            return None, rotation_index
        all_candidates = [item for series in series_rotation for item in groups[series]]
        unused_exists = any(item.asset_id not in used_assets for item in all_candidates)
        indexes: list[int] = []
        if preferred_series in groups:
            preferred_indexes = [
                index
                for index, series in enumerate(series_rotation)
                if series == preferred_series
            ]
            if preferred_indexes:
                indexes.append(preferred_indexes[0])
        indexes.extend(
            index
            for attempt in range(len(series_rotation))
            for index in [(rotation_index + attempt) % len(series_rotation)]
            if index not in indexes
        )
        for index in indexes:
            series = series_rotation[index]
            items = groups[series]
            candidate = None
            for item_offset in range(len(items)):
                candidate_index = (pointers[series] + item_offset) % len(items)
                proposed = items[candidate_index]
                if not unused_exists or proposed.asset_id not in used_assets:
                    candidate = proposed
                    pointers[series] = candidate_index
                    break
            if candidate is None:
                continue
            if candidate.duration_ms <= remaining_ms:
                pointers[series] = (pointers[series] + 1) % len(items)
                return candidate, (index + 1) % len(series_rotation)
        return None, rotation_index

    def _same_series_package(
        self,
        primary: Candidate,
        groups: dict[str, list[Candidate]],
        pointers: dict[str, int],
        used_assets: set[int],
        remaining_ms: int,
    ) -> list[Candidate]:
        programs = [primary]
        if (
            not self.same_series_pairing
            or primary.duration_ms >= self.short_program_target_ms
            or self.short_program_slot_ms > remaining_ms
        ):
            return programs
        series_items = groups.get(primary.series, [])
        if len(series_items) < 2:
            return programs

        package_assets = set(used_assets)
        package_assets.add(primary.asset_id)
        program_ms = primary.duration_ms
        while program_ms < self.short_program_target_ms:
            start = pointers.get(primary.series, 0)
            selected: Candidate | None = None
            selected_index: int | None = None
            for offset in range(len(series_items)):
                index = (start + offset) % len(series_items)
                candidate = series_items[index]
                if candidate.asset_id in package_assets:
                    continue
                selected = candidate
                selected_index = index
                break
            if selected is None or selected_index is None:
                break
            # Keep strict series order: if the next unused item cannot fit,
            # do not skip ahead to a later, shorter episode.
            if program_ms + selected.duration_ms > self.short_program_slot_ms:
                break
            programs.append(selected)
            package_assets.add(selected.asset_id)
            program_ms += selected.duration_ms
            pointers[primary.series] = (selected_index + 1) % len(series_items)
        return programs

    def _package_duration_ms(
        self,
        programs: list[Candidate],
        remaining_ms: int,
    ) -> int:
        program_ms = sum(item.duration_ms for item in programs)
        if program_ms <= self.short_program_slot_ms:
            target = self.short_program_slot_ms
        else:
            quantum = self.clock_slot_minutes * 60_000
            target = ((program_ms + quantum - 1) // quantum) * quantum
        if target <= remaining_ms:
            return target
        # A daypart or special-window boundary can be shorter than the normal
        # clock package. The program still fits, so let that boundary close it.
        return remaining_ms

    def _propose_break(
        self,
        bumpers: list[Candidate],
        commercials: list[Candidate],
        holiday_commercial_max: int | None,
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
        reserved_ids: set[int],
    ) -> list[tuple[Candidate, int]]:
        proposed: list[tuple[Candidate, int]] = []
        selected_bumper_ids: set[int] = set()
        break_has_commercial = False
        for sequence_item in self.sequence_template:
            if sequence_item == "episode":
                continue
            if sequence_item in {"bumper", "promo"}:
                if (
                    sequence_item == "bumper"
                    and selected_bumper_ids
                    and not break_has_commercial
                ):
                    continue
                type_choices = [
                    item
                    for item in bumpers
                    if (
                        sequence_item == "bumper"
                        and item.media_type == "bumper"
                        or sequence_item == "promo"
                        and item.media_type == "promo"
                    )
                ]
                choices = [
                    item for item in type_choices
                    if item.asset_id not in reserved_ids
                    and item.asset_id not in selected_bumper_ids
                ] or type_choices
                if sequence_item == "bumper":
                    general_choices = [
                        item for item in choices
                        if _candidate_bumper_cue(item, self.config).position == "general"
                    ]
                    bumper = self._pick_bumper(
                        general_choices, rng, cursor, last_used
                    )
                else:
                    bumper = self._pick_interstitial(
                        choices, rng, cursor, last_used, self.bumper_cooldown
                    )
                if bumper:
                    proposed.append((bumper, self.bumper_cooldown))
                    reserved_ids.add(bumper.asset_id)
                    if bumper.media_type == "bumper":
                        selected_bumper_ids.add(bumper.asset_id)
            elif sequence_item == "commercial_break" and commercials:
                count = rng.randint(self.commercial_min, self.commercial_max)
                selected_ids: set[int] = set()
                holiday_selected = 0
                for _ in range(count):
                    available = [
                        item
                        for item in commercials
                        if item.asset_id not in reserved_ids
                        and item.asset_id not in selected_ids
                        and (
                            item.collection_type != "holiday"
                            or holiday_commercial_max is None
                            or holiday_selected < holiday_commercial_max
                        )
                    ]
                    commercial = self._pick_interstitial(
                        available,
                        rng,
                        cursor,
                        last_used,
                        self.commercial_cooldown,
                    )
                    if commercial:
                        proposed.append((commercial, self.commercial_cooldown))
                        selected_ids.add(commercial.asset_id)
                        reserved_ids.add(commercial.asset_id)
                        if commercial.collection_type == "holiday":
                            holiday_selected += 1
                        break_has_commercial = True
        return proposed

    def _pick_opening_bumper(
        self,
        bumpers: list[Candidate],
        budget_ms: int,
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
    ) -> Candidate | None:
        if not self.opening_bumper_enabled:
            return None
        fitting = [
            item for item in bumpers
            if item.media_type == "bumper" and item.duration_ms <= budget_ms
            and _candidate_bumper_cue(item, self.config).position == "general"
        ]
        return self._pick_bumper(fitting, rng, cursor, last_used)

    def _pick_boundary_bumper(
        self,
        bumpers: list[Candidate],
        position: str,
        program_series: str | None,
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
        *,
        exclude_id: int | None = None,
        general_only: bool = False,
    ) -> Candidate | None:
        matching: list[Candidate] = []
        general: list[Candidate] = []
        for item in bumpers:
            cue = _candidate_bumper_cue(item, self.config)
            if cue.position == "general":
                general.append(item)
            elif (
                cue.position == position
                and (cue.series_hint is None or series_matches(cue.series_hint, program_series))
            ):
                matching.append(item)
        # Avoid playing the same clip at both sides when the library allows it.
        pools = (general,) if general_only else (matching, general)
        for pool in pools:
            distinct = [item for item in pool if item.asset_id != exclude_id]
            if distinct:
                return self._pick_bumper(distinct, rng, cursor, last_used)
        for pool in pools:
            if pool:
                return self._pick_bumper(pool, rng, cursor, last_used)
        return None

    def _propose_midroll(
        self,
        bumpers: list[Candidate],
        commercials: list[Candidate],
        budget_ms: int,
        holiday_commercial_max: int | None,
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
        reserved_ids: set[int],
        out_series: str | None = None,
        in_series: str | None = None,
    ) -> list[Candidate]:
        """Return a commercial midroll, preferably bookended by bumpers."""
        if not self.midroll_enabled or not commercials:
            return []
        shortest_commercial_ms = min(item.duration_ms for item in commercials)
        all_fitting_bumpers = [
            item for item in bumpers
            if item.media_type == "bumper"
            and item.duration_ms + shortest_commercial_ms <= budget_ms
        ]
        out_bumper: Candidate | None = None
        in_bumper: Candidate | None = None
        if all_fitting_bumpers:
            # Opening use and cooldown influence rotation, but never remove a
            # bumper opportunity. Pool size scales the effective cooldown.
            out_bumper = self._pick_boundary_bumper(
                all_fitting_bumpers, "break_start", out_series,
                rng, cursor, last_used,
            )
            in_bumper = self._pick_boundary_bumper(
                all_fitting_bumpers, "break_end", in_series,
                rng, cursor, last_used,
                exclude_id=out_bumper.asset_id if out_bumper else None,
            )
        general_out = (
            out_bumper
            if out_bumper is not None
            and _candidate_bumper_cue(out_bumper, self.config).position == "general"
            else self._pick_boundary_bumper(
                all_fitting_bumpers, "break_start", out_series,
                rng, cursor, last_used, general_only=True,
            )
        )
        general_in = (
            in_bumper
            if in_bumper is not None
            and _candidate_bumper_cue(in_bumper, self.config).position == "general"
            else self._pick_boundary_bumper(
                all_fitting_bumpers, "break_end", in_series,
                rng, cursor, last_used,
                exclude_id=general_out.asset_id if general_out else None,
                general_only=True,
            )
        )

        def choose_commercials(available_ms: int) -> list[Candidate]:
            remaining_holiday_slots = holiday_commercial_max
            if remaining_holiday_slots is not None:
                already_reserved_holidays = {
                    item.asset_id
                    for item in commercials
                    if item.collection_type == "holiday"
                    and item.asset_id in reserved_ids
                }
                remaining_holiday_slots = max(
                    0, remaining_holiday_slots - len(already_reserved_holidays)
                )
            return self._gap_commercial_pack(
                commercials,
                rng,
                cursor,
                last_used,
                available_ms,
                0,
                remaining_holiday_slots,
                max_items=self.commercial_max,
                reserved_ids=reserved_ids,
            )

        # A short act-break budget may fit one bookend or just commercials.
        seen_pairs: set[tuple[int | None, int | None]] = set()
        for opening, closing in (
            (out_bumper, in_bumper), (out_bumper, general_in),
            (general_out, in_bumper), (general_out, general_in),
            (out_bumper, None), (None, in_bumper),
            (general_out, None), (None, general_in), (None, None),
        ):
            pair = (opening.asset_id if opening else None,
                    closing.asset_id if closing else None)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            reserved_ms = sum(
                item.duration_ms for item in (opening, closing) if item is not None
            )
            chosen = choose_commercials(max(0, budget_ms - reserved_ms))
            if chosen:
                return [*([opening] if opening else []), *chosen,
                        *([closing] if closing else [])]
        return []

    def _breakpoints_for(self, candidate: Candidate) -> list[int]:
        if (
            not self.midroll_enabled
            or candidate.media_type not in self.midroll_media_types
            or candidate.duration_ms <= self.midroll_min_media_ms
        ):
            return []
        offsets = self.breakpoints.selected_offsets(candidate.asset_id)
        return [
            offset for offset in offsets
            if self.midroll_min_segment_ms
            <= offset
            <= candidate.duration_ms - self.midroll_min_segment_ms
        ]

    def _paced_breakpoints_for(
        self,
        candidate: Candidate,
        package_ms: int,
        available_break_ms: int,
    ) -> list[int]:
        """Offer recorded offsets; the pod planner decides which can fit."""
        return self._breakpoints_for(candidate)

    @staticmethod
    def _series_rotation(
        groups: dict[str, list[Candidate]], rng: random.Random
    ) -> list[str]:
        rotation = sorted(groups)
        if any(
            abs(item.weight - 1.0) > 0.001
            for candidates in groups.values()
            for item in candidates
        ):
            rotation = [
                series
                for series in sorted(groups)
                for _ in range(
                    max(1, round(max(item.weight for item in groups[series]) * 2))
                )
            ]
        rng.shuffle(rotation)
        return rotation

    @staticmethod
    def _pick_interstitial(
        pool: list[Candidate],
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
        cooldown_seconds: int,
    ) -> Candidate | None:
        if not pool:
            return None
        eligible = [
            item
            for item in pool
            if item.asset_id not in last_used
            or (cursor - last_used[item.asset_id]).total_seconds()
            >= (
                item.cooldown_seconds
                if item.cooldown_seconds is not None
                else cooldown_seconds
            )
        ]
        # Promos participate in two pools; never use the small-pool fallback
        # to replay a cooling promo immediately in its other role.
        choices = eligible or [item for item in pool if item.media_type != "promo"]
        if not choices:
            return None
        return rng.choices(choices, weights=[item.weight for item in choices], k=1)[0]

    def _pick_bumper(
        self,
        pool: list[Candidate],
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
        population_size: int | None = None,
    ) -> Candidate | None:
        """Choose a bumper without ever letting repeat protection omit it.

        One bumper has no repeat delay. As the pool grows, the effective delay
        approaches the configured cooldown. When every choice is recent, the
        least recently used bumper is selected instead of dropping the slot.
        """
        if not pool:
            return None
        count = max(1, population_size or len({item.asset_id for item in pool}))
        cooldown_scale = (count - 1) / count
        eligible: list[Candidate] = []
        for item in pool:
            configured = (
                item.cooldown_seconds
                if item.cooldown_seconds is not None
                else self.bumper_cooldown
            )
            if (
                item.asset_id not in last_used
                or (cursor - last_used[item.asset_id]).total_seconds()
                >= configured * cooldown_scale
            ):
                eligible.append(item)
        if eligible:
            return rng.choices(
                eligible, weights=[item.weight for item in eligible], k=1
            )[0]
        oldest = min(last_used.get(item.asset_id, cursor) for item in pool)
        choices = [
            item for item in pool
            if last_used.get(item.asset_id, cursor) == oldest
        ]
        return rng.choices(
            choices, weights=[item.weight for item in choices], k=1
        )[0]

    def _gap_commercial_pack(
        self,
        pool: list[Candidate],
        rng: random.Random,
        cursor: datetime,
        last_used: dict[int, datetime],
        remaining_ms: int,
        reserve_ms: int,
        holiday_slots: int | None,
        *,
        max_items: int | None = None,
        reserved_ids: set[int] | None = None,
    ) -> list[Candidate]:
        """Choose a bounded best-fit pod without overrunning its time budget."""
        maximum = self.gap_fill_commercial_max if max_items is None else max(0, max_items)
        budget_seconds = max(0, (remaining_ms - reserve_ms) // 1000)
        if budget_seconds <= 0 or maximum <= 0:
            return []
        fitting = [
            item for item in pool
            if math.ceil(item.duration_ms / 1000) <= budget_seconds
            and (holiday_slots is None or item.collection_type != "holiday" or holiday_slots > 0)
        ]
        if not fitting:
            return []
        if reserved_ids:
            fresh = [item for item in fitting if item.asset_id not in reserved_ids]
            # Prefer a fresh spot, but allow a small library to reuse one in a
            # later break rather than removing the break entirely.
            if fresh:
                fitting = fresh
        cooled = [
            item for item in fitting
            if item.asset_id not in last_used
            or (cursor - last_used[item.asset_id]).total_seconds()
            >= (
                item.cooldown_seconds
                if item.cooldown_seconds is not None
                else self.commercial_cooldown
            )
        ]
        # Prefer cooldown-eligible spots, but an otherwise empty break may
        # reuse a spot from a small library rather than dropping the pod.
        available = cooled or fitting

        # One representative per rounded duration keeps the bounded subset-sum
        # search fast even with thousands of commercials while retaining the
        # normal weighted randomness among equivalent choices.
        by_duration: dict[int, list[Candidate]] = {}
        for item in available:
            by_duration.setdefault(math.ceil(item.duration_ms / 1000), []).append(item)
        candidates: list[Candidate] = []
        for _duration, items in sorted(by_duration.items()):
            remaining = list(items)
            for _ in range(min(maximum, len(remaining))):
                chosen = rng.choices(
                    remaining,
                    weights=[item.weight for item in remaining],
                    k=1,
                )[0]
                candidates.append(chosen)
                remaining.remove(chosen)
        states: list[dict[tuple[int, int], list[Candidate]]] = [
            {} for _ in range(maximum + 1)
        ]
        states[0][(0, 0)] = []
        for candidate in candidates:
            seconds = math.ceil(candidate.duration_ms / 1000)
            holiday = int(candidate.collection_type == "holiday")
            for count in range(maximum, 0, -1):
                for (total, holiday_count), selected in list(states[count - 1].items()):
                    proposed_total = total + seconds
                    proposed_holidays = holiday_count + holiday
                    if proposed_total > budget_seconds:
                        continue
                    if holiday_slots is not None and proposed_holidays > holiday_slots:
                        continue
                    key = (proposed_total, proposed_holidays)
                    states[count].setdefault(key, [*selected, candidate])
        choices = [
            (total, count, selected)
            for count, state in enumerate(states[1:], start=1)
            for (total, _holiday_count), selected in state.items()
        ]
        if not choices:
            return []
        return max(choices, key=lambda item: (item[0], item[1]))[2]

    def _append_asset(
        self,
        items: list[TimelineItem],
        candidate: Candidate,
        cursor: datetime,
        daypart_slug: str,
        group: int | None,
        *,
        is_program: bool,
        context: RuleContext,
        protected_block: bool = False,
        programming_block_id: int | None = None,
        media_start_ms: int = 0,
        media_end_ms: int | None = None,
        part_label: str | None = None,
    ) -> datetime:
        media_end_ms = candidate.duration_ms if media_end_ms is None else media_end_ms
        if media_start_ms < 0 or media_end_ms <= media_start_ms:
            raise ValueError("Scheduled media slice has invalid boundaries")
        if media_end_ms > candidate.duration_ms:
            raise ValueError("Scheduled media slice exceeds its source duration")
        slice_duration_ms = media_end_ms - media_start_ms
        ends_at = cursor + timedelta(milliseconds=slice_duration_ms)
        explanation = context.explanation()
        explanation["selection"] = {
            "asset_id": candidate.asset_id,
            "collection_type": candidate.collection_type,
            "collection": candidate.collection_slug,
            "rule": candidate.rule_slug,
            "weight_multiplier": candidate.weight,
            "series": candidate.series,
        }
        if part_label:
            explanation["media_slice"] = {
                "part": part_label,
                "start_ms": media_start_ms,
                "end_ms": media_end_ms,
            }
        owner = context.owner_slug
        if is_program and candidate.recurring_holiday_override:
            owner = candidate.collection_slug
            explanation["programming_owner"] = owner
            explanation["selection"]["recurring_holiday_override"] = {
                "holiday": candidate.collection_slug,
                "reason": "Holiday series takes priority over the regular recurring slot",
            }
        if is_program and candidate.collection_type == "holiday":
            replacing = next(
                (
                    rule
                    for rule in context.holidays
                    if rule.slug == candidate.collection_slug and rule.mode == "replace"
                ),
                None,
            )
            if replacing:
                owner = replacing.slug
        items.append(
            TimelineItem(
                candidate.asset_id,
                cursor,
                ends_at,
                media_start_ms,
                media_end_ms,
                candidate.media_type,
                candidate.title,
                daypart_slug,
                group,
                len(items),
                is_program,
                owner,
                context.guide_label,
                json.dumps(explanation, sort_keys=True),
                protected_block,
                candidate.title if protected_block else None,
                programming_block_id,
            )
        )
        return ends_at

    def _append_announcement(
        self,
        items: list[TimelineItem],
        cursor: datetime,
        daypart_slug: str,
        context: RuleContext,
        *,
        programming_block_id: int | None = None,
    ) -> datetime:
        duration = max(
            1,
            int(
                self.config.data.get("announcements", {}).get(
                    "opportunity_duration_seconds", 30
                )
            ),
        )
        ends_at = cursor + timedelta(seconds=duration)
        items.append(
            TimelineItem(
                None,
                cursor,
                ends_at,
                None,
                None,
                "announcement_opportunity",
                "Up Next",
                daypart_slug,
                None,
                len(items),
                False,
                context.owner_slug,
                context.guide_label,
                json.dumps(context.explanation(), sort_keys=True),
                False,
                None,
                programming_block_id,
            )
        )
        return ends_at

    def _append_graphic_if_fit(
        self,
        items: list[TimelineItem],
        channel_slug: str,
        cursor: datetime,
        boundary: datetime,
        daypart_slug: str,
        context: RuleContext,
        last_graphic_at: datetime | None,
        rng: random.Random,
        *,
        programming_block_id: int | None,
        ignore_cooldown: bool = False,
    ) -> tuple[datetime, datetime | None]:
        settings = self.config.data.get("graphics", {})
        cooldown = max(0, int(settings.get("cooldown_minutes", 30)))
        if not bool(settings.get("standalone_enabled", True)):
            return cursor, last_graphic_at
        if cursor >= boundary:
            return cursor, last_graphic_at
        if (
            not ignore_cooldown
            and last_graphic_at
            and (cursor - last_graphic_at).total_seconds() < cooldown * 60
        ):
            return cursor, last_graphic_at
        if items and items[-1].entry_type == "graphic_interstitial":
            return cursor, last_graphic_at
        graphic = GraphicsService(self.connection, self.config).select_asset(
            channel_slug, "interstitials", cursor, rng=rng,
            weekly_collection_slug=(
                context.owner.collection_slug if context.owner else None
            ),
        )
        if graphic is None:
            return cursor, last_graphic_at
        # Once selected, one station interstitial owns the whole unresolved
        # gap. Still images hold and motion backgrounds loop during playback.
        # The configured duration is used when rendering the asset, but the
        # schedule may hold it for any positive unresolved gap.
        ends = boundary
        items.append(
            TimelineItem(
                None, cursor, ends, None, None, "graphic_interstitial",
                "Station Interstitial", daypart_slug, None, len(items), False,
                context.owner_slug, context.guide_label,
                json.dumps(context.explanation(), sort_keys=True), False, None,
                programming_block_id, int(graphic["id"]),
            )
        )
        return ends, cursor

    @staticmethod
    def _link_announcement_blocks(items: list[TimelineItem]) -> list[TimelineItem]:
        """Associate each announcement with the programming it introduces."""
        linked = list(items)
        for index, item in enumerate(linked):
            if item.entry_type != "announcement_opportunity":
                continue
            upcoming = next(
                (
                    later
                    for later in linked[index + 1 :]
                    if later.is_program and later.programming_block_id is not None
                ),
                None,
            )
            linked[index] = replace(
                item,
                title="Up Next" if upcoming else "Station Identification",
                programming_block_id=(
                    upcoming.programming_block_id if upcoming else None
                ),
                sequence_group=upcoming.sequence_group if upcoming else None,
                guide_listing_title=(
                    upcoming.guide_listing_title or upcoming.title
                    if upcoming
                    else "Station Identification"
                ),
            )
        return linked

    @staticmethod
    def _unique_candidates(items: Iterable[Candidate]) -> list[Candidate]:
        result: dict[int, Candidate] = {}
        for item in items:
            existing = result.get(item.asset_id)
            if existing is None or item.weight > existing.weight:
                result[item.asset_id] = item
        return list(result.values())

    def _pools_for_context(
        self,
        channel_slug: str,
        daypart_slug: str,
        context: RuleContext,
    ) -> tuple[
        list[Candidate],
        list[Candidate],
        list[Candidate],
        list[Candidate],
        int | None,
    ]:
        programs = self._candidates(
            channel_slug, daypart_slug, ("episode", "short", "movie")
        )
        bumpers = self._candidates(
            channel_slug, daypart_slug, ("bumper", "promo")
        )
        commercials = self._candidates(channel_slug, daypart_slug, ("commercial",))
        filler = self._candidates(channel_slug, daypart_slug, ("filler",))
        maximum_holiday_per_break: int | None = None

        if context.owner:
            # A weekly window is a complete temporary channel owner. Never
            # borrow programming or interstitials from the base channel while
            # it is active; an empty category remains empty. Holiday rules are
            # applied below as explicit modifiers of that weekly collection.
            programs = self._special_candidates(
                channel_slug,
                daypart_slug,
                ("episode", "short", "movie"),
                collection_type="weekly",
                collection_slug=context.owner.collection_slug,
                rule_slug=context.owner.slug,
            )
            bumpers = self._special_candidates(
                channel_slug,
                daypart_slug,
                ("bumper", "promo"),
                collection_type="weekly",
                collection_slug=context.owner.collection_slug,
                rule_slug=context.owner.slug,
            )
            commercials = self._special_candidates(
                channel_slug,
                daypart_slug,
                ("commercial",),
                collection_type="weekly",
                collection_slug=context.owner.collection_slug,
                rule_slug=context.owner.slug,
            )
            filler = self._special_candidates(
                channel_slug,
                daypart_slug,
                ("filler", "station_id"),
                collection_type="weekly",
                collection_slug=context.owner.collection_slug,
                rule_slug=context.owner.slug,
            )

        for holiday in reversed(context.holidays):
            holiday_programs = self._special_candidates(
                channel_slug,
                daypart_slug,
                ("episode", "short", "movie"),
                collection_type="holiday",
                collection_slug=holiday.slug,
                rule_slug=holiday.slug,
                weights=holiday.weights,
                cooldown_seconds=holiday.cooldown_seconds,
                allow_shared_movies=holiday.allows_shared_movie(channel_slug),
                holiday_weekly_slug=context.owner.collection_slug if context.owner else None,
            )
            recurring_overlay = (context.owner is None
                                 and self.schedule_preferences(channel_slug)["recurring_enabled"]
                                 and self.schedule_preferences(channel_slug)["presentation_format"] != "content_only")
            if not recurring_overlay and holiday.mode == "replace" and holiday_programs:
                programs = holiday_programs
            elif not recurring_overlay and holiday.mode == "boost":
                programs.extend(holiday_programs)
            bumpers.extend(
                self._special_candidates(
                    channel_slug,
                    daypart_slug,
                    ("bumper", "promo"),
                    collection_type="holiday",
                    collection_slug=holiday.slug,
                    rule_slug=holiday.slug,
                    weights=holiday.weights,
                    cooldown_seconds=holiday.cooldown_seconds,
                    holiday_weekly_slug=context.owner.collection_slug if context.owner else None,
                )
            )
            commercials.extend(
                self._special_candidates(
                    channel_slug,
                    daypart_slug,
                    ("commercial",),
                    collection_type="holiday",
                    collection_slug=holiday.slug,
                    rule_slug=holiday.slug,
                    weights=holiday.weights,
                    cooldown_seconds=holiday.cooldown_seconds,
                    holiday_weekly_slug=context.owner.collection_slug if context.owner else None,
                )
            )
            maximum_holiday_per_break = (
                holiday.maximum_per_break
                if maximum_holiday_per_break is None
                else max(maximum_holiday_per_break, holiday.maximum_per_break)
            )
        # Resolve ownership and holiday modifiers first, then share the exact
        # same promo identities and cooldowns between advertising and gap fill.
        promos = [
            replace(item, cooldown_seconds=(
                self.commercial_cooldown
                if item.cooldown_seconds is None else item.cooldown_seconds
            ))
            for item in self._unique_candidates(bumpers)
            if item.media_type == "promo"
        ]
        bumpers = [item for item in bumpers if item.media_type != "promo"]
        commercials.extend(promos)
        filler.extend(promos)
        return (
            self._unique_candidates(programs),
            self._unique_candidates(bumpers),
            self._unique_candidates(commercials),
            self._unique_candidates(filler),
            maximum_holiday_per_break,
        )

    def _block_pool_for_context(
        self,
        channel_slug: str,
        daypart_slug: str,
        context: RuleContext,
    ) -> list[Candidate]:
        if context.owner:
            blocks = self._special_candidates(
                channel_slug,
                daypart_slug,
                ("complete_block",),
                collection_type="weekly",
                collection_slug=context.owner.collection_slug,
                rule_slug=context.owner.slug,
            )
        else:
            blocks = self._candidates(
                channel_slug, daypart_slug, ("complete_block",)
            )
        for holiday in context.holidays:
            blocks.extend(
                self._special_candidates(
                    channel_slug,
                    daypart_slug,
                    ("complete_block",),
                    collection_type="holiday",
                    collection_slug=holiday.slug,
                    rule_slug=holiday.slug,
                    weights=holiday.weights,
                    cooldown_seconds=holiday.cooldown_seconds,
                    holiday_weekly_slug=context.owner.collection_slug if context.owner else None,
                )
            )
        return self._unique_candidates(blocks)

    def _recent_block_ids(
        self, channel_id: int, before: datetime
    ) -> set[int]:
        if self.block_repeat_days <= 0:
            return set()
        cutoff = before - timedelta(days=self.block_repeat_days)
        rows = self.connection.execute(
            """
            SELECT DISTINCT se.asset_id
            FROM schedule_entries se
            JOIN schedule_generations sg ON sg.id = se.generation_id
            WHERE se.channel_id = ?
              AND se.protected_block = 1
              AND se.asset_id IS NOT NULL
              AND sg.status IN ('active', 'superseded')
              AND se.starts_at >= ?
              AND se.starts_at < ?
            """,
            (channel_id, _iso(cutoff), _iso(before)),
        ).fetchall()
        result = {int(row["asset_id"]) for row in rows}
        checkpoints = self.connection.execute(
            """SELECT asset_id FROM schedule_history_checkpoints
               WHERE channel_id=? AND context_type='block'
                 AND asset_id IS NOT NULL AND last_used_at >= ? AND last_used_at < ?""",
            (channel_id, _iso(cutoff), _iso(before)),
        ).fetchall()
        result.update(int(row["asset_id"]) for row in checkpoints)
        return result

    def _block_last_used(
        self, channel_id: int, before: datetime
    ) -> dict[int, str]:
        rows = self.connection.execute(
            """
            SELECT se.asset_id, MAX(se.starts_at) AS last_starts_at
            FROM schedule_entries se
            JOIN schedule_generations sg ON sg.id = se.generation_id
            WHERE se.channel_id = ?
              AND se.protected_block = 1
              AND se.asset_id IS NOT NULL
              AND sg.status IN ('active', 'superseded')
              AND se.starts_at < ?
            GROUP BY se.asset_id
            """,
            (channel_id, _iso(before)),
        ).fetchall()
        result = {
            int(row["asset_id"]): str(row["last_starts_at"])
            for row in rows
        }
        checkpoints = self.connection.execute(
            """SELECT asset_id,last_used_at FROM schedule_history_checkpoints
               WHERE channel_id=? AND context_type='block'
                 AND asset_id IS NOT NULL AND last_used_at < ?""",
            (channel_id, _iso(before)),
        ).fetchall()
        for row in checkpoints:
            asset_id = int(row["asset_id"])
            used_at = str(row["last_used_at"])
            if asset_id not in result or used_at > result[asset_id]:
                result[asset_id] = used_at
        return result

    def _select_block(
        self,
        pool: list[Candidate],
        rng: random.Random,
        opportunity: BlockOpportunity,
        cursor: datetime,
        day_end: datetime,
        excluded: set[int],
        *,
        fallback: bool = False,
        last_used: dict[int, str] | None = None,
    ) -> BlockSelection | None:
        choices: list[BlockSelection] = []
        lateness_ms = max(
            0,
            round((cursor - opportunity.starts_at).total_seconds() * 1000),
        )
        for candidate in pool:
            if (
                (not fallback or not self.block_fallback_relaxes_repeats)
                and candidate.asset_id in excluded
            ) or lateness_ms >= candidate.duration_ms:
                continue
            policy = candidate.boundary_policy or self.block_boundary_policy
            candidate_end = opportunity.starts_at + timedelta(
                milliseconds=candidate.duration_ms
            )
            if candidate_end > day_end:
                continue
            if policy not in {"soft", "may_overrun"}:
                if lateness_ms > 0 or candidate_end > opportunity.window_ends_at:
                    continue
            elif lateness_ms > 0:
                if self.block_overlap_policy != "join_in_progress":
                    continue
                remaining_ms = candidate.duration_ms - lateness_ms
                if remaining_ms < self.block_minimum_join_remaining_ms:
                    continue
                if (
                    remaining_ms / candidate.duration_ms
                    < self.block_minimum_join_remaining_fraction
                ):
                    continue
            choices.append(
                BlockSelection(candidate, lateness_ms, opportunity.starts_at)
            )
        if not choices:
            return None
        if fallback:
            usage = last_used or {}
            oldest = min(usage.get(item.candidate.asset_id, "") for item in choices)
            selected = rng.choice(
                [
                    item for item in choices
                    if usage.get(item.candidate.asset_id, "") == oldest
                ]
            )
            return selected
        selected = rng.choice(choices)
        probability = (
            float(selected.candidate.probability_override)
            if selected.candidate.probability_override is not None
            else self.block_probability
        )
        return selected if rng.random() < probability else None

    def _build_content_only_timeline(
        self,
        channel_id: int,
        channel_slug: str,
        schedule_date: date,
        rng: random.Random,
    ) -> list[TimelineItem]:
        """Build a channel-wide, real-duration sequence with no interruptions.

        Content Only is for ambient channels such as a screensaver channel. It
        intentionally has no clock-slot package, commercial pod, bumper,
        graphic, announcement, or continuity stage. The next program begins at
        the exact end of the preceding one. Dayparts still decide which media is
        eligible at an item's start; a program already underway is allowed to
        finish across a daypart boundary.
        """
        day_start = datetime.combine(schedule_date, time.min, self.timezone)
        day_end = datetime.combine(
            schedule_date + timedelta(days=1), time.min, self.timezone
        )
        previous = self._previous_programs(channel_id, day_start)
        state: dict[
            str,
            tuple[dict[str, list[Candidate]], dict[str, int], list[str], int],
        ] = {}
        items: list[TimelineItem] = []
        used_programs: set[int] = set()
        carried_cursor = day_start
        programming_block_number = 0
        last_program_id: int | None = None

        for segment in self.segments_for_date(schedule_date, channel_slug):
            if carried_cursor >= segment.ends_at:
                continue
            cursor = max(segment.starts_at, carried_cursor)
            context = self.rules.context(channel_slug, cursor)
            programs, _bumpers, _commercials, _filler, _holiday_maximum = (
                self._pools_for_context(channel_slug, segment.slug, context)
            )
            # Complete blocks are genuine uninterrupted programming in this
            # format, rather than chance-selected station events.
            programs = self._unique_candidates(
                [*programs, *self._block_pool_for_context(channel_slug, segment.slug, context)]
            )
            if not programs:
                raise ValueError(
                    f"Content Only channel '{channel_slug}' has no eligible "
                    f"programming for the {segment.slug} daypart"
                )
            state_key = (
                segment.slug
                + ":"
                + context.owner_slug
                + ":"
                + ",".join(item.slug for item in context.holidays)
            )
            if state_key not in state:
                groups, pointers = self._series_state(programs, previous)
                state[state_key] = (groups, pointers, self._series_rotation(groups, rng), 0)
            groups, pointers, rotation, rotation_index = state[state_key]

            while cursor < segment.ends_at:
                remaining_ms = round((day_end - cursor).total_seconds() * 1000)
                program, next_rotation = self._select_program(
                    groups,
                    pointers,
                    rotation,
                    rotation_index,
                    remaining_ms,
                    used_programs,
                )
                if program is None:
                    # Once every available file has played, start a new pass.
                    # Pointer state keeps the restart from favoring the same
                    # file repeatedly when alternatives exist.
                    used_programs.clear()
                    program, next_rotation = self._select_program(
                        groups,
                        pointers,
                        rotation,
                        rotation_index,
                        remaining_ms,
                        used_programs,
                    )
                media_end_ms: int | None = None
                if program is None:
                    # Only the midnight boundary can leave less time than every
                    # real file. Keep the channel in content instead of adding
                    # a color-bar continuity slate; tomorrow starts a fresh
                    # wall-clock schedule.
                    alternatives = [
                        candidate
                        for candidates in groups.values()
                        for candidate in candidates
                        if candidate.asset_id != last_program_id
                    ] or [
                        candidate
                        for candidates in groups.values()
                        for candidate in candidates
                    ]
                    program = rng.choice(alternatives)
                    media_end_ms = min(program.duration_ms, remaining_ms)
                    next_rotation = rotation_index
                rotation_index = next_rotation
                programming_block_number += 1
                cursor = self._append_asset(
                    items,
                    program,
                    cursor,
                    segment.slug,
                    programming_block_number,
                    is_program=True,
                    context=context,
                    protected_block=(program.media_type == "complete_block"),
                    programming_block_id=programming_block_number,
                    media_end_ms=media_end_ms,
                )
                used_programs.add(program.asset_id)
                last_program_id = program.asset_id
            carried_cursor = cursor
        return items

    def _build_timeline(
        self,
        channel_id: int,
        channel_slug: str,
        schedule_date: date,
        rng: random.Random,
        *,
        reuse_recurring_template: bool = True,
        recurring_state: dict[str, object] | None = None,
        recurring_eligible_assets: dict[str, set[int]] | None = None,
        scoped_allocation: dict[str, object] | None = None,
    ) -> list[TimelineItem]:
        if self.schedule_preferences(channel_slug)["presentation_format"] == "content_only":
            return self._build_content_only_timeline(
                channel_id, channel_slug, schedule_date, rng
            )
        items: list[TimelineItem] = []
        previous = self._previous_programs(
            channel_id,
            datetime.combine(schedule_date, time.min, self.timezone),
        )
        if recurring_state is not None:
            # Base and seasonal placements may share a name or shadow target.
            # Only the base projection may seed the regular episode position.
            previous = {}
            progress = recurring_state.get("progress", {})
            if isinstance(progress, dict):
                previous.update(
                    {
                        str(series): int(row["last_asset_id"])
                        for series, row in progress.items()
                        if isinstance(row, dict) and row.get("last_asset_id") is not None
                    }
                )
        state: dict[str, tuple[dict[str, list[Candidate]], dict[str, int], list[str], int]] = {}
        used_programs: set[int] = set()
        last_used: dict[int, datetime] = {}
        group_number = 0
        programming_block_number = 0
        pending_announcement = False
        last_graphic_at: datetime | None = None
        used_blocks = self._recent_block_ids(
            channel_id,
            datetime.combine(schedule_date, time.min, self.timezone),
        )
        block_last_used = self._block_last_used(
            channel_id,
            datetime.combine(schedule_date, time.min, self.timezone),
        )
        preferences = self.schedule_preferences(channel_slug)
        recurring_template = self._recurring_template_kind(
            schedule_date, bool(preferences["unique_weekends"])
        )
        recurring_eligible_assets = recurring_eligible_assets or {}
        ordinary_clock_slots: dict[str, set[str]] = {}
        weekend_clock_slots: dict[str, dict[str, set[str]]] = {}
        if recurring_state is not None:
            ordinary_clock_slots = self._ordinary_recurring_clock_slots(
                channel_slug, schedule_date
            )
            if bool(preferences["unique_weekends"]) and schedule_date.weekday() < 5:
                for weekday, template in ((5, "saturday"), (6, "sunday")):
                    next_date = schedule_date + timedelta(
                        days=weekday - schedule_date.weekday()
                    )
                    weekend_clock_slots[template] = self._ordinary_recurring_clock_slots(
                        channel_slug, next_date
                    )
        scoped_plan: dict[str, dict[str, str]] | None = None
        if recurring_state is not None and recurring_state.get("version") == 2:
            slots = recurring_state.get("slots", {})
            queues = recurring_state.get("queues", {})
            progress = recurring_state.get("progress", {})
            assert isinstance(slots, dict) and isinstance(queues, dict)
            assert isinstance(progress, dict)
            scoped_plan, scoped_waiting = allocate_recurring_scopes(
                self._recurring_source_scopes(channel_slug),
                ordinary_clock_slots,
                {
                    part.slug: dict(slots.get(self._recurring_state_key(recurring_template, part.slug), {}))
                    for part in self.dayparts
                },
                {part: list(queue) for part, queue in queues.items() if isinstance(queue, list)},
                progress,
                recurring_eligible_assets,
                recurring_template,
                schedule_date.isoformat(),
                {
                    str(series)
                    for key, assignments in slots.items()
                    if str(key).split(":", 1)[0] != recurring_template
                    and isinstance(assignments, dict)
                    for series in assignments.values() if series
                },
            )
            if scoped_allocation is not None:
                scoped_allocation["plan"] = scoped_plan
                scoped_allocation["waiting"] = scoped_waiting
        holiday_overlays = self._holiday_recurring_plans(
            channel_slug, schedule_date, recurring_state, recurring_template,
            ordinary_clock_slots,
        ) if recurring_state is not None else []
        if holiday_overlays and scoped_allocation is not None:
            if scoped_plan is not None:
                underlying = {part: dict(assignments) for part, assignments in scoped_plan.items()}
                for key, assignments in recurring_state.get("slots", {}).items():
                    pattern, part = key.split(":", 1)
                    if pattern != recurring_template:
                        continue
                    for slot, series in assignments.items():
                        if (slot not in ordinary_clock_slots.get(part, set())
                                and series in recurring_eligible_assets):
                            underlying.setdefault(part, {})[slot] = series
                scoped_allocation["underlying_plan"] = underlying
        block_opportunities = self._block_opportunities_for_date(
            channel_slug, schedule_date
        )
        decided_block_opportunities: set[datetime] = set()
        carried_cursor = datetime.combine(schedule_date, time.min, self.timezone)
        day_end = datetime.combine(
            schedule_date + timedelta(days=1), time.min, self.timezone
        )

        for segment in self.segments_for_date(
            schedule_date,
            channel_slug,
            include_block_opportunities=True,
        ):
            if carried_cursor >= segment.ends_at:
                continue
            effective_start = max(segment.starts_at, carried_cursor)
            context = self.rules.context(channel_slug, effective_start)
            programs, bumpers, commercials, filler, holiday_commercial_max = (
                self._pools_for_context(channel_slug, segment.slug, context)
            )
            block_pool = self._block_pool_for_context(
                channel_slug, segment.slug, context
            )
            state_key = (
                segment.slug
                + ":"
                + context.owner_slug
                + ":"
                + ",".join(item.slug for item in context.holidays)
            )
            if state_key not in state:
                groups, pointers = self._series_state(programs, previous)
                rotation = self._series_rotation(groups, rng)
                state[state_key] = (groups, pointers, rotation, 0)
            groups, pointers, rotation, rotation_index = state[state_key]
            cursor = effective_start

            while cursor < segment.ends_at:
                opening_announcement_start: datetime | None = None
                if pending_announcement:
                    opportunity_seconds = int(
                        self.config.data.get("announcements", {}).get(
                            "opportunity_duration_seconds", 30
                        )
                    )
                    if cursor + timedelta(seconds=opportunity_seconds) <= min(segment.ends_at, day_end):
                        opening_announcement_start = cursor
                        cursor = self._append_announcement(
                            items, cursor, segment.slug, context
                        )
                        pending_announcement = False
                    else:
                        break
                block_opportunity = block_opportunities.get(segment.starts_at)
                if (
                    block_opportunity is not None
                    and block_opportunity.starts_at
                    not in decided_block_opportunities
                ):
                    decided_block_opportunities.add(block_opportunity.starts_at)
                    remaining_ms = round(
                        (segment.ends_at - cursor).total_seconds() * 1000
                    )
                    ordinary_programming_available = any(
                        candidate.duration_ms <= remaining_ms
                        for candidates in groups.values()
                        for candidate in candidates
                    )
                    block_fallback = (
                        self.block_fallback_when_no_programming
                        and not ordinary_programming_available
                    )
                    selection = self._select_block(
                        block_pool,
                        rng,
                        block_opportunity,
                        cursor,
                        day_end,
                        used_blocks,
                        fallback=block_fallback,
                        last_used=block_last_used,
                    )
                    if selection is not None:
                        block = selection.candidate
                        block_starts_at = cursor
                        programming_block_number += 1
                        group_number += 1
                        cursor = self._append_asset(
                            items,
                            block,
                            cursor,
                            segment.slug,
                            group_number,
                            is_program=True,
                            context=context,
                            protected_block=True,
                            programming_block_id=programming_block_number,
                            media_start_ms=selection.media_start_ms,
                            part_label=(
                                "join_in_progress"
                                if selection.media_start_ms > 0
                                else None
                            ),
                        )
                        used_blocks.add(block.asset_id)
                        block_last_used[block.asset_id] = _iso(block_starts_at)
                        pending_announcement = True
                        if cursor >= segment.ends_at:
                            break
                        continue
                remaining_ms = round((segment.ends_at - cursor).total_seconds() * 1000)
                preferred_series = None
                promoted_series = None
                selection_groups = groups
                selection_rotation = rotation
                selection_used = set(used_programs)
                if reuse_recurring_template or scoped_plan is not None:
                    queue_managed = (
                        recurring_state is not None
                        and bool(preferences["recurring_enabled"])
                        and context.owner is None
                    )
                    if queue_managed:
                        if scoped_plan is None:
                            self._recurring_prepare_slots(
                                recurring_state,
                                recurring_template,
                                segment.slug,
                                groups,
                                schedule_date,
                                unique_weekends=bool(preferences["unique_weekends"]),
                                all_eligible_assets=recurring_eligible_assets,
                                available_clock_slots=ordinary_clock_slots.get(segment.slug, set()),
                                weekend_available_slots={
                                    template: dayparts.get(segment.slug, set())
                                    for template, dayparts in weekend_clock_slots.items()
                                },
                            )
                            preferred_series = self._recurring_series_for_slot(
                                recurring_state,
                                recurring_template,
                                segment.slug,
                                cursor,
                                schedule_date,
                                groups,
                                all_eligible_assets=recurring_eligible_assets,
                            )
                            assignments = self._recurring_slots(
                                recurring_state, recurring_template, segment.slug
                            )
                        else:
                            assignments = scoped_plan.get(segment.slug, {})
                            clock_key = str(
                                (cursor.hour * 60 + cursor.minute)
                                // self.clock_slot_minutes * self.clock_slot_minutes
                            )
                            preferred_series = assignments.get(clock_key)
                        active_series = {name for name in assignments.values() if name}
                        if assignments:
                            progress = recurring_state.get("progress", {})
                            selection_groups = {}
                            for name in active_series:
                                candidates = groups.get(name, [])
                                progress_row = (
                                    progress.get(name, {})
                                    if isinstance(progress, dict) else {}
                                )
                                seen = (
                                    {int(value) for value in progress_row.get("seen_asset_ids", [])}
                                    if isinstance(progress_row, dict) else set()
                                )
                                remaining = [
                                    item for item in candidates
                                    if item.asset_id not in seen
                                ]
                                if remaining:
                                    # Keep pointer indexes in the full ordered
                                    # list; removing seen episodes shifts them
                                    # and would silently skip the next episode.
                                    selection_groups[name] = candidates
                                    selection_used.update(seen)
                            selection_rotation = [
                                name for name in rotation if name in selection_groups
                            ]
                        if preferred_series not in selection_groups:
                            preferred_series = None
                    else:
                        preferred_series = self._recurring_grid_preference(
                            channel_id,
                            channel_slug,
                            schedule_date,
                            segment.slug,
                            cursor,
                            context,
                        )
                        if recurring_state is None:
                            promoted_series = self._recurring_promotion_preference(
                                channel_id,
                                channel_slug,
                                schedule_date,
                                segment.slug,
                                cursor,
                                context,
                                groups,
                            )
                if promoted_series is not None:
                    preferred_series = promoted_series
                program, holiday_overlay, selection_budget = (
                    self._select_holiday_recurring_program(
                        holiday_overlays, segment.slug, cursor, remaining_ms,
                        schedule_date)
                    if context.owner is None else (None, None, remaining_ms)
                )
                next_rotation = rotation_index
                if program is None:
                    program, next_rotation = self._select_program(
                        selection_groups, pointers, selection_rotation, rotation_index,
                        selection_budget, selection_used, preferred_series,
                    )
                if program is None:
                    if selection_budget < remaining_ms:
                        gap_end = cursor + timedelta(milliseconds=selection_budget)
                        cursor, last_graphic_at = self._append_graphic_if_fit(
                            items, channel_slug, cursor, gap_end, segment.slug, context,
                            last_graphic_at, rng, programming_block_id=None,
                            ignore_cooldown=True)
                        if cursor < gap_end:
                            items.append(TimelineItem(
                                None, cursor, gap_end, None, None, "continuity",
                                "CRTV Sim Continuity", segment.slug, None, len(items),
                                False, context.owner_slug, context.guide_label,
                                json.dumps(context.explanation(), sort_keys=True)))
                        cursor = gap_end
                        continue
                    break
                rotation_index = next_rotation

                package_groups, package_pointers = (
                    holiday_overlay["pools"][segment.slug] if holiday_overlay
                    else (selection_groups, pointers)
                )
                package_programs = self._same_series_package(
                    program,
                    package_groups,
                    package_pointers,
                    (holiday_overlay["used"] | set(holiday_overlay["saved"]["progress"].get(
                        program.series, {}).get("seen_asset_ids", [])) if holiday_overlay else selection_used)
                    | {program.asset_id},
                    selection_budget,
                )
                if holiday_overlay:
                    package_programs = [replace(item, recurring_holiday_override=True)
                                        for item in package_programs]
                    self._record_holiday_recurring_programs(holiday_overlay, package_programs)

                package_ms = self._package_duration_ms(
                    package_programs,
                    selection_budget,
                )
                program_ms = sum(item.duration_ms for item in package_programs)
                if opening_announcement_start is None:
                    package_end = cursor + timedelta(milliseconds=package_ms)
                else:
                    package_end = opening_announcement_start + timedelta(
                        milliseconds=package_ms
                    )
                    slot_ms = self.clock_slot_minutes * 60 * 1000
                    while cursor + timedelta(milliseconds=program_ms) > package_end:
                        package_end += timedelta(milliseconds=slot_ms)
                    package_end = min(package_end, segment.ends_at, day_end)
                    package_ms = round((package_end - cursor).total_seconds() * 1000)
                opportunity_ms = max(
                    1000,
                    int(
                        self.config.data.get("announcements", {}).get(
                            "opportunity_duration_seconds", 30
                        )
                    )
                    * 1000,
                )
                if holiday_overlay:
                    holiday_overlay["airings"][-1]["actual_end"] = _iso(package_end)
                reserve_tail = program_ms <= package_ms - opportunity_ms
                fill_end = package_end
                interstitial_budget = max(
                    0, package_ms - program_ms - (opportunity_ms if reserve_tail else 0)
                )
                programming_block_number += 1

                reserved_ids: set[int] = set()
                opening_bumper = self._pick_opening_bumper(
                    bumpers, interstitial_budget, rng, cursor, last_used
                )
                if opening_bumper is not None:
                    reserved_ids.add(opening_bumper.asset_id)
                remaining_break_budget = interstitial_budget - (
                    opening_bumper.duration_ms if opening_bumper else 0
                )

                # Each occupied 30-minute portion of an eligible episode gets
                # one act-break window. Shorts, movies, and complete blocks are
                # never cut by this path.
                midroll_plans: dict[int, list[tuple[int, list[Candidate]]]] = {}
                boundary_midrolls: dict[int, list[Candidate]] = {}
                midroll_items: list[Candidate] = []
                paired_episode_package = (
                    len(package_programs) > 1
                    and all(item.media_type == "episode" for item in package_programs)
                )
                episode_offsets = [
                    [] if paired_episode_package else self._breakpoints_for(item)
                    for item in package_programs
                ]
                if len(package_programs) == 1 and not paired_episode_package:
                    episode_offsets[0] = self._paced_breakpoints_for(
                        package_programs[0], package_ms, remaining_break_budget
                    )
                pending_breaks = (
                    len(package_programs) - 1
                    if paired_episode_package
                    else sum(len(offsets) for offsets in episode_offsets)
                )
                planned_breaks = pending_breaks

                def next_midroll_budget() -> int:
                    # An hour-long episode needs several short interruptions.
                    # Reserve an equal share for every remaining act break and
                    # the end-of-package fill instead of letting the first pod
                    # consume the entire hour's advertising allowance.
                    return min(
                        self.midroll_max_pod_ms,
                        max(0, remaining_break_budget // (pending_breaks + 1)),
                    )

                for index, selected_program in enumerate(package_programs):
                    # Separate files already provide exact act boundaries. Do
                    # not also cut inside one of the paired episode segments.
                    proposed_offsets = episode_offsets[index]
                    if not proposed_offsets:
                        continue
                    plans: list[tuple[int, list[Candidate]]] = []
                    for proposed_offset in proposed_offsets:
                        proposed = self._propose_midroll(
                            bumpers,
                            commercials,
                            next_midroll_budget(),
                            holiday_commercial_max,
                            rng,
                            cursor,
                            last_used,
                            reserved_ids,
                            selected_program.series,
                            selected_program.series,
                        )
                        pending_breaks -= 1
                        if not proposed:
                            continue
                        plans.append((proposed_offset, proposed))
                        midroll_items.extend(proposed)
                        remaining_break_budget -= sum(
                            item.duration_ms for item in proposed
                        )
                        reserved_ids.update(
                            item.asset_id for item in proposed
                            if item.media_type in {"commercial", "promo"}
                        )
                    if plans:
                        midroll_plans[index] = plans

                # Paired episode segments receive a normal midroll pod at each
                # file boundary. The pod is optional when the complete programs,
                # opening bumper, and Up Next reservation leave insufficient room.
                if paired_episode_package:
                    for index in range(len(package_programs) - 1):
                        proposed = self._propose_midroll(
                            bumpers,
                            commercials,
                            next_midroll_budget(),
                            holiday_commercial_max,
                            rng,
                            cursor,
                            last_used,
                            reserved_ids,
                            package_programs[index].series,
                            package_programs[index + 1].series,
                        )
                        pending_breaks -= 1
                        if not proposed:
                            continue
                        boundary_midrolls[index] = proposed
                        midroll_items.extend(proposed)
                        remaining_break_budget -= sum(
                            item.duration_ms for item in proposed
                        )
                        reserved_ids.update(
                            item.asset_id for item in proposed
                            if item.media_type in {"commercial", "promo"}
                        )

                last_group: int | None = None
                for index, selected_program in enumerate(package_programs):
                    group_number += 1
                    last_group = group_number
                    if index == 0 and opening_bumper is not None:
                        cursor = self._append_asset(
                            items,
                            opening_bumper,
                            cursor,
                            segment.slug,
                            group_number,
                            is_program=False,
                            context=context,
                            programming_block_id=programming_block_number,
                        )
                        last_used[opening_bumper.asset_id] = cursor
                    plans = midroll_plans.get(index, [])
                    if plans:
                        media_start_ms = 0
                        for part_index, (break_offset, interstitials) in enumerate(
                            plans, start=1
                        ):
                            cursor = self._append_asset(
                                items, selected_program, cursor, segment.slug,
                                group_number, is_program=True, context=context,
                                programming_block_id=programming_block_number,
                                media_start_ms=media_start_ms,
                                media_end_ms=break_offset,
                                part_label=f"part_{part_index}",
                            )
                            for interstitial in interstitials:
                                cursor = self._append_asset(
                                    items, interstitial, cursor, segment.slug,
                                    group_number, is_program=False, context=context,
                                    programming_block_id=programming_block_number,
                                )
                                last_used[interstitial.asset_id] = cursor
                            media_start_ms = break_offset
                        cursor = self._append_asset(
                            items, selected_program, cursor, segment.slug,
                            group_number, is_program=True, context=context,
                            programming_block_id=programming_block_number,
                            media_start_ms=media_start_ms,
                            part_label=f"part_{len(plans) + 1}",
                        )
                    else:
                        cursor = self._append_asset(
                            items,
                            selected_program,
                            cursor,
                            segment.slug,
                            group_number,
                            is_program=True,
                            context=context,
                            programming_block_id=programming_block_number,
                        )
                    for interstitial in boundary_midrolls.get(index, []):
                        cursor = self._append_asset(
                            items,
                            interstitial,
                            cursor,
                            segment.slug,
                            group_number,
                            is_program=False,
                            context=context,
                            programming_block_id=programming_block_number,
                        )
                        last_used[interstitial.asset_id] = cursor
                    last_used[selected_program.asset_id] = cursor
                    if holiday_overlay is None:
                        used_programs.add(selected_program.asset_id)

                if reserve_tail:
                    # Tell viewers what follows as soon as the programming
                    # finishes, before the closing ad pod fills the clock slot.
                    cursor = self._append_announcement(
                        items,
                        cursor,
                        segment.slug,
                        context,
                        programming_block_id=None,
                    )

                # Use commercials as ordinary end-of-package fill when they
                # fit, but never displace programming to preserve a pod.
                tail_commercials = 0
                tail_holiday_commercials = sum(
                    1
                    for item in midroll_items
                    if item.media_type in {"commercial", "promo"}
                    and item.collection_type == "holiday"
                )
                tail_holiday_slots = (
                    None
                    if holiday_commercial_max is None
                    else max(0, holiday_commercial_max - tail_holiday_commercials)
                )
                # Keep the closing pod short when a long episode has multiple
                # internal act breaks. Any extra clock time can use filler or
                # the channel graphic rather than another oversized ad run.
                tail_ad_cap_ms = (
                    self.midroll_max_pod_ms if planned_breaks >= 2 else None
                )
                tail_ad_spent_ms = 0
                tail_pack = self._gap_commercial_pack(
                    commercials,
                    rng,
                    cursor,
                    last_used,
                    min(
                        round((fill_end - cursor).total_seconds() * 1000),
                        tail_ad_cap_ms if tail_ad_cap_ms is not None else package_ms,
                    ),
                    0,
                    tail_holiday_slots,
                    max_items=self.commercial_max,
                    reserved_ids=reserved_ids,
                )
                for selected in tail_pack:
                    cursor = self._append_asset(
                        items,
                        selected,
                        cursor,
                        segment.slug,
                        last_group,
                        is_program=False,
                        context=context,
                        programming_block_id=programming_block_number,
                    )
                    last_used[selected.asset_id] = cursor
                    reserved_ids.add(selected.asset_id)
                    tail_commercials += 1
                    tail_ad_spent_ms += selected.duration_ms
                    if selected.collection_type == "holiday":
                        tail_holiday_commercials += 1

                while cursor < fill_end and filler:
                    tail_remaining = round(
                        (fill_end - cursor).total_seconds() * 1000
                    )
                    fitting = [
                        item for item in filler
                        if item.duration_ms <= tail_remaining
                        and item.asset_id not in reserved_ids
                        and (
                            item.media_type != "promo"
                            or item.collection_type != "holiday"
                            or holiday_commercial_max is None
                            or tail_holiday_commercials < holiday_commercial_max
                        )
                    ]
                    if not fitting:
                        break
                    selected = self._pick_interstitial(
                        fitting,
                        rng,
                        cursor,
                        last_used,
                        self.bumper_cooldown,
                    )
                    if selected is None:
                        break
                    cursor = self._append_asset(
                        items,
                        selected,
                        cursor,
                        segment.slug,
                        None,
                        is_program=False,
                        context=context,
                        programming_block_id=programming_block_number,
                    )
                    last_used[selected.asset_id] = cursor
                    reserved_ids.add(selected.asset_id)

                    if selected.media_type == "promo" and selected.collection_type == "holiday":
                        tail_holiday_commercials += 1

                # A bounded best-fit overflow pod gets one last chance before
                # the channel graphic absorbs whatever time remains.
                if cursor < fill_end and commercials:
                    gap_remaining = round(
                        (fill_end - cursor).total_seconds() * 1000
                    )
                    if tail_ad_cap_ms is not None:
                        gap_remaining = min(
                            gap_remaining, max(0, tail_ad_cap_ms - tail_ad_spent_ms)
                        )
                    holiday_slots = (
                        None
                        if holiday_commercial_max is None
                        else max(0, holiday_commercial_max - tail_holiday_commercials)
                    )
                    overflow_commercials = self._gap_commercial_pack(
                        commercials,
                        rng,
                        cursor,
                        last_used,
                        gap_remaining,
                        0,
                        holiday_slots,
                        max_items=max(0, self.commercial_max - tail_commercials),
                        reserved_ids=reserved_ids,
                    )
                    for selected in overflow_commercials:
                        cursor = self._append_asset(
                            items,
                            selected,
                            cursor,
                            segment.slug,
                            last_group,
                            is_program=False,
                            context=context,
                            programming_block_id=programming_block_number,
                        )
                        last_used[selected.asset_id] = cursor
                        reserved_ids.add(selected.asset_id)
                        tail_commercials += 1
                        tail_ad_spent_ms += selected.duration_ms
                        if selected.collection_type == "holiday":
                            tail_holiday_commercials += 1

                if cursor < fill_end:
                    cursor, last_graphic_at = self._append_graphic_if_fit(
                        items, channel_slug, cursor, fill_end, segment.slug, context,
                        last_graphic_at, rng,
                        programming_block_id=programming_block_number,
                        ignore_cooldown=True,
                    )

                if cursor < fill_end:
                    items.append(
                        TimelineItem(
                            None,
                            cursor,
                            fill_end,
                            None,
                            None,
                            "continuity",
                            "CRTV Sim Continuity",
                            segment.slug,
                            None,
                            len(items),
                            False,
                            context.owner_slug,
                            context.guide_label,
                            json.dumps(context.explanation(), sort_keys=True),
                            False,
                            None,
                            programming_block_number,
                        )
                    )
                    cursor = fill_end
                if not reserve_tail:
                    pending_announcement = True
                cursor = package_end

            state[state_key] = (groups, pointers, rotation, rotation_index)
            # A daypart-closing remainder may use a bounded commercial pod,
            # but never loose bumpers. Future generated information screens
            # belong between explicit filler and plain continuity.
            remainder_commercials = 0
            remainder_holiday_commercials = 0
            remainder_selected_ids: set[int] = set()
            remainder_pack = self._gap_commercial_pack(
                commercials,
                rng,
                cursor,
                last_used,
                round((segment.ends_at - cursor).total_seconds() * 1000),
                0,
                holiday_commercial_max,
                max_items=self.commercial_max,
            )
            for selected in remainder_pack:
                cursor = self._append_asset(
                    items,
                    selected,
                    cursor,
                    segment.slug,
                    None,
                    is_program=False,
                    context=context,
                )
                last_used[selected.asset_id] = cursor
                remainder_selected_ids.add(selected.asset_id)
                remainder_commercials += 1
                if selected.collection_type == "holiday":
                    remainder_holiday_commercials += 1

            short_pool = filler
            while cursor < segment.ends_at and short_pool:
                remaining_ms = round((segment.ends_at - cursor).total_seconds() * 1000)
                fitting = [
                    item for item in short_pool if item.duration_ms <= remaining_ms
                    and (
                        item.media_type != "promo"
                        or item.collection_type != "holiday"
                        or holiday_commercial_max is None
                        or remainder_holiday_commercials < holiday_commercial_max
                    )
                ]
                if not fitting:
                    break
                selected = self._pick_interstitial(
                    fitting, rng, cursor, last_used, self.bumper_cooldown
                )
                if selected is None:
                    break
                cursor = self._append_asset(
                    items,
                    selected,
                    cursor,
                    segment.slug,
                    None,
                    is_program=False,
                    context=context,
                )
                last_used[selected.asset_id] = cursor
                if selected.media_type == "promo" and selected.collection_type == "holiday":
                    remainder_holiday_commercials += 1
            cursor, last_graphic_at = self._append_graphic_if_fit(
                items, channel_slug, cursor, segment.ends_at, segment.slug, context,
                last_graphic_at, rng, programming_block_id=None,
            )
            if cursor < segment.ends_at:
                cursor, last_graphic_at = self._append_graphic_if_fit(
                    items, channel_slug, cursor, segment.ends_at, segment.slug,
                    context, last_graphic_at, rng, programming_block_id=None,
                    ignore_cooldown=True,
                )
            if cursor < segment.ends_at:
                items.append(
                    TimelineItem(
                        None,
                        cursor,
                        segment.ends_at,
                        None,
                        None,
                        "continuity",
                        "CRTV Sim Continuity",
                        segment.slug,
                        None,
                        len(items),
                        False,
                        context.owner_slug,
                        context.guide_label,
                        json.dumps(context.explanation(), sort_keys=True),
                    )
                )
                cursor = segment.ends_at
            carried_cursor = cursor
        if pending_announcement and items and items[-1].ends_at == day_end:
            duration = timedelta(
                seconds=max(
                    1,
                    int(
                        self.config.data.get("announcements", {}).get(
                            "opportunity_duration_seconds", 30
                        )
                    ),
                )
            )
            final = items[-1]
            if final.entry_type == "continuity" and final.ends_at - final.starts_at >= duration:
                signoff_start = final.ends_at - duration
                if signoff_start > final.starts_at:
                    items[-1] = replace(final, ends_at=signoff_start)
                else:
                    items.pop()
                items.append(
                    TimelineItem(
                        None, signoff_start, day_end, None, None,
                        "announcement_opportunity", "Station Identification",
                        final.daypart_slug, None, len(items), False,
                        final.programming_owner, final.guide_label,
                        final.explanation_json,
                    )
                )
        for overlay in holiday_overlays:
            overlay["saved"]["last_date"] = schedule_date.isoformat()
            overlay["saved"]["airings"] = overlay["airings"]
            overlay["saved"]["deferred"] = [
                {"daypart": part, "slot": int(slot), "series": series}
                for part, assignments in overlay["plan"].items()
                for slot, series in assignments.items()
                if (part, slot) not in overlay["done"]
            ] + [{"daypart": part, "slot": None, "series": entry["series"],
                  "reason": "No available compatible slot"}
                 for part, queue in overlay["waiting"].items() for entry in queue]
        return self._link_announcement_blocks(items)

    @staticmethod
    def _validate_items(
        items: list[TimelineItem], starts_at: datetime, ends_at: datetime
    ) -> list[str]:
        errors: list[str] = []
        if not items:
            return ["Schedule has no entries"]
        expected = starts_at
        for position, item in enumerate(items):
            if item.starts_at != expected:
                errors.append(
                    f"Entry {position} starts at {_iso(item.starts_at)}; "
                    f"expected {_iso(expected)}"
                )
            if item.ends_at <= item.starts_at:
                errors.append(f"Entry {position} has non-positive duration")
            if item.asset_id is not None:
                media_ms = (item.media_end_ms or 0) - (item.media_start_ms or 0)
                wall_ms = round((item.ends_at - item.starts_at).total_seconds() * 1000)
                if abs(media_ms - wall_ms) > 2:
                    errors.append(f"Entry {position} media duration differs from wall time")
            expected = item.ends_at
        if items[0].starts_at != starts_at:
            errors.append("Schedule does not start at local midnight")
        if expected != ends_at:
            errors.append("Schedule does not end at the next local midnight")
        return errors

    def generate_day(
        self,
        channel_slug: str,
        schedule_date: date,
        *,
        reroll_token: str | None = None,
    ) -> GenerationResult:
        self.rules.sync_catalog()
        self.blocks.sync_profiles()
        channel_id = self.ensure_channel(channel_slug)
        existing = self.connection.execute(
            """
            SELECT id, locked FROM schedule_generations
            WHERE channel_id = ? AND schedule_date = ? AND status = 'active'
            """,
            (channel_id, schedule_date.isoformat()),
        ).fetchone()
        if existing is not None and existing["locked"]:
            raise ValueError(
                f"Active schedule {existing['id']} is locked and cannot be replaced"
            )
        preferences = self.schedule_preferences(channel_slug)
        recurring_state: dict[str, object] | None = None
        recurring_input_hash = ""
        recurring_eligible_assets: dict[str, set[int]] = {}
        if preferences["recurring_enabled"] and preferences["presentation_format"] != "content_only":
            recurring_state, recurring_input_hash = self._recurring_state_before(
                channel_id,
                channel_slug,
                schedule_date,
                unique_weekends=bool(preferences["unique_weekends"]),
            )
            recurring_eligible_assets = self._eligible_base_series_assets(channel_slug)
            if reroll_token is not None:
                template = self._recurring_template_kind(
                    schedule_date, bool(preferences["unique_weekends"])
                )
                slots = recurring_state.get("slots", {})
                if isinstance(slots, dict):
                    for key in list(slots):
                        if str(key).startswith(f"{template}:"):
                            slots[key] = {}
                borrowed = recurring_state.get("borrowed_weekend_slots", {})
                if isinstance(borrowed, dict):
                    for key in list(borrowed):
                        if str(key).startswith(f"{template}:"):
                            borrowed.pop(key, None)
        seed = self._seed(channel_slug, schedule_date, reroll_token)
        rng = random.Random(seed)
        scoped_allocation: dict[str, object] = {}
        timeline = self._build_timeline(
            channel_id,
            channel_slug,
            schedule_date,
            rng,
            reuse_recurring_template=reroll_token is None,
            recurring_state=recurring_state,
            recurring_eligible_assets=recurring_eligible_assets,
            scoped_allocation=scoped_allocation,
        )
        timeline = self._insert_weekly_special_promos(
            channel_slug, schedule_date, timeline
        )
        if recurring_state is not None:
            self._record_recurring_state(
                recurring_state,
                timeline,
                channel_slug,
                schedule_date,
                unique_weekends=bool(preferences["unique_weekends"]),
                all_eligible_assets=recurring_eligible_assets,
                scoped_waiting=scoped_allocation.get("waiting"),
                underlying_plan=scoped_allocation.get("underlying_plan"),
            )
        starts_at = datetime.combine(schedule_date, time.min, self.timezone)
        ends_at = datetime.combine(
            schedule_date + timedelta(days=1), time.min, self.timezone
        )
        errors = self._validate_items(timeline, starts_at, ends_at)
        validation_status = "valid" if not errors else "invalid"
        detail = "No gaps or overlaps." if not errors else "; ".join(errors)
        now = utc_now()

        try:
            self.connection.execute("BEGIN")
            cursor = self.connection.execute(
                """
                INSERT INTO schedule_generations(
                    channel_id, schedule_date, seed, configuration_fingerprint,
                    status, validation_status, validation_detail, generated_at
                ) VALUES (?, ?, ?, ?, 'building', ?, ?, ?)
                """,
                (
                    channel_id,
                    schedule_date.isoformat(),
                    seed,
                    self._configuration_fingerprint(channel_slug),
                    validation_status,
                    detail,
                    now,
                ),
            )
            generation_id = int(cursor.lastrowid)
            self.connection.executemany(
                """
                INSERT INTO schedule_entries(
                    generation_id, channel_id, asset_id, starts_at, ends_at,
                    media_start_ms, media_end_ms, entry_type, title,
                    daypart_slug, sequence_group, sequence_position, is_program,
                    programming_owner, guide_label, explanation_json,
                    protected_block, guide_listing_title
                    , programming_block_id, graphic_asset_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        generation_id,
                        channel_id,
                        item.asset_id,
                        _iso(item.starts_at),
                        _iso(item.ends_at),
                        item.media_start_ms,
                        item.media_end_ms,
                        item.entry_type,
                        item.title,
                        item.daypart_slug,
                        item.sequence_group,
                        item.sequence_position,
                        int(item.is_program),
                        item.programming_owner,
                        item.guide_label,
                        item.explanation_json,
                        int(item.protected_block),
                        item.guide_listing_title,
                        item.programming_block_id,
                        item.graphic_asset_id,
                    )
                    for item in timeline
                ],
            )
            entry_rows = self.connection.execute(
                """
                SELECT id, explanation_json
                FROM schedule_entries
                WHERE generation_id = ?
                """,
                (generation_id,),
            ).fetchall()
            influences = []
            for row in entry_rows:
                explanation = json.loads(row["explanation_json"])
                selected = explanation.get("selection", {})
                for rule in explanation.get("active_rules", []):
                    weight = (
                        float(selected.get("weight_multiplier", 1.0))
                        if selected.get("rule") == rule["rule"]
                        else 1.0
                    )
                    influences.append(
                        (
                            int(row["id"]),
                            rule["rule"],
                            rule["collection"],
                            rule["type"],
                            weight,
                            (
                                f"{rule['rule']} active as {rule['type']}"
                                + (
                                    f"; selected at {weight:g}x weight"
                                    if weight != 1.0
                                    else ""
                                )
                            ),
                        )
                    )
            if influences:
                self.connection.executemany(
                    """
                    INSERT INTO schedule_entry_influences(
                        entry_id, rule_slug, collection_slug, influence_type,
                        weight_multiplier, explanation
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    influences,
                )
            self._persist_announcement_opportunities(
                generation_id, channel_id, channel_slug, now
            )
            if errors:
                self.connection.execute(
                    "UPDATE schedule_generations SET status = 'rejected' WHERE id = ?",
                    (generation_id,),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE schedule_generations SET status = 'superseded'
                    WHERE channel_id = ? AND schedule_date = ? AND status = 'active'
                    """,
                    (channel_id, schedule_date.isoformat()),
                )
                self.connection.execute(
                    """
                    UPDATE schedule_generations
                    SET status = 'active', activated_at = ?
                    WHERE id = ?
                    """,
                    (now, generation_id),
                )
                if recurring_state is not None:
                    serialized_state = json.dumps(recurring_state, sort_keys=True)
                    self.connection.execute(
                        """INSERT INTO recurring_lineup_snapshots(
                               generation_id,channel_id,schedule_date,state_json,
                               state_hash,input_state_hash,updated_at
                           ) VALUES (?,?,?,?,?,?,?)""",
                        (
                            generation_id,
                            channel_id,
                            schedule_date.isoformat(),
                            serialized_state,
                            self._recurring_state_hash(recurring_state),
                            recurring_input_hash,
                            now,
                        ),
                    )
            self.connection.commit()
            self._recurring_grid_cache.clear()
        except Exception:
            self.connection.rollback()
            raise

        return GenerationResult(
            generation_id,
            channel_slug,
            schedule_date,
            len(timeline),
            len(
                {
                    (item.programming_block_id, item.asset_id)
                    for item in timeline
                    if item.is_program
                }
            ),
            sum(
                (item.ends_at - item.starts_at).total_seconds()
                for item in timeline
                if item.entry_type == "continuity"
            ),
            validation_status,
            detail,
        )

    def _insert_weekly_special_promos(
        self, channel_slug: str, schedule_date: date, timeline: list[TimelineItem]
    ) -> list[TimelineItem]:
        """Replace commercial spots with time-bounded weekly-special promos.

        Promo cadence is tied to each configured special's actual weekly start.
        A cadence window can only consume a commercial that already exists in
        that window; otherwise it expires. Competing specials are reduced to at
        most one promo in any rolling two-hour channel interval.
        """
        day_start = datetime.combine(schedule_date, time.min, self.timezone)
        day_end = datetime.combine(schedule_date + timedelta(days=1), time.min, self.timezone)
        proposals: list[tuple[int, datetime, datetime, Any]] = []
        promo_duration = 15.0
        channel_row = self.connection.execute(
            "SELECT id FROM channels WHERE slug=?", (channel_slug,)
        ).fetchone()
        disabled_collections: set[str] = set()
        if channel_row is not None:
            disabled_collections = {
                str(row["collection_slug"])
                for row in self.connection.execute(
                    "SELECT collection_slug FROM announcement_weekly_profiles "
                    "WHERE channel_id=? AND promo_enabled=0",
                    (int(channel_row["id"]),),
                )
            }
        for window in self.rules.windows:
            if not window.enabled or window.channel_slug != channel_slug:
                continue
            if window.collection_slug in disabled_collections:
                continue
            for start_day_offset in range(0, 8):
                airing_date = schedule_date + timedelta(days=start_day_offset)
                if airing_date.weekday() not in window.weekdays:
                    continue
                airing_at, airing_end_at = window.interval_for_start_date(
                    airing_date, self.timezone
                )
                target = airing_at - timedelta(days=6)
                while target < airing_at:
                    if day_start <= target < day_end:
                        slot_end = min(target + timedelta(hours=2), airing_at)
                        suitable = [
                            (index, item)
                            for index, item in enumerate(timeline)
                            if item.entry_type == "commercial"
                            and item.starts_at >= target
                            and item.starts_at < slot_end
                            and item.ends_at <= slot_end
                            and (item.ends_at - item.starts_at).total_seconds()
                                >= promo_duration
                        ]
                        if suitable:
                            index, _item = suitable[0]
                            proposals.append((index, airing_at, target, window, airing_end_at))
                    target += timedelta(hours=2)

        if not proposals:
            return timeline

        # A single spot may fall into overlapping cadence windows. Prefer the
        # nearest upcoming special for that spot, then resolve near-simultaneous
        # spots by the same soonest-airing rule.
        by_item: dict[int, tuple[int, datetime, datetime, Any, datetime]] = {}
        for proposal in proposals:
            index, airing_at, target, window, airing_end_at = proposal
            prior = by_item.get(index)
            if prior is None or airing_at < prior[1]:
                by_item[index] = proposal
        ordered = sorted(
            by_item.values(), key=lambda item: (timeline[item[0]].starts_at, item[1])
        )
        selected: list[tuple[int, datetime, datetime, Any]] = []
        while ordered:
            first_start = timeline[ordered[0][0]].starts_at
            competing = [
                item for item in ordered
                if timeline[item[0]].starts_at < first_start + timedelta(hours=2)
            ]
            winner = min(
                competing,
                key=lambda item: (item[1], timeline[item[0]].starts_at),
            )
            selected.append(winner)
            competing_ids = {item[0] for item in competing}
            ordered = [item for item in ordered if item[0] not in competing_ids]

        replacements: dict[int, list[TimelineItem]] = {}
        for index, airing_at, target, window, airing_end_at in selected:
            item = timeline[index]
            facts = {
                "weekly_special_promo": {
                    "window_slug": window.slug,
                    "collection_slug": window.collection_slug,
                    "special_name": window.guide_label,
                    "airing_at": airing_at.isoformat(timespec="seconds"),
                    "airing_ends_at": airing_end_at.isoformat(timespec="seconds"),
                    "cadence_window_start": target.isoformat(timespec="seconds"),
                }
            }
            try:
                explanation = json.loads(item.explanation_json)
            except (TypeError, json.JSONDecodeError):
                explanation = {}
            explanation.update(facts)
            promo_end = item.starts_at + timedelta(seconds=promo_duration)
            promo = replace(
                item,
                ends_at=promo_end,
                asset_id=None,
                media_start_ms=None,
                media_end_ms=None,
                entry_type="weekly_special_promo_opportunity",
                title=f"Weekly Special Promo: {window.guide_label}",
                explanation_json=json.dumps(explanation, sort_keys=True),
            )
            replacement = [promo]
            if promo_end < item.ends_at:
                # Reuse only complete, already eligible items from this same
                # schedule scope. Never cut an ordinary commercial short.
                cursor = promo_end
                used = {item.asset_id}
                while cursor < item.ends_at:
                    remaining = (item.ends_at - cursor).total_seconds()
                    choices = [candidate for candidate in timeline
                               if candidate.asset_id is not None
                               and candidate.asset_id not in used
                               and candidate.entry_type in {"commercial", "filler"}
                               and candidate.daypart_slug == item.daypart_slug
                               and candidate.programming_owner == item.programming_owner
                               and not candidate.protected_block
                               and (candidate.ends_at - candidate.starts_at).total_seconds()
                                   <= remaining]
                    if not choices:
                        break
                    candidate = max(choices, key=lambda choice: (
                        (choice.ends_at - choice.starts_at).total_seconds(),
                        -abs((choice.starts_at - item.starts_at).total_seconds())))
                    length = candidate.ends_at - candidate.starts_at
                    replacement.append(replace(candidate, starts_at=cursor,
                                               ends_at=cursor + length,
                                               sequence_group=item.sequence_group))
                    used.add(candidate.asset_id)
                    cursor += length
            if replacement[-1].ends_at < item.ends_at:
                # The channel's interstitial holds any remainder that ordinary
                # spots cannot fill exactly.
                from retrotv.graphics import GraphicsService
                graphic = GraphicsService(self.connection, self.config).select_asset(
                    channel_slug, "interstitials", replacement[-1].ends_at,
                    weekly_collection_slug=(window.collection_slug if
                                            replacement[-1].ends_at < airing_at else None),
                )
                replacement.append(replace(
                    item, asset_id=None, starts_at=replacement[-1].ends_at,
                    media_start_ms=None, media_end_ms=None,
                    entry_type="graphic_interstitial" if graphic else "continuity",
                    graphic_asset_id=int(graphic["id"]) if graphic else None,
                    title="Station Interstitial" if graphic else "Continuity",
                    explanation_json="{}",
                ))
            replacements[index] = replacement
        result = [part for index, item in enumerate(timeline)
                  for part in replacements.get(index, [item])]
        result = [replace(item, sequence_position=index)
                  for index, item in enumerate(result)]
        return result

    def reroll_day(
        self,
        channel_slug: str,
        schedule_date: date,
        *,
        reroll_token: str,
    ) -> tuple[GenerationResult, list[date], list[date]]:
        """Create a fresh lineup and propagate it through generated recurrence.

        A normal recurring generation inherits the most recent matching lineup.
        A user-requested re-roll deliberately bypasses that inheritance so its
        ordinary programming becomes the new base. Already-generated future
        dates in the same recurrence class are then rebuilt chronologically.
        Locked dates are never replaced.
        """
        token = str(reroll_token).strip()
        if not token:
            raise ValueError("A re-roll token is required")
        result = self.generate_day(
            channel_slug,
            schedule_date,
            reroll_token=token,
        )
        preferences = self.schedule_preferences(channel_slug)
        if not preferences["recurring_enabled"]:
            return result, [], []

        channel_id = self.ensure_channel(channel_slug)
        rows = self.connection.execute(
            """
            SELECT schedule_date, locked
            FROM schedule_generations
            WHERE channel_id=? AND schedule_date>? AND status='active'
            ORDER BY schedule_date
            """,
            (channel_id, schedule_date.isoformat()),
        ).fetchall()
        regenerated: list[date] = []
        skipped_locked: list[date] = []
        for row in rows:
            future_date = date.fromisoformat(str(row["schedule_date"]))
            if bool(row["locked"]):
                skipped_locked.append(future_date)
                # Lineup snapshots flow across weekday/weekend classes. A
                # locked date is therefore a boundary for every later class.
                break
            self.generate_day(channel_slug, future_date)
            regenerated.append(future_date)
        return result, regenerated, skipped_locked

    def _persist_announcement_opportunities(
        self,
        generation_id: int,
        channel_id: int,
        channel_slug: str,
        now: str,
    ) -> None:
        graphics = GraphicsService(self.connection, self.config)
        opportunities = self.connection.execute(
            """
            SELECT id, starts_at, ends_at, programming_block_id, entry_type,
                   explanation_json
            FROM schedule_entries
            WHERE generation_id=? AND entry_type IN (
                'announcement_opportunity','weekly_special_promo_opportunity'
            )
            ORDER BY starts_at
            """,
            (generation_id,),
        ).fetchall()
        for opportunity in opportunities:
            promo = opportunity["entry_type"] == "weekly_special_promo_opportunity"
            if promo:
                explanation = json.loads(opportunity["explanation_json"] or "{}")
                promo_facts = explanation.get("weekly_special_promo", {})
                weekly_collection_slug = promo_facts.get("collection_slug")
                facts = {
                    "channel": channel_slug,
                    "weekly_special_promo": promo_facts,
                    "lineup": [],
                }
            else:
                weekly_collection_slug = graphics.weekly_collection_at(
                    channel_slug, datetime.fromisoformat(str(opportunity["starts_at"]))
                )
                programs = self.connection.execute(
                    """
                    SELECT se.programming_block_id, MIN(se.starts_at) AS starts_at,
                           COALESCE(MAX(a.series_title), MAX(se.guide_listing_title),
                                    MAX(se.title)) AS title
                    FROM schedule_entries se
                    LEFT JOIN assets a ON a.id=se.asset_id
                    WHERE se.generation_id=? AND se.is_program=1
                      AND se.starts_at >= ?
                    GROUP BY se.programming_block_id
                    ORDER BY MIN(se.starts_at)
                    LIMIT 3
                    """,
                    (generation_id, opportunity["ends_at"]),
                ).fetchall()
                facts = {
                    "channel": channel_slug,
                    "station_identification": not bool(programs),
                    "listings": [
                        {
                            "programming_block_id": row["programming_block_id"],
                            "starts_at": row["starts_at"],
                            "title": row["title"] or "TBD",
                        }
                        for row in programs
                    ],
                }
            cache_key = hashlib.sha256(
                json.dumps(
                    {
                        "generation": generation_id,
                        "entry": int(opportunity["id"]),
                        "facts": facts,
                        "renderer": 4,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            self.connection.execute(
                """
                INSERT INTO announcement_opportunities(
                    schedule_entry_id, generation_id, channel_id,
                    programming_block_id, starts_at, ends_at, facts_json,
                    cache_key, weekly_collection_slug, opportunity_type, render_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    int(opportunity["id"]), generation_id, channel_id,
                    opportunity["programming_block_id"], opportunity["starts_at"],
                    opportunity["ends_at"], json.dumps(facts, sort_keys=True),
                    cache_key, weekly_collection_slug,
                    "weekly_special_promo" if promo else "up_next", now, now,
                ),
            )

    def active_generation(self, channel_slug: str, schedule_date: date):
        return self.connection.execute(
            """
            SELECT sg.*, c.slug AS channel_slug, c.display_name
            FROM schedule_generations sg
            JOIN channels c ON c.id = sg.channel_id
            WHERE c.slug = ? AND sg.schedule_date = ? AND sg.status = 'active'
            """,
            (channel_slug, schedule_date.isoformat()),
        ).fetchone()

    def generation_is_current(
        self,
        channel_slug: str,
        schedule_date: date,
    ) -> bool:
        active = self.active_generation(channel_slug, schedule_date)
        if active is None:
            return False
        if bool(active["locked"]):
            return True
        if active["configuration_fingerprint"] != self._configuration_fingerprint(channel_slug):
            return False
        preferences = self.schedule_preferences(channel_slug)
        if not preferences["recurring_enabled"] or preferences["presentation_format"] == "content_only":
            return True
        channel_id = self.ensure_channel(channel_slug)
        _state, expected_input_hash = self._recurring_state_before(
            channel_id,
            channel_slug,
            schedule_date,
            unique_weekends=bool(preferences["unique_weekends"]),
        )
        snapshot = self.connection.execute(
            "SELECT input_state_hash FROM recurring_lineup_snapshots WHERE generation_id=?",
            (int(active["id"]),),
        ).fetchone()
        return bool(
            snapshot is not None
            and str(snapshot["input_state_hash"]) == expected_input_hash
        )

    def entries(self, generation_id: int, *, programs_only: bool = False):
        where = " AND se.is_program = 1" if programs_only else ""
        return self.connection.execute(
            f"""
            SELECT se.*, a.source_relative_path, a.series_title,
                   m.duration_seconds AS source_duration_seconds,
                   n.proxy_status, n.proxy_relative_path
            FROM schedule_entries se
            LEFT JOIN assets a ON a.id = se.asset_id
            LEFT JOIN asset_media_info m ON m.asset_id = se.asset_id
            LEFT JOIN normalization_profiles n ON n.asset_id = se.asset_id
            WHERE se.generation_id = ? {where}
            ORDER BY se.sequence_position
            """,
            (generation_id,),
        ).fetchall()

    def validate_generation(self, generation_id: int) -> tuple[str, str]:
        generation = self.connection.execute(
            """
            SELECT sg.*, c.slug AS channel_slug
            FROM schedule_generations sg
            JOIN channels c ON c.id = sg.channel_id
            WHERE sg.id = ?
            """,
            (generation_id,),
        ).fetchone()
        if generation is None:
            raise ValueError(f"Schedule generation {generation_id} does not exist")
        rows = self.entries(generation_id)
        items = [
            TimelineItem(
                row["asset_id"],
                datetime.fromisoformat(row["starts_at"]),
                datetime.fromisoformat(row["ends_at"]),
                row["media_start_ms"],
                row["media_end_ms"],
                row["entry_type"],
                row["title"],
                row["daypart_slug"],
                row["sequence_group"],
                row["sequence_position"],
                bool(row["is_program"]),
                row["programming_owner"],
                row["guide_label"],
                row["explanation_json"],
                bool(row["protected_block"]),
                row["guide_listing_title"],
                row["programming_block_id"],
                row["graphic_asset_id"],
            )
            for row in rows
        ]
        schedule_date = date.fromisoformat(generation["schedule_date"])
        starts_at = datetime.combine(schedule_date, time.min, self.timezone)
        ends_at = datetime.combine(
            schedule_date + timedelta(days=1), time.min, self.timezone
        )
        errors = self._validate_items(items, starts_at, ends_at)
        for row in rows:
            if row["asset_id"] is None:
                continue
            source_duration = row["source_duration_seconds"]
            if source_duration is None:
                errors.append(f"Entry {row['id']} has no source duration")
                continue
            if row["media_start_ms"] < 0:
                errors.append(f"Entry {row['id']} has a negative media offset")
            maximum_ms = round(float(source_duration) * 1000) + 1000
            if row["media_end_ms"] > maximum_ms:
                errors.append(
                    f"Entry {row['id']} ends beyond its source duration"
                )
        status = "valid" if not errors else "invalid"
        detail = "No gaps or overlaps." if not errors else "; ".join(errors)
        self.connection.execute(
            """
            UPDATE schedule_generations
            SET validation_status = ?, validation_detail = ?
            WHERE id = ?
            """,
            (status, detail, generation_id),
        )
        self.connection.commit()
        return status, detail
