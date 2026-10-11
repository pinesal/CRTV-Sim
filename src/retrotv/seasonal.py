from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from retrotv.classification import humanize, slugify
from retrotv.db import utc_now


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

DEFAULT_WEIGHTS = {
    "commercial": 3.0,
    "bumper": 2.0,
    "promo": 2.0,
    "episode": 1.5,
    "movie": 1.5,
}


def _parse_clock(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid programming-window time: {value!r}") from exc


def _month_day(value: str) -> tuple[int, int]:
    try:
        month, day = (int(part) for part in value.split("-", 1))
        date(2000, month, day)
        return month, day
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid annual month-day: {value!r}") from exc


@dataclass(frozen=True)
class HolidayRule:
    slug: str
    start_month_day: str
    end_month_day: str
    mode: str
    priority: int
    weights: dict[str, float]
    maximum_per_break: int
    cooldown_seconds: int
    shared_movie_channels: tuple[str, ...]
    enabled: bool = True

    def active_on(self, value: date) -> bool:
        current = (value.month, value.day)
        start = _month_day(self.start_month_day)
        end = _month_day(self.end_month_day)
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def allows_shared_movie(self, channel_slug: str) -> bool:
        return "*" in self.shared_movie_channels or channel_slug in self.shared_movie_channels


@dataclass(frozen=True)
class ProgrammingWindow:
    slug: str
    channel_slug: str
    collection_slug: str
    weekdays: tuple[int, ...]
    start: time
    end: time
    priority: int
    guide_label: str
    mode: str = "replace"
    enabled: bool = True

    def interval_for_start_date(self, value: date, timezone) -> tuple[datetime, datetime]:
        starts_at = datetime.combine(value, self.start, timezone)
        end_date = value + timedelta(days=1) if self.end <= self.start else value
        ends_at = datetime.combine(end_date, self.end, timezone)
        return starts_at, ends_at

    def active_interval(self, value: datetime) -> tuple[datetime, datetime] | None:
        local_date = value.date()
        for start_date in (local_date - timedelta(days=1), local_date):
            if start_date.weekday() not in self.weekdays:
                continue
            starts_at, ends_at = self.interval_for_start_date(start_date, value.tzinfo)
            if starts_at <= value < ends_at:
                return starts_at, ends_at
        return None


@dataclass(frozen=True)
class RuleContext:
    owner: ProgrammingWindow | None
    holidays: tuple[HolidayRule, ...]

    @property
    def owner_slug(self) -> str:
        return self.owner.slug if self.owner else "base"

    @property
    def guide_label(self) -> str | None:
        return self.owner.guide_label if self.owner else None

    def explanation(self) -> dict[str, Any]:
        return {
            "programming_owner": self.owner_slug,
            "guide_label": self.guide_label,
            "active_rules": [
                *(
                    [
                        {
                            "rule": self.owner.slug,
                            "collection": self.owner.collection_slug,
                            "type": "weekly_owner",
                            "priority": self.owner.priority,
                        }
                    ]
                    if self.owner
                    else []
                ),
                *[
                    {
                        "rule": holiday.slug,
                        "collection": holiday.slug,
                        "type": "holiday_modifier",
                        "priority": holiday.priority,
                        "mode": holiday.mode,
                    }
                    for holiday in self.holidays
                ],
            ],
        }


class SeasonalRuleEngine:
    def __init__(self, connection, config, timezone):
        self.connection = connection
        self.config = config
        self.timezone = timezone
        settings = config.data.get("seasonal", {})
        self.holidays = self._load_holidays(
            self._named_rules(settings.get("holidays", []))
        )
        self.windows = self._load_windows(
            self._named_rules(settings.get("windows", []))
        )
        self._validate_conflicts()

    @staticmethod
    def _named_rules(
        raw_rules: Iterable[dict[str, Any]] | dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if isinstance(raw_rules, dict):
            return [
                {"slug": slug, **settings}
                for slug, settings in raw_rules.items()
            ]
        return list(raw_rules)

    @staticmethod
    def _load_holidays(raw_rules: Iterable[dict[str, Any]]) -> tuple[HolidayRule, ...]:
        rules = []
        for raw in raw_rules:
            slug = slugify(str(raw["slug"]))
            mode = str(raw.get("mode", "boost"))
            if mode not in {"decorate", "boost", "replace"}:
                raise ValueError(f"Holiday {slug!r} has invalid mode {mode!r}")
            raw_weights = raw.get("weights", {})
            weights = {
                media_type: float(raw_weights.get(media_type, default))
                for media_type, default in DEFAULT_WEIGHTS.items()
            }
            rules.append(
                HolidayRule(
                    slug=slug,
                    start_month_day=str(raw["start"]),
                    end_month_day=str(raw["end"]),
                    mode=mode,
                    priority=int(raw.get("priority", 200)),
                    weights=weights,
                    maximum_per_break=max(0, int(raw.get("maximum_per_break", 2))),
                    cooldown_seconds=max(0, int(raw.get("cooldown_seconds", 3600))),
                    shared_movie_channels=tuple(
                        slugify(str(value))
                        if str(value) != "*"
                        else "*"
                        for value in raw.get("shared_movie_channels", [])
                    ),
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        return tuple(rules)

    @staticmethod
    def _load_windows(raw_rules: Iterable[dict[str, Any]]) -> tuple[ProgrammingWindow, ...]:
        rules = []
        for raw in raw_rules:
            slug = slugify(str(raw["slug"]))
            weekdays = tuple(
                WEEKDAYS[str(value).casefold()]
                for value in raw.get("weekdays", [])
            )
            if not weekdays:
                raise ValueError(f"Programming window {slug!r} requires weekdays")
            rules.append(
                ProgrammingWindow(
                    slug=slug,
                    channel_slug=slugify(str(raw["channel"])),
                    collection_slug=slugify(str(raw.get("collection", slug))),
                    weekdays=weekdays,
                    start=_parse_clock(str(raw["start"])),
                    end=_parse_clock(str(raw["end"])),
                    priority=int(raw.get("priority", 300)),
                    guide_label=str(raw.get("guide_label", humanize(slug).title())),
                    mode=str(raw.get("mode", "replace")),
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        return tuple(rules)

    def active_holidays(self, value: date) -> tuple[HolidayRule, ...]:
        return tuple(
            sorted(
                (item for item in self.holidays if item.enabled and item.active_on(value)),
                key=lambda item: (-item.priority, item.slug),
            )
        )

    def active_owner(self, channel_slug: str, value: datetime) -> ProgrammingWindow | None:
        active = [
            item
            for item in self.windows
            if item.enabled
            and item.channel_slug == channel_slug
            and item.active_interval(value) is not None
        ]
        if not active:
            return None
        active.sort(key=lambda item: (-item.priority, item.slug))
        if len(active) > 1 and active[0].priority == active[1].priority:
            raise ValueError(
                f"Equal-priority programming conflict on {channel_slug}: "
                f"{active[0].slug} and {active[1].slug}"
            )
        return active[0]

    def context(self, channel_slug: str, value: datetime) -> RuleContext:
        return RuleContext(
            self.active_owner(channel_slug, value),
            self.active_holidays(value.date()),
        )

    def boundaries_for_day(self, channel_slug: str, schedule_date: date) -> set[datetime]:
        day_start = datetime.combine(schedule_date, time.min, self.timezone)
        day_end = datetime.combine(schedule_date + timedelta(days=1), time.min, self.timezone)
        boundaries: set[datetime] = set()
        for window in self.windows:
            if not window.enabled or window.channel_slug != channel_slug:
                continue
            for start_date in (schedule_date - timedelta(days=1), schedule_date):
                if start_date.weekday() not in window.weekdays:
                    continue
                starts_at, ends_at = window.interval_for_start_date(start_date, self.timezone)
                if day_start < starts_at < day_end:
                    boundaries.add(starts_at)
                if day_start < ends_at < day_end:
                    boundaries.add(ends_at)
        return boundaries

    def _validate_conflicts(self) -> None:
        # A representative Monday-through-Sunday week catches weekly overlaps,
        # including windows carried in from the previous date.
        monday = date(2026, 1, 5)
        for day_offset in range(7):
            current_date = monday + timedelta(days=day_offset)
            boundaries = {time.min}
            for window in self.windows:
                if not window.enabled:
                    continue
                boundaries.add(window.start)
                boundaries.add(window.end)
            for boundary in boundaries:
                moment = datetime.combine(current_date, boundary, self.timezone)
                by_channel: dict[str, list[ProgrammingWindow]] = {}
                for window in self.windows:
                    if window.enabled and window.active_interval(moment):
                        by_channel.setdefault(window.channel_slug, []).append(window)
                for channel, matches in by_channel.items():
                    matches.sort(key=lambda item: -item.priority)
                    if len(matches) > 1 and matches[0].priority == matches[1].priority:
                        raise ValueError(
                            f"Equal-priority programming conflict on {channel}: "
                            f"{matches[0].slug} and {matches[1].slug}"
                        )

    def sync_catalog(self) -> None:
        configured: list[tuple[str, str, str, str]] = []
        for holiday in self.holidays:
            configured.append((holiday.slug, "holiday", "*", humanize(holiday.slug).title()))
        for window in self.windows:
            configured.append(
                (
                    window.collection_slug,
                    "weekly",
                    window.channel_slug,
                    window.guide_label,
                )
            )
        rows = self.connection.execute(
            """
            SELECT DISTINCT collection_slug, collection_type, channel_slug
            FROM assets
            WHERE collection_slug IS NOT NULL AND collection_type IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            configured.append(
                (
                    row["collection_slug"],
                    row["collection_type"],
                    row["channel_slug"] or "*",
                    humanize(row["collection_slug"]).title(),
                )
            )
        for slug, collection_type, channel, label in configured:
            self.connection.execute(
                """
                INSERT INTO programming_collections(
                    slug, collection_type, channel_slug, display_name, enabled
                ) VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(slug, collection_type, channel_slug) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = 1
                """,
                (slug, collection_type, channel, label),
            )
        self.connection.execute("DELETE FROM asset_collection_memberships")
        self.connection.execute(
            """
            INSERT INTO asset_collection_memberships(asset_id, collection_id)
            SELECT a.id, pc.id
            FROM assets a
            JOIN programming_collections pc
              ON pc.slug = a.collection_slug
             AND pc.collection_type = a.collection_type
             AND pc.channel_slug = COALESCE(a.channel_slug, '*')
            WHERE a.collection_slug IS NOT NULL
            """
        )
        now = utc_now()
        for holiday in self.holidays:
            settings = {
                "weights": holiday.weights,
                "maximum_per_break": holiday.maximum_per_break,
                "cooldown_seconds": holiday.cooldown_seconds,
                "shared_movie_channels": holiday.shared_movie_channels,
            }
            self._upsert_rule(
                holiday.slug,
                "holiday",
                holiday.slug,
                None,
                holiday.mode,
                holiday.priority,
                holiday.start_month_day,
                holiday.end_month_day,
                None,
                None,
                None,
                None,
                settings,
                holiday.enabled,
                now,
            )
        for window in self.windows:
            self._upsert_rule(
                window.slug,
                "weekly",
                window.collection_slug,
                window.channel_slug,
                window.mode,
                window.priority,
                None,
                None,
                json.dumps(list(window.weekdays)),
                window.start.isoformat(timespec="minutes"),
                window.end.isoformat(timespec="minutes"),
                window.guide_label,
                {},
                window.enabled,
                now,
            )
        self.connection.commit()

    def _upsert_rule(
        self,
        slug: str,
        rule_type: str,
        collection_slug: str,
        channel_slug: str | None,
        mode: str,
        priority: int,
        start_month_day: str | None,
        end_month_day: str | None,
        weekdays_json: str | None,
        start_time: str | None,
        end_time: str | None,
        guide_label: str | None,
        settings: dict[str, Any],
        enabled: bool,
        now: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO calendar_rules(
                slug, rule_type, collection_slug, channel_slug, mode, priority,
                start_month_day, end_month_day, weekdays_json, start_time,
                end_time, guide_label, settings_json, enabled, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                rule_type = excluded.rule_type,
                collection_slug = excluded.collection_slug,
                channel_slug = excluded.channel_slug,
                mode = excluded.mode,
                priority = excluded.priority,
                start_month_day = excluded.start_month_day,
                end_month_day = excluded.end_month_day,
                weekdays_json = excluded.weekdays_json,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                guide_label = excluded.guide_label,
                settings_json = excluded.settings_json,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                slug,
                rule_type,
                collection_slug,
                channel_slug,
                mode,
                priority,
                start_month_day,
                end_month_day,
                weekdays_json,
                start_time,
                end_time,
                guide_label,
                json.dumps(settings, sort_keys=True),
                int(enabled),
                now,
            ),
        )
