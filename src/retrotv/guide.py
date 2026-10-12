from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from retrotv.db import utc_now
from retrotv.graphics import AUDIO_EXTENSIONS, GraphicsService
from retrotv.normalization import NormalizationService
from retrotv.scheduling import resolve_timezone
from retrotv.synthetic import (
    weather_channel_available,
    SyntheticChannelError,
    weather_channel_definition,
    weather_channel_reader,
)


@dataclass(frozen=True)
class GuideListing:
    entry_id: int
    asset_id: int | None
    title: str
    starts_at: str
    ends_at: str
    entry_type: str
    guide_label: str | None
    series_title: str | None
    file_title: str | None
    programming_block_id: int | None = None


@dataclass(frozen=True)
class GuideChannel:
    slug: str
    display_name: str
    channel_number: int | None
    now: GuideListing | None
    next: GuideListing | None
    columns: tuple[GuideListing | None, ...]
    playback_mode: str = "scheduled"
    progress_seconds: float | None = None
    preparation_state: str | None = None


class GuideService:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config
        settings = config.data.get("guide", {})
        self.settings = settings
        timezone_name = str(
            settings.get(
                "timezone",
                config.data.get("scheduling", {}).get(
                    "timezone",
                    config.data.get("application", {}).get("timezone", "UTC"),
                ),
            )
        )
        self.timezone = resolve_timezone(timezone_name)
        self.offset_minutes = tuple(
            int(value) for value in settings.get("columns_minutes", [0, 30, 60])
        )
        self.slot_minutes = max(1, int(settings.get("slot_minutes", 30)))
        self.channels_per_page = max(1, int(settings.get("channels_per_page", 4)))
        self.weather_definition = weather_channel_definition(config)
        self.weather_reader = weather_channel_reader(config)

    def _daypart_at(self, moment: datetime) -> str:
        raw_dayparts = self.config.data.get("scheduling", {}).get("dayparts") or [
            {"slug": "morning", "start": "06:00", "end": "12:00"},
            {"slug": "afternoon", "start": "12:00", "end": "17:00"},
            {"slug": "evening", "start": "17:00", "end": "22:00"},
            {"slug": "night", "start": "22:00", "end": "06:00"},
        ]
        current = moment.timetz().replace(tzinfo=None)
        for item in raw_dayparts:
            start = time.fromisoformat(str(item["start"]))
            end = time.fromisoformat(str(item["end"]))
            active = (
                start <= current < end
                if start < end
                else current >= start or current < end
            )
            if active:
                return str(item["slug"])
        return "all-day"

    def _guide_greeting(self, moment: datetime, daypart: str) -> str:
        defaults = {
            "morning": "Good Morning!",
            "afternoon": "Good Afternoon!",
            "evening": "Good Evening!",
            "night": "Good Night!",
            "all-day": "Welcome!",
        }
        greetings = self.settings.get("greetings", {})
        message = str(greetings.get(daypart, defaults.get(daypart, "Welcome!")))
        holiday_messages = self.settings.get("holiday_greetings", {})
        holidays = self.config.data.get("seasonal", {}).get("holidays", {})
        month_day = moment.strftime("%m-%d")
        for holiday_slug, holiday in holidays.items():
            if not bool(holiday.get("enabled", True)):
                continue
            start = str(holiday.get("start", ""))
            end = str(holiday.get("end", ""))
            active = (
                start <= month_day <= end
                if start and end and start <= end
                else bool(start and end) and (month_day >= start or month_day <= end)
            )
            variations = holiday_messages.get(holiday_slug, {})
            if active and daypart in variations:
                return str(variations[daypart])
        return message

    def _pending_action_summary(self) -> dict[str, int | bool]:
        library_review = int(
            self.connection.execute(
                "SELECT COUNT(*) count FROM assets WHERE review_status = 'open'"
            ).fetchone()["count"]
        )
        shadow_review = int(
            self.connection.execute(
                "SELECT COUNT(*) count FROM shadow_files WHERE status='review'"
            ).fetchone()["count"]
        )
        aspect_review = int(
            self.connection.execute(
                """
                SELECT COUNT(*) count
                FROM normalization_profiles n
                JOIN assets a ON a.id = n.asset_id
                WHERE n.analysis_status = 'review'
                  AND a.integrity_status IN ('ready', 'not_scanned')
                """
            ).fetchone()["count"]
        )
        rows = self.connection.execute(
            """
            SELECT a.id, a.source_relative_path, m.width, m.height, n.*
            FROM assets a
            JOIN normalization_profiles n ON n.asset_id = a.id
            JOIN asset_media_info m ON m.asset_id = a.id
            WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND a.review_status = 'ready'
              AND a.metadata_status = 'ready'
              AND a.integrity_status = 'ready'
              AND n.analysis_status = 'analyzed'
            """
        ).fetchall()
        normalizer = NormalizationService(self.connection, self.config)
        replace_sources = (
            str(
                self.config.data.get("normalization", {}).get(
                    "output_policy", "proxy"
                )
            )
            == "replace_with_backup"
        )
        conversion_ready = 0
        for row in rows:
            try:
                needs_conversion = normalizer.requires_conversion(row)
            except (TypeError, ValueError):
                continue
            already_done = (
                row["proxy_status"] == "promoted"
                if replace_sources
                else row["proxy_status"] in {"ready", "promoted"}
            )
            if needs_conversion and not already_done:
                conversion_ready += 1
        audio_analysis_pending = int(
            self.connection.execute(
                """
                SELECT COUNT(*) count
                FROM assets a
                JOIN asset_media_info m ON m.asset_id=a.id
                LEFT JOIN audio_normalization_profiles p ON p.asset_id=a.id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND a.review_status='ready'
                  AND a.metadata_status='ready'
                  AND a.integrity_status IN ('ready', 'not_scanned')
                  AND a.enabled_for_scheduling=1
                  AND (
                    p.asset_id IS NULL OR p.analysis_status IN ('pending', 'analyzing', 'error') OR
                    p.source_signature !=
                      (CAST(a.file_size AS TEXT) || ':' || CAST(a.modified_ns AS TEXT))
                  )
                """
            ).fetchone()["count"]
        )
        audio_review = int(
            self.connection.execute(
                """
                SELECT COUNT(*) count
                FROM audio_normalization_profiles p
                JOIN assets a ON a.id = p.asset_id
                WHERE p.analysis_status='review'
                  AND a.integrity_status IN ('ready', 'not_scanned')
                """
            ).fetchone()["count"]
        )
        audio_conversion_ready = int(
            self.connection.execute(
                """
                SELECT COUNT(*) count
                FROM audio_normalization_profiles p JOIN assets a ON a.id=p.asset_id
                WHERE p.analysis_status='analyzed'
                  AND p.needs_correction=1
                  AND p.approval_status='approved'
                  AND p.correction_status IN ('pending', 'normalizing', 'error', 'stale')
                  AND p.source_signature =
                      (CAST(a.file_size AS TEXT) || ':' || CAST(a.modified_ns AS TEXT))
                """
            ).fetchone()["count"]
        )
        duplicate_review = int(
            self.connection.execute(
                """
                WITH groups AS (
                    SELECT h.original_sha256, COUNT(*) AS member_count
                    FROM asset_content_hashes h JOIN assets a ON a.id=h.asset_id
                    WHERE a.integrity_status IN ('ready','not_scanned')
                    GROUP BY h.original_sha256 HAVING COUNT(*) > 1
                )
                SELECT COUNT(*) AS count FROM groups g
                LEFT JOIN duplicate_reviews r
                  ON r.original_sha256=g.original_sha256
                WHERE r.status IS NULL OR r.status<>'intentional'
                   OR r.reviewed_member_count<>g.member_count
                """
            ).fetchone()["count"]
        )
        from retrotv.processing import enabled
        if not enabled(self.config, "normalization"):
            aspect_review = conversion_ready = 0
        if not enabled(self.config, "audio_normalization"):
            audio_analysis_pending = audio_review = audio_conversion_ready = 0
        total = (
            library_review
            + shadow_review
            + aspect_review
            + conversion_ready
            + audio_review
            + audio_conversion_ready
            + duplicate_review
        )
        return {
            "library_review": library_review,
            "shadow_review": shadow_review,
            "aspect_review": aspect_review,
            "conversion_ready": conversion_ready,
            "duplicate_review": duplicate_review,
            "audio_analysis_pending": audio_analysis_pending,
            "audio_review": audio_review,
            "audio_conversion_ready": audio_conversion_ready,
            "total": total,
            "show_indicator": total > 0,
        }

    def snapshot(self, when: datetime | None = None) -> dict[str, Any]:
        weather_available = weather_channel_available(self.config, self.weather_reader)
        moment = when or datetime.now(self.timezone)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.timezone)
        else:
            moment = moment.astimezone(self.timezone)
        channel_rows = self.connection.execute(
            """
            SELECT c.*, sg.id AS generation_id
            FROM schedule_generations sg
            JOIN channels c ON c.id = sg.channel_id
            LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id
            WHERE sg.schedule_date = ? AND sg.status = 'active' AND c.enabled = 1
              AND COALESCE(p.playback_mode,'scheduled')='scheduled'
            ORDER BY c.channel_number IS NULL, c.channel_number, c.display_name
            """,
            (moment.date().isoformat(),),
        ).fetchall()
        slot_start = self._slot_start(moment)
        channels = []
        column_moments = tuple(
            slot_start + timedelta(minutes=offset)
            for offset in self.offset_minutes
        )
        configured_numbers = self.settings.get("channel_numbers", {})
        graphics = GraphicsService(self.connection, self.config)
        for channel in channel_rows:
            if (
                self.weather_definition.enabled
                and str(channel["slug"]) == self.weather_definition.slug
            ):
                continue
            programs = self._program_listings(int(channel["generation_id"]))
            columns = tuple(
                self._listing_at(programs, moment)
                if index == 0
                else self._listing_for_slot(
                    programs,
                    column_moment,
                    column_moment + timedelta(minutes=self.slot_minutes),
                )
                for index, column_moment in enumerate(column_moments)
            )
            now = columns[0] if columns else self._listing_at(programs, moment)
            following = self._next_listing(programs, now, moment)
            channels.append(
                GuideChannel(
                    channel["slug"],
                    graphics.identity_name(
                        str(channel["slug"]),
                        str(channel["display_name"]),
                        graphics.daypart_slug_at(moment),
                    ),
                    configured_numbers.get(
                        channel["slug"], channel["channel_number"]
                    ),
                    now,
                    following,
                    columns,
                )
            )
        pace_rows = self.connection.execute(
            """SELECT c.* FROM channels c
               JOIN channel_playback_preferences p ON p.channel_id=c.id
               WHERE c.enabled=1 AND p.playback_mode='watch_at_your_pace'
               ORDER BY c.channel_number IS NULL,c.channel_number,c.display_name"""
        ).fetchall()
        if pace_rows:
            from retrotv.internet_media import WatchProgressService

            progress = WatchProgressService(self.connection, self.config)
            for channel in pace_rows:
                status = progress.status(str(channel["slug"]))
                current = status["current"]
                upcoming = status["up_next"]

                def listing(item, entry_type):
                    if not item:
                        return None
                    return GuideListing(
                        int(item["id"]), None, str(item["display_title"]), "", "",
                        entry_type, "Watch at Your Pace", str(item["show_title"]),
                        str(item["remote_filename"]),
                    )

                now_listing = listing(current, "watch_episode")
                if current is None and status["state"] == "caught_up":
                    now_listing = GuideListing(
                        -1, None, "You're caught up", "", "", "watch_caught_up",
                        "Watch at Your Pace", None, None,
                    )
                channels.append(
                    GuideChannel(
                        str(channel["slug"]),
                        graphics.identity_name(
                            str(channel["slug"]), str(channel["display_name"]), "all-day"
                        ),
                        configured_numbers.get(channel["slug"], channel["channel_number"]),
                        now_listing,
                        listing(upcoming, "watch_next"),
                        tuple(now_listing for _ in column_moments),
                        "watch_at_your_pace",
                        float(status["position_seconds"]),
                        str(current["preparation_state"]) if current else None,
                    )
                )
        if weather_available:
            channels.append(
                self._synthetic_guide_channel(moment, column_moments)
            )
        channels.sort(
            key=lambda channel: (
                channel.channel_number is None,
                channel.channel_number if channel.channel_number is not None else 9999,
                channel.display_name.casefold(),
            )
        )
        daypart = self._daypart_at(moment)
        weather_manifest = self.weather_reader.last_good
        payload = {
            "generated_at": datetime.now(self.timezone).isoformat(timespec="seconds"),
            "as_of": slot_start.isoformat(timespec="seconds"),
            "window_ends_at": (
                max(column_moments, default=moment) + timedelta(minutes=30)
            ).isoformat(timespec="seconds"),
            "guide_channel_number": int(self.settings.get("channel_number", 2)),
            "daypart": daypart,
            "greeting": self._guide_greeting(moment, daypart),
            "pending_actions": self._pending_action_summary(),
            "column_offsets_minutes": list(self.offset_minutes),
            "column_times": [
                column_moment.isoformat(timespec="seconds")
                for column_moment in column_moments
            ],
            "channels": [asdict(item) for item in channels],
            "synthetic_generations": (
                {"weather": weather_manifest.generation_id}
                if weather_available and weather_manifest is not None
                else {}
            ),
        }
        fingerprint_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"generated_at", "pending_actions"}
        }
        fingerprint_payload["text_size_percent"] = int(
            self.settings.get("text_size_percent", 100)
        )
        fingerprint_payload["channels"] = [
            {key: value for key, value in item.items() if key != "progress_seconds"}
            for item in fingerprint_payload["channels"]
        ]
        if self.weather_definition.enabled:
            # Segment titles and exact cue boundaries move every few seconds,
            # but the prerecorded Guide should retain its half-hour cadence.
            # The publication generation still invalidates the render as soon
            # as RetroWeather atomically publishes a new loop.
            fingerprint_payload["channels"] = [
                item
                if item["slug"] != self.weather_definition.slug
                else {
                    "slug": item["slug"],
                    "display_name": item["display_name"],
                    "channel_number": item["channel_number"],
                    "generation_id": (
                        weather_manifest.generation_id
                        if weather_manifest is not None else None
                    ),
                }
                for item in payload["channels"]
            ]
        payload["fingerprint"] = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return payload

    def _synthetic_guide_channel(
        self,
        moment: datetime,
        column_moments: tuple[datetime, ...],
    ) -> GuideChannel:
        try:
            columns = tuple(
                self._synthetic_listing_at(
                    moment if index == 0 else column_moment
                )
                for index, column_moment in enumerate(column_moments)
            )
            now = columns[0] if columns else self._synthetic_listing_at(moment)
            boundary = datetime.fromisoformat(now.ends_at)
            following = self._synthetic_listing_at(
                boundary + timedelta(microseconds=1)
            )
        except (OSError, ValueError, SyntheticChannelError):
            columns = tuple(None for _ in column_moments)
            now = None
            following = None
        return GuideChannel(
            self.weather_definition.slug,
            self.weather_definition.display_name,
            self.weather_definition.channel_number,
            now,
            following,
            columns,
        )

    def _synthetic_listing_at(self, moment: datetime) -> GuideListing:
        position = self.weather_reader.playback_position(now=moment)
        segments = position.manifest.payload["segments"]
        index = next(
            (
                item_index
                for item_index, item in enumerate(segments)
                if item["id"] == position.segment_id
            ),
            0,
        )
        return GuideListing(
            -1000 - index,
            None,
            position.segment_title,
            position.starts_at.astimezone(self.timezone).isoformat(),
            position.ends_at.astimezone(self.timezone).isoformat(),
            "synthetic_channel",
            None,
            None,
            None,
            None,
        )

    def _slot_start(self, moment: datetime) -> datetime:
        hour_start = moment.replace(minute=0, second=0, microsecond=0)
        minutes_since_hour = moment.minute
        slot_index = minutes_since_hour // self.slot_minutes
        return hour_start + timedelta(minutes=slot_index * self.slot_minutes)

    def _program_listings(self, generation_id: int) -> list[GuideListing]:
        rows = self.connection.execute(
            """
            SELECT se.*,
                   a.series_title AS asset_series_title,
                   a.original_filename AS asset_original_filename,
                   COALESCE(
                       (
                           SELECT MIN(group_entry.starts_at)
                           FROM schedule_entries group_entry
                           WHERE group_entry.generation_id = se.generation_id
                             AND group_entry.sequence_group = se.sequence_group
                             AND group_entry.programming_owner = se.programming_owner
                             AND group_entry.entry_type != 'announcement_opportunity'
                       ),
                       se.starts_at
                   ) AS listing_starts_at
            FROM schedule_entries se
            LEFT JOIN assets a ON a.id = se.asset_id
            WHERE se.generation_id = ? AND se.is_program = 1
            ORDER BY se.starts_at
            """,
            (generation_id,),
        ).fetchall()
        listings: list[GuideListing] = []
        collapsed: dict[tuple, int] = {}
        for row in rows:
            scheduled_title = row["guide_listing_title"] or row["title"]
            series_title = row["asset_series_title"]
            original_filename = row["asset_original_filename"]
            file_title = Path(original_filename).stem if original_filename else None
            title = (
                f"{series_title} — {file_title or scheduled_title}"
                if series_title
                else scheduled_title
            )
            block_id = row["programming_block_id"]
            key = (
                "slice", int(block_id), int(row["asset_id"])
            ) if block_id is not None and row["asset_id"] is not None else (
                "entry", int(row["id"])
            )
            if key in collapsed:
                position = collapsed[key]
                previous = listings[position]
                listings[position] = GuideListing(
                    previous.entry_id, previous.asset_id, previous.title,
                    min(previous.starts_at, row["listing_starts_at"]),
                    max(previous.ends_at, row["ends_at"]),
                    previous.entry_type, previous.guide_label,
                    previous.series_title, previous.file_title,
                    previous.programming_block_id,
                )
                continue
            collapsed[key] = len(listings)
            listings.append(
                GuideListing(
                    int(row["id"]), row["asset_id"], title,
                    row["listing_starts_at"], row["ends_at"], row["entry_type"],
                    row["guide_label"], series_title, file_title,
                    int(block_id) if block_id is not None else None,
                )
            )
        return self._extend_listings_through_owned_continuity(generation_id, listings)

    def _extend_listings_through_owned_continuity(
        self, generation_id: int, listings: list[GuideListing]
    ) -> list[GuideListing]:
        """Keep the final program title visible through its owned block tail.

        A program block often ends with commercials, a graphic, and an Up Next
        opportunity. Those rows are intentionally not program listings, but
        they should not leave a Guide cell blank before the next program starts.
        The extension is limited to a contiguous handoff between two numbered
        programming blocks; unowned continuity still remains a real Guide gap.
        """
        numbered = [
            (index, listing)
            for index, listing in enumerate(listings)
            if listing.programming_block_id is not None
        ]
        if not numbered:
            return listings
        last_for_block: dict[int, int] = {}
        for index, listing in numbered:
            last_for_block[int(listing.programming_block_id)] = index
        rows = self.connection.execute(
            """
            SELECT starts_at,ends_at,programming_block_id
            FROM schedule_entries
            WHERE generation_id=?
            ORDER BY starts_at,sequence_position,id
            """,
            (generation_id,),
        ).fetchall()
        extended = list(listings)
        for position, (index, listing) in enumerate(numbered):
            block_id = int(listing.programming_block_id)
            if last_for_block[block_id] != index:
                continue
            next_listing = next(
                (
                    later
                    for _later_index, later in numbered[position + 1 :]
                    if later.programming_block_id != listing.programming_block_id
                ),
                None,
            )
            if next_listing is None:
                continue
            start = datetime.fromisoformat(listing.ends_at)
            end = datetime.fromisoformat(next_listing.starts_at)
            if end <= start:
                continue
            allowed_blocks = {block_id, int(next_listing.programming_block_id)}
            tail = [
                row
                for row in rows
                if datetime.fromisoformat(row["ends_at"]) > start
                and datetime.fromisoformat(row["starts_at"]) < end
            ]
            if not tail or any(
                row["programming_block_id"] is None
                or int(row["programming_block_id"]) not in allowed_blocks
                for row in tail
            ):
                continue
            extended[index] = GuideListing(
                listing.entry_id,
                listing.asset_id,
                listing.title,
                listing.starts_at,
                next_listing.starts_at,
                listing.entry_type,
                listing.guide_label,
                listing.series_title,
                listing.file_title,
                listing.programming_block_id,
            )
        return extended

    def _listing_at(
        self, listings: list[GuideListing], moment: datetime
    ) -> GuideListing | None:
        for listing in listings:
            starts = datetime.fromisoformat(listing.starts_at)
            ends = datetime.fromisoformat(listing.ends_at)
            if starts <= moment < ends:
                return self._cue_adjusted(listing, moment)
        return None

    def _listing_for_slot(
        self,
        listings: list[GuideListing],
        slot_start: datetime,
        slot_end: datetime,
    ) -> GuideListing | None:
        candidates: list[tuple[float, datetime, GuideListing]] = []
        for listing in listings:
            starts = datetime.fromisoformat(listing.starts_at)
            ends = datetime.fromisoformat(listing.ends_at)
            overlap = (
                min(ends, slot_end) - max(starts, slot_start)
            ).total_seconds()
            if overlap > 0:
                candidates.append((overlap, starts, listing))
        if not candidates:
            return None
        grouped: dict[str, list[tuple[float, datetime, GuideListing]]] = {}
        for candidate in candidates:
            listing = candidate[2]
            key = (
                f"series:{listing.series_title}"
                if listing.series_title
                else f"entry:{listing.entry_id}"
            )
            grouped.setdefault(key, []).append(candidate)
        selected_group = max(
            grouped.values(),
            key=lambda group: (
                sum(candidate[0] for candidate in group),
                max(candidate[1] for candidate in group),
            ),
        )
        if (
            len(selected_group) > 1
            and selected_group[0][2].series_title
        ):
            ordered = sorted(selected_group, key=lambda candidate: candidate[1])
            first = ordered[0][2]
            count = len(ordered)
            label = f"{count} episodes"
            return GuideListing(
                first.entry_id,
                first.asset_id,
                f"{first.series_title} — {label}",
                min(item[2].starts_at for item in ordered),
                max(item[2].ends_at for item in ordered),
                "episode_package",
                first.guide_label,
                first.series_title,
                label,
                first.programming_block_id,
            )
        # Listings are fixed for the whole half-hour Guide loop. Select the
        # program occupying the largest portion of the slot instead of a show
        # that may only bleed a few seconds past the boundary.
        _overlap, starts, listing = max(
            selected_group,
            key=lambda candidate: (candidate[0], candidate[1]),
        )
        representative = max(slot_start, starts)
        return self._cue_adjusted(listing, representative)

    @staticmethod
    def _next_listing(
        listings: list[GuideListing],
        current: GuideListing | None,
        moment: datetime,
    ) -> GuideListing | None:
        boundary = (
            datetime.fromisoformat(current.ends_at) if current else moment
        )
        return next(
            (
                listing
                for listing in listings
                if datetime.fromisoformat(listing.starts_at) >= boundary
                and (current is None or listing.entry_id != current.entry_id)
            ),
            None,
        )

    def _cue_adjusted(
        self, listing: GuideListing, moment: datetime
    ) -> GuideListing:
        if listing.entry_type != "complete_block" or listing.asset_id is None:
            return listing
        elapsed_ms = round(
            (moment - datetime.fromisoformat(listing.starts_at)).total_seconds() * 1000
        )
        cue = self.connection.execute(
            """
            SELECT *
            FROM block_cues
            WHERE asset_id = ?
              AND offset_ms <= ?
              AND (end_offset_ms IS NULL OR end_offset_ms > ?)
            ORDER BY offset_ms DESC
            LIMIT 1
            """,
            (listing.asset_id, elapsed_ms, elapsed_ms),
        ).fetchone()
        if cue is None or cue["cue_type"] != "program":
            return listing
        return GuideListing(
            listing.entry_id,
            listing.asset_id,
            cue["title"],
            listing.starts_at,
            listing.ends_at,
            listing.entry_type,
            listing.guide_label,
            listing.series_title,
            listing.file_title,
            listing.programming_block_id,
        )


class GuideRenderer:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config
        self.service = GuideService(connection, config)
        self.settings = config.data.get("guide", {})

    def _text_scale(self) -> float:
        return max(75, min(150, int(self.settings.get("text_size_percent", 100)))) / 100

    def _font_size(self, base: int) -> int:
        return max(1, round(base * self._text_scale()))

    def needs_refresh(self, when: datetime | None = None) -> bool:
        moment = when or datetime.now(self.service.timezone)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.service.timezone)
        else:
            moment = moment.astimezone(self.service.timezone)
        latest = self.connection.execute(
            """
            SELECT window_starts_at, window_ends_at, output_relative_path,
                   source_fingerprint
            FROM guide_renders
            WHERE status = 'valid'
            ORDER BY activated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if latest is None:
            return True
        stored = Path(latest["output_relative_path"])
        target = (
            stored
            if stored.is_absolute()
            else (self.config.project_root / stored).resolve()
        )
        if not target.exists():
            return True
        window_start = datetime.fromisoformat(latest["window_starts_at"])
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=self.service.timezone)
        localized_start = window_start.astimezone(self.service.timezone)
        current_slot = self.service._slot_start(moment)
        rendered_slot = self.service._slot_start(localized_start)
        current_fingerprint = self.service.snapshot(moment)["fingerprint"]
        return (
            moment < localized_start
            or current_slot != rendered_slot
            or latest["source_fingerprint"] != current_fingerprint
        )

    def render(
        self,
        when: datetime | None = None,
        *,
        output: Path | None = None,
        page_seconds: float | None = None,
        versioned: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.service.snapshot(when)
        channels = snapshot["channels"]
        text_scale = self._text_scale()
        row_height = max(
            40,
            int(self.settings.get("scroll_row_height", 48)),
            math.ceil(36 * text_scale),
        )
        scroll_speed = max(
            1.0, float(self.settings.get("scroll_pixels_per_second", 20))
        )
        belt_rows = max(1, len(channels))
        scroll_distance = belt_rows * row_height
        requested_cycle_duration = (
            float(page_seconds)
            if page_seconds is not None
            else scroll_distance / scroll_speed
        )
        frame_count = max(1, round(requested_cycle_duration * 30))
        cycle_duration = frame_count / 30
        if cycle_duration <= 0:
            raise ValueError("Guide loop duration must be positive")
        music_path, music_duration, music_relative = self._select_music()
        maximum_music_seconds = max(
            cycle_duration,
            float(self.settings.get("music_max_minutes", 10)) * 60,
        )
        desired_duration = (
            min(music_duration, maximum_music_seconds)
            if music_path is not None and music_duration is not None
            else cycle_duration
        )
        # The scrolling belt is the authoritative loop. End at the last full
        # visual cycle before the selected track finishes instead of extending
        # the Guide and replaying the beginning of the music.
        cycle_count = self._music_cycle_count(desired_duration, cycle_duration)
        duration = cycle_duration * cycle_count
        effective_speed = scroll_distance / cycle_duration
        target = output or self._output_path()
        if versioned:
            target = target.with_name(
                f"{target.stem}.runtime-{uuid.uuid4().hex[:12]}{target.suffix}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        render_id = self._begin_render(
            snapshot, target, 1, duration, music_relative
        )
        staged_files: list[Path] = []
        try:
            work = target.parent
            prefix = f".guide-{uuid.uuid4().hex}"
            ass = work / f"{prefix}.ass"
            ass.write_text(
                self._ass_scroll(
                    snapshot,
                    channels,
                    duration,
                    cycle_duration,
                    row_height,
                    scroll_distance,
                ),
                encoding="utf-8-sig",
            )
            staged_files.append(ass)
            staged = work / f"{prefix}-final.mp4"
            staged_files.append(staged)
            self._encode_page(
                work, ass.name, staged.name, duration, music_path=music_path
            )
            self._validate(staged, duration)
            os.replace(staged, target)
            if versioned:
                self._cleanup_versioned_outputs(target)
            self.connection.execute(
                """
                UPDATE guide_renders
                SET status = 'valid', detail = ?, activated_at = ?
                WHERE id = ?
                """,
                (
                    "Validated continuous seamless 640x480 scrolling guide "
                    f"at {effective_speed:.3f}px/s"
                    + (
                        f" with music {music_relative}."
                        if music_relative
                        else " with silent audio."
                    ),
                    utc_now(),
                    render_id,
                ),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.execute(
                "UPDATE guide_renders SET status = 'failed', detail = ? WHERE id = ?",
                (str(exc), render_id),
            )
            self.connection.commit()
            raise
        finally:
            for staged_file in staged_files:
                try:
                    staged_file.unlink(missing_ok=True)
                except OSError:
                    pass
        return {
            "render_id": render_id,
            "output": str(target),
            "pages": 1,
            "duration_seconds": duration,
            "cycle_duration_seconds": cycle_duration,
            "channels": len(channels),
            "scroll_pixels_per_second": effective_speed,
            "visual_cycles": cycle_count,
            "music": music_relative,
            "fingerprint": snapshot["fingerprint"],
        }

    @staticmethod
    def _cleanup_versioned_outputs(active: Path, keep: int = 3) -> None:
        pattern = f"{active.stem.split('.runtime-', 1)[0]}.runtime-*{active.suffix}"
        candidates = sorted(
            active.parent.glob(pattern),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for candidate in candidates[keep:]:
            try:
                candidate.unlink()
            except OSError:
                pass

    def _output_path(self) -> Path:
        raw = Path(str(self.settings.get("output", "./data/generated/guide.mp4")))
        return raw if raw.is_absolute() else (self.config.project_root / raw).resolve()

    def _relative_output_path(self, target: Path) -> str:
        try:
            return target.relative_to(self.config.project_root).as_posix()
        except ValueError:
            return str(target)

    def _music_directory(self) -> Path:
        media_root = self.config.storage_root("media").resolve()
        raw = Path(str(self.settings.get("music_directory", "_guide/music")))
        directory = raw.resolve() if raw.is_absolute() else (media_root / raw).resolve()
        directory.relative_to(media_root)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _music_cycle_count(music_duration: float, cycle_duration: float) -> int:
        return max(
            1,
            int(math.floor((music_duration + 0.000001) / cycle_duration)),
        )

    def _select_music(self) -> tuple[Path | None, float | None, str | None]:
        directory = self._music_directory()
        media_root = self.config.storage_root("media").resolve()
        candidates: list[tuple[Path, float, str]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
                continue
            duration = self._audio_duration(path)
            if duration is None or duration <= 0:
                continue
            candidates.append(
                (path, duration, path.resolve().relative_to(media_root).as_posix())
            )
        if not candidates:
            return None, None, None
        latest = self.connection.execute(
            """
            SELECT music_relative_path FROM guide_renders
            WHERE status='valid' AND music_relative_path IS NOT NULL
            ORDER BY activated_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        previous = str(latest["music_relative_path"]) if latest else None
        choices = [item for item in candidates if item[2] != previous]
        if not choices:
            choices = candidates
        return random.SystemRandom().choice(choices)

    def _audio_duration(self, path: Path) -> float | None:
        try:
            completed = hidden_run(
                [
                    self.config.ffprobe,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode:
                return None
            value = float(completed.stdout.strip())
            return value if math.isfinite(value) and value > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _begin_render(
        self,
        snapshot: dict[str, Any],
        target: Path,
        pages: int,
        duration: float,
        music_relative_path: str | None,
    ) -> int:
        relative = self._relative_output_path(target)
        cursor = self.connection.execute(
            """
            INSERT INTO guide_renders(
                window_starts_at, window_ends_at, output_relative_path,
                source_fingerprint, page_count, duration_seconds, status,
                generated_at, music_relative_path
            ) VALUES (?, ?, ?, ?, ?, ?, 'building', ?, ?)
            """,
            (
                snapshot["as_of"],
                snapshot["window_ends_at"],
                relative,
                snapshot["fingerprint"],
                pages,
                pages * duration,
                utc_now(),
                music_relative_path,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def _encode_page(
        self,
        cwd: Path,
        subtitle_name: str,
        output_name: str,
        duration: float,
        *,
        music_path: Path | None = None,
    ) -> None:
        fonts_dir = (
            self.config.project_root
            / str(self.settings.get("fonts_dir", "assets/fonts"))
        ).resolve()
        logo_path = (
            self.config.project_root
            / str(
                self.settings.get(
                    "logo", "assets/branding/retrotv-guide-logo.png"
                )
            )
        ).resolve()
        if not fonts_dir.is_dir():
            raise FileNotFoundError(f"Guide fonts directory is missing: {fonts_dir}")
        if not logo_path.is_file():
            raise FileNotFoundError(f"Guide logo is missing: {logo_path}")
        relative_fonts = os.path.relpath(fonts_dir, cwd).replace("\\", "/")
        if music_path is None:
            audio_input = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_filter = ""
            audio_map = "1:a"
        else:
            audio_input = ["-i", str(music_path)]
            volume = float(self.settings.get("music_volume", 0.20))
            audio_filter = (
                f"[1:a:0]volume={volume:g},apad,atrim=0:{duration:g},"
                "asetpts=N/SR/TB[a];"
            )
            audio_map = "[a]"
        self._run(
            [
                self.config.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x07172f:s=640x480:r=30:d={duration:g}",
                *audio_input,
                "-loop",
                "1",
                "-i",
                str(logo_path),
                "-filter_complex",
                audio_filter
                + f"[0:v]subtitles={subtitle_name}:fontsdir={relative_fonts}[guide];"
                "[guide][2:v]overlay=510:16:format=auto[v]",
                "-map",
                "[v]",
                "-map",
                audio_map,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-g",
                "30",
                "-keyint_min",
                "30",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-t",
                f"{duration:g}",
                "-shortest",
                output_name,
            ],
            cwd=cwd,
        )

    def _validate(self, path: Path, expected_duration: float) -> None:
        completed = hidden_run(
            [
                self.config.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height,codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        payload = json.loads(completed.stdout)
        video = next(
            stream for stream in payload["streams"] if stream["codec_type"] == "video"
        )
        actual = float(payload["format"]["duration"])
        if video.get("width") != 640 or video.get("height") != 480:
            raise RuntimeError("Guide output is not 640x480")
        if abs(actual - expected_duration) > 0.25:
            raise RuntimeError(
                f"Guide duration {actual:.3f}s differs from expected {expected_duration:.3f}s"
            )

    @staticmethod
    def _run(command: list[str], *, cwd: Path) -> None:
        completed = hidden_run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "Guide render failed")

    @staticmethod
    def _ass_escape(value: str) -> str:
        return (
            value.replace("\\", r"\\")
            .replace("{", "(")
            .replace("}", ")")
            .replace("\n", " ")
        )

    @staticmethod
    def _channel_label(
        channel_number: int | None,
        display_name: str,
        *,
        abbreviate: bool = False,
    ) -> str:
        number = str(channel_number) if channel_number is not None else "--"
        name = " ".join(display_name.upper().split())
        if abbreviate and len(name.replace(" ", "")) > 6:
            abbreviated = "".join(char for char in name if char not in "AEIOU")
            if abbreviated.strip():
                name = "".join(abbreviated.split())
        return f"{number}  {name}"

    @staticmethod
    def _same_continuous_listing(
        left: dict[str, Any] | None,
        right: dict[str, Any] | None,
    ) -> bool:
        """Return whether two Guide columns show one continuous program.

        Titles alone are deliberately insufficient: consecutive episodes can
        share a series or even a display title.  A repeated schedule entry with
        the same authored time range is the stable identity of a program that
        crosses a half-hour Guide boundary.
        """
        if left is None or right is None:
            return False
        return all(
            left.get(key) == right.get(key)
            for key in ("entry_id", "starts_at", "ends_at", "title")
        )

    def _ass_scroll(
        self,
        snapshot: dict[str, Any],
        channels: list[dict[str, Any]],
        duration: float,
        cycle_duration: float,
        row_height: int,
        scroll_distance: int,
    ) -> str:
        offsets = snapshot["column_offsets_minutes"]
        column_times = [
            datetime.fromisoformat(value) for value in snapshot["column_times"]
        ]
        header_times = [
            "NOW"
            if index == 0
            else column_times[index].strftime("%I:%M").lstrip("0")
            for index, _offset in enumerate(offsets)
        ]
        end_time = self._ass_time(duration)
        move_end_ms = round(cycle_duration * 1000)
        cycle_count = max(1, round(duration / cycle_duration))
        listing_top = 102
        listing_bottom = 440
        clip = f"\\clip(20,{listing_top},620,{listing_bottom})"
        events: list[str] = []

        def fixed_box(
            x1: int, y1: int, x2: int, y2: int, style: str = "Box"
        ) -> None:
            events.append(
                f"Dialogue: 0,0:00:00.00,{end_time},{style},,0,0,0,,"
                f"{{\\an7\\pos(0,0)\\p1}}m {x1} {y1} l {x2} {y1} "
                f"{x2} {y2} {x1} {y2}"
            )

        def fixed_text(x: int, y: int, style: str, value: str) -> None:
            events.append(
                f"Dialogue: 2,0:00:00.00,{end_time},{style},,0,0,0,,"
                f"{{\\pos({x},{y})}}{self._ass_escape(value)}"
            )

        def moving_box(
            x1: int,
            x2: int,
            y: int,
            start_time: str,
            cycle_end_time: str,
            style: str = "Box",
        ) -> None:
            events.append(
                f"Dialogue: 0,{start_time},{cycle_end_time},{style},,0,0,0,,"
                f"{{\\an7\\move(0,{y},0,{y - scroll_distance},0,{move_end_ms})"
                f"{clip}\\p1}}m {x1} 0 l {x2} 0 {x2} {row_height - 2} "
                f"{x1} {row_height - 2}"
            )

        def moving_text(
            x: int,
            y: int,
            style: str,
            value: str,
            start_time: str,
            cycle_end_time: str,
            *,
            escaped: bool = False,
        ) -> None:
            events.append(
                f"Dialogue: 1,{start_time},{cycle_end_time},{style},,0,0,0,,"
                f"{{\\move({x},{y},{x},{y - scroll_distance},0,{move_end_ms})"
                f"{clip}}}{value if escaped else self._ass_escape(value)}"
            )

        fixed_text(
            28,
            22,
            "Title",
            f"RETRO TV GUIDE  CH {snapshot['guide_channel_number']}",
        )
        fixed_box(20, 58, 158, 96, "ChannelBox")
        fixed_text(28, 70, "ChannelHeader", "CHANNEL")
        for index, label in enumerate(header_times):
            x1 = 164 + index * 152
            fixed_box(x1, 58, x1 + 148, 96, "HeaderBox")
            fixed_text(x1 + 8, 70, "Header", label)
        fixed_box(20, listing_top - 2, 620, listing_bottom + 2, "FrameBox")

        belt = channels or [
            {
                "channel_number": None,
                "display_name": "No channels",
                "columns": [None for _ in offsets],
            }
        ]
        viewport_height = listing_bottom - listing_top
        belt_height = len(belt) * row_height
        belt_copies = (viewport_height + belt_height - 1) // belt_height + 1
        for cycle_index in range(cycle_count):
            start_time = self._ass_time(cycle_index * cycle_duration)
            cycle_end_time = self._ass_time((cycle_index + 1) * cycle_duration)
            for belt_index in range(len(belt) * belt_copies):
                channel = belt[belt_index % len(belt)]
                y = listing_top + belt_index * row_height
                moving_box(
                    20, 158, y, start_time, cycle_end_time, "ChannelBox"
                )
                number = channel["channel_number"]
                name = self._channel_label(
                    number,
                    channel["display_name"],
                    abbreviate=bool(
                        self.settings.get("abbreviate_channel_labels", True)
                    ),
                )
                font_size = max(
                    8, min(self._font_size(16), round(205 / max(1, len(name))))
                )
                moving_text(
                    28,
                    y + 11,
                    "Channel",
                    f"{{\\fs{font_size}}}{self._ass_escape(name)}",
                    start_time,
                    cycle_end_time,
                    escaped=True,
                )
                columns = channel["columns"]
                column_index = 0
                while column_index < len(columns):
                    listing = columns[column_index]
                    span = 1
                    while (
                        column_index + span < len(columns)
                        and self._same_continuous_listing(
                            listing, columns[column_index + span]
                        )
                    ):
                        span += 1
                    x1 = 164 + column_index * 152
                    moving_box(
                        x1,
                        x1 + 148 + (span - 1) * 152,
                        y,
                        start_time,
                        cycle_end_time,
                    )
                    if listing:
                        series = listing.get("series_title")
                        file_title = listing.get("file_title")
                        if series and file_title:
                            series_limit = max(
                                8, round((16 + (span - 1) * 18) / self._text_scale())
                            )
                            file_limit = max(
                                8, round((20 + (span - 1) * 20) / self._text_scale())
                            )
                            title = (
                                f"{{\\fs{self._font_size(13)}\\b1}}{self._ass_escape(series[:series_limit])}\\N"
                                f"{{\\fs{self._font_size(9)}\\b0}}{self._ass_escape(file_title[:file_limit])}"
                            )
                            text_y = y + 4
                        else:
                            title_limit = max(
                                8, round((17 + (span - 1) * 18) / self._text_scale())
                            )
                            title = self._ass_escape(
                                listing["title"][:title_limit]
                            )
                            text_y = y + max(4, 13 - (self._font_size(12) - 12) // 2)
                    else:
                        title = "TBD"
                        text_y = y + max(4, 13 - (self._font_size(12) - 12) // 2)
                    moving_text(
                        x1 + 8,
                        text_y,
                        "Cell",
                        title,
                        start_time,
                        cycle_end_time,
                        escaped=True,
                    )
                    column_index += span
        fixed_text(
            28,
            451,
            "Greeting",
            str(snapshot.get("greeting", "Welcome!")),
        )
        if (
            bool(self.settings.get("show_pending_action_indicator", True))
            and snapshot.get("pending_actions", {}).get("show_indicator")
        ):
            fixed_box(584, 446, 620, 474, "AlertBox")
            fixed_text(590, 450, "Alert", "[!]")
        font_name = "VCR OSD Mono" if (self.config.project_root / "assets/fonts/VCR_OSD_MONO_1.001.ttf").exists() else "DejaVu Sans Mono"
        return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 480
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Title,{font_name},{self._font_size(25)},&H00FFFFFF,&H00FFFFFF,&H002A1A00,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1
Style: Header,{font_name},{self._font_size(17)},&H0000FFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: ChannelHeader,{font_name},{self._font_size(17)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Channel,{font_name},{self._font_size(16)},&H0000FFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Cell,{font_name},{self._font_size(12)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Footer,{font_name},{self._font_size(13)},&H0090B8D8,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Greeting,{font_name},{self._font_size(11)},&H00B8D8F0,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Alert,{font_name},{self._font_size(16)},&H00302005,&H00302005,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Box,Arial,1,&H007A4A23,&H007A4A23,&H007A4A23,&H007A4A23,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: ChannelBox,Arial,1,&H00A0622F,&H00A0622F,&H00A0622F,&H00A0622F,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: HeaderBox,Arial,1,&H00523516,&H00523516,&H00523516,&H00523516,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: FrameBox,Arial,1,&H00784D28,&H00784D28,&H00784D28,&H00784D28,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: AlertBox,Arial,1,&H0000FFFF,&H0000FFFF,&H0000FFFF,&H0000FFFF,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
""" + "\n".join(events) + "\n"

    @staticmethod
    def _ass_time(seconds: float) -> str:
        centiseconds = round(seconds * 100)
        hours, remainder = divmod(centiseconds, 360000)
        minutes, remainder = divmod(remainder, 6000)
        whole_seconds, fraction = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"
from retrotv.processes import hidden_run
