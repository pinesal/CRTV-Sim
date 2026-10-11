from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from retrotv.db import utc_now


CUE_TYPES = {
    "program",
    "commercial_break",
    "bumper",
    "promo",
    "station_id",
    "other",
}
BOUNDARY_POLICIES = {"hard", "soft", "must_fit", "fill_short", "may_overrun"}
_UNSET = object()


@dataclass(frozen=True)
class BlockCue:
    offset_ms: int
    end_offset_ms: int | None
    cue_type: str
    title: str
    description: str | None = None


class BlockService:
    def __init__(self, connection):
        self.connection = connection

    def sync_profiles(self) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO block_profiles(asset_id, general_title, updated_at)
            SELECT a.id,
                   COALESCE(a.display_title, a.original_filename),
                   ?
            FROM assets a
            WHERE a.media_type = 'complete_block'
            ON CONFLICT(asset_id) DO NOTHING
            """,
            (now,),
        )
        self.connection.commit()

    def blocks(self):
        self.sync_profiles()
        return self.connection.execute(
            """
            SELECT a.id AS asset_id, a.channel_slug, a.daypart_slug,
                   a.collection_type, a.collection_slug, a.source_relative_path,
                   a.review_status, a.enabled_for_scheduling,
                   m.duration_seconds, bp.general_title, bp.boundary_policy,
                   bp.probability_override, bp.enabled,
                   COUNT(bc.id) AS cue_count
            FROM block_profiles bp
            JOIN assets a ON a.id = bp.asset_id
            LEFT JOIN asset_media_info m ON m.asset_id = a.id
            LEFT JOIN block_cues bc ON bc.asset_id = a.id
            GROUP BY a.id
            ORDER BY a.channel_slug, a.collection_slug, bp.general_title
            """
        ).fetchall()

    def block(self, asset_id: int):
        self.sync_profiles()
        row = self.connection.execute(
            """
            SELECT a.id AS asset_id, a.channel_slug, a.daypart_slug,
                   a.collection_type, a.collection_slug, a.source_relative_path,
                   m.duration_seconds, bp.*
            FROM assets a
            JOIN asset_media_info m ON m.asset_id = a.id
            JOIN block_profiles bp ON bp.asset_id = a.id
            WHERE a.id = ? AND a.media_type = 'complete_block'
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Complete block asset {asset_id} does not exist")
        return row

    def cues(self, asset_id: int):
        self.block(asset_id)
        return self.connection.execute(
            """
            SELECT * FROM block_cues
            WHERE asset_id = ?
            ORDER BY sequence_position, offset_ms
            """,
            (asset_id,),
        ).fetchall()

    def update_profile(
        self,
        asset_id: int,
        *,
        general_title: str | None = None,
        boundary_policy: str | None = None,
        probability_override: float | None | object = _UNSET,
        enabled: bool | None = None,
    ) -> None:
        current = self.block(asset_id)
        policy = boundary_policy or current["boundary_policy"]
        if policy not in BOUNDARY_POLICIES:
            raise ValueError(f"Invalid block boundary policy: {policy}")
        probability = (
            current["probability_override"]
            if probability_override is _UNSET
            else probability_override
        )
        if probability is not None and not 0 <= float(probability) <= 1:
            raise ValueError("Block probability must be between 0 and 1")
        title = (general_title or current["general_title"]).strip()
        if not title:
            raise ValueError("Block general title cannot be empty")
        self.connection.execute(
            """
            UPDATE block_profiles
            SET general_title = ?, boundary_policy = ?,
                probability_override = ?, enabled = ?, updated_at = ?
            WHERE asset_id = ?
            """,
            (
                title,
                policy,
                probability,
                int(enabled if enabled is not None else current["enabled"]),
                utc_now(),
                asset_id,
            ),
        )
        self.connection.commit()

    def replace_cues(
        self, asset_id: int, raw_cues: Iterable[dict[str, Any] | BlockCue]
    ) -> list[BlockCue]:
        block = self.block(asset_id)
        duration_ms = round(float(block["duration_seconds"]) * 1000)
        cues = []
        for raw in raw_cues:
            cue = raw if isinstance(raw, BlockCue) else self._parse_cue(raw)
            cues.append(cue)
        cues.sort(key=lambda item: item.offset_ms)
        self._validate_cues(cues, duration_ms)
        now = utc_now()
        try:
            self.connection.execute("BEGIN")
            self.connection.execute(
                "DELETE FROM block_cues WHERE asset_id = ?", (asset_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO block_cues(
                    asset_id, offset_ms, end_offset_ms, cue_type, title,
                    description, sequence_position, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset_id,
                        cue.offset_ms,
                        cue.end_offset_ms,
                        cue.cue_type,
                        cue.title,
                        cue.description,
                        position,
                        now,
                    )
                    for position, cue in enumerate(cues)
                ],
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return cues

    @staticmethod
    def _parse_cue(raw: dict[str, Any]) -> BlockCue:
        try:
            offset_raw = raw.get("offset_ms", raw.get("offset"))
            offset_ms = BlockService._parse_offset(offset_raw)
            end_raw = raw.get("end_offset_ms", raw.get("end"))
            end_offset_ms = (
                BlockService._parse_offset(end_raw)
                if end_raw not in (None, "")
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Cue offsets must be milliseconds or HH:MM:SS"
            ) from exc
        cue_type = str(raw.get("cue_type", "program"))
        title = str(raw.get("title", "")).strip()
        description = str(raw.get("description", "")).strip() or None
        return BlockCue(offset_ms, end_offset_ms, cue_type, title, description)

    @staticmethod
    def _parse_offset(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            raise ValueError("Invalid cue time")
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) == 3 else 0
        if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
            raise ValueError("Invalid cue time")
        return round((hours * 3600 + minutes * 60 + seconds) * 1000)

    @staticmethod
    def _validate_cues(cues: list[BlockCue], duration_ms: int) -> None:
        previous_end = 0
        for position, cue in enumerate(cues):
            if cue.cue_type not in CUE_TYPES:
                raise ValueError(
                    f"Cue {position + 1} has invalid type {cue.cue_type!r}"
                )
            if not cue.title:
                raise ValueError(f"Cue {position + 1} requires a title")
            if cue.offset_ms < 0 or cue.offset_ms >= duration_ms:
                raise ValueError(
                    f"Cue {position + 1} begins outside the block duration"
                )
            end = cue.end_offset_ms
            if end is not None and end > duration_ms:
                raise ValueError(
                    f"Cue {position + 1} ends outside the block duration"
                )
            if cue.offset_ms < previous_end:
                raise ValueError(f"Cue {position + 1} overlaps the previous cue")
            previous_end = end if end is not None else cue.offset_ms
