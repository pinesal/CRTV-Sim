from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from retrotv.classification import (
    CHANNEL_DAYPARTS,
    MEDIA_TYPES,
    Classification,
    classify_path,
    resolve_channel_root,
    slugify,
)
from retrotv.config import AppConfig
from retrotv.db import utc_now


SHADOW_SUFFIX = ".shadow"
PROGRAM_MEDIA_TYPES = {"episode", "short", "movie", "complete_block"}
MEDIA_FOLDERS = {value: key for key, value in MEDIA_TYPES.items()}


@dataclass(frozen=True)
class ShadowRefreshResult:
    discovered: int
    ready: int
    review: int
    removed: int


class ShadowService:
    def __init__(self, connection: sqlite3.Connection, config: AppConfig) -> None:
        self.connection = connection
        self.config = config
        self.media_root = config.storage_root("media").resolve()

    def _known_channels(self) -> set[str]:
        return {
            str(row["slug"])
            for row in self.connection.execute("SELECT slug FROM channels")
        }

    def _known_holidays(self) -> set[str]:
        configured = self.config.data.get("library", {}).get("holidays", [])
        result = {slugify(str(value)) for value in configured}
        raw = self.config.data.get("seasonal", {}).get("holidays", {})
        if isinstance(raw, dict):
            result.update(slugify(str(value)) for value in raw)
        else:
            result.update(
                slugify(str(item.get("slug", "")))
                for item in raw if isinstance(item, dict)
            )
        return {value for value in result if value}

    def discover(self) -> Iterable[Path]:
        for path in sorted(self.media_root.rglob(f"*{SHADOW_SUFFIX}")):
            relative = path.relative_to(self.media_root)
            if not path.is_file() or not relative.parts:
                continue
            if relative.parts[0].casefold() in {"_channel_template", "_guide"}:
                continue
            if any(part.casefold() == "graphics" for part in relative.parts):
                continue
            yield path

    @staticmethod
    def _target_from_payload(path: Path) -> tuple[str, str | None]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid shadow file: {exc}") from exc
        if not isinstance(payload, dict) or int(payload.get("version", 0)) != 1:
            raise ValueError("Shadow file must contain a version 1 object")
        target = str(payload.get("target", "")).strip().replace("\\", "/")
        parsed = PurePosixPath(target)
        if not target or parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError("Shadow target must be a media-root-relative path")
        raw_digest = payload.get("sha256")
        digest = (
            str(raw_digest).strip().casefold()
            if raw_digest is not None else None
        ) or None
        if digest is not None and (
            len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("Shadow SHA-256 is invalid")
        return parsed.as_posix(), digest

    def _resolve_target(
        self, relative: str, digest: str | None, shadow_relative: str
    ):
        row = self.connection.execute(
            """
            SELECT a.* FROM assets a
            WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE logical_name='media' AND source_kind='managed') AND a.source_relative_path=?
              AND a.integrity_status IN ('ready','not_scanned')
            """,
            (relative,),
        ).fetchone()
        if row is not None:
            return row
        previous = self.connection.execute(
            """
            SELECT a.* FROM shadow_files s JOIN assets a ON a.id=s.target_asset_id
            WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE logical_name='media' AND source_kind='managed') AND s.shadow_relative_path=?
              AND a.integrity_status IN ('ready','not_scanned')
            """,
            (shadow_relative,),
        ).fetchone()
        if previous is not None:
            return previous
        if digest:
            rows = self.connection.execute(
                """
                SELECT a.* FROM assets a
                JOIN asset_content_hashes h ON h.asset_id=a.id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE logical_name='media' AND source_kind='managed') AND (h.working_sha256=? OR h.original_sha256=?)
                  AND a.integrity_status IN ('ready','not_scanned')
                ORDER BY h.working_sha256=? DESC, a.id
                """,
                (digest, digest, digest),
            ).fetchall()
            if len(rows) == 1:
                return rows[0]
        return None

    def _classification(self, shadow_relative: str) -> Classification:
        media_relative = shadow_relative[:-len(SHADOW_SUFFIX)]
        configured_dayparts = {
            slugify(str(item["slug"]))
            for item in self.config.data.get("scheduling", {}).get("dayparts", [])
            if isinstance(item, dict) and item.get("slug")
        }
        return classify_path(
            media_relative,
            known_channels=self._known_channels(),
            known_holidays=self._known_holidays(),
            known_dayparts=(configured_dayparts | {"all-day", "daytime"}
                            if configured_dayparts else CHANNEL_DAYPARTS),
        )

    def refresh(self) -> ShadowRefreshResult:
        seen: set[str] = set()
        ready = review = 0
        now = utc_now()
        for path in self.discover():
            relative = path.relative_to(self.media_root).as_posix()
            seen.add(relative)
            stat = path.stat()
            detail: str | None = None
            target = None
            digest: str | None = None
            target_relative = ""
            classification = self._classification(relative)
            try:
                target_relative, digest = self._target_from_payload(path)
                target = self._resolve_target(target_relative, digest, relative)
                if target is None:
                    raise ValueError("Target media was not found")
                if classification.review_reasons:
                    raise ValueError("; ".join(item[1] for item in classification.review_reasons))
                if classification.media_type != target["media_type"]:
                    raise ValueError(
                        f"Shadow folder classifies this as {classification.media_type}; "
                        f"the target is {target['media_type']}"
                    )
                status = "ready"
                ready += 1
            except (ValueError, OSError) as exc:
                status = "review"
                detail = str(exc)
                review += 1

            self.connection.execute(
                """
                INSERT INTO shadow_files(
                    shadow_relative_path,target_asset_id,target_relative_path,
                    target_sha256,media_type,channel_slug,daypart_slug,
                    collection_type,collection_slug,holiday_slug,series_title,
                    display_title,file_size,modified_ns,status,detail,
                    first_seen_at,last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(shadow_relative_path) DO UPDATE SET
                    target_asset_id=excluded.target_asset_id,
                    target_relative_path=excluded.target_relative_path,
                    target_sha256=excluded.target_sha256,
                    media_type=excluded.media_type,
                    channel_slug=excluded.channel_slug,
                    daypart_slug=excluded.daypart_slug,
                    collection_type=excluded.collection_type,
                    collection_slug=excluded.collection_slug,
                    holiday_slug=excluded.holiday_slug,
                    series_title=excluded.series_title,
                    display_title=excluded.display_title,
                    file_size=excluded.file_size,modified_ns=excluded.modified_ns,
                    status=excluded.status,detail=excluded.detail,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    relative, int(target["id"]) if target is not None else None,
                    target_relative, digest, classification.media_type,
                    classification.channel_slug, classification.daypart_slug,
                    classification.collection_type, classification.collection_slug,
                    classification.holiday_slug, classification.series_title,
                    classification.display_title, stat.st_size, stat.st_mtime_ns,
                    status, detail, now, now,
                ),
            )
            shadow_id = int(self.connection.execute(
                "SELECT id FROM shadow_files WHERE shadow_relative_path=?", (relative,)
            ).fetchone()["id"])
            self.connection.execute(
                "UPDATE shadow_files SET holiday_weekly_slug=? WHERE id=?",
                (classification.holiday_weekly_slug, shadow_id),
            )
            self.connection.execute("DELETE FROM shadow_eligibility WHERE shadow_id=?", (shadow_id,))
            if status == "ready":
                self.connection.executemany(
                    """
                    INSERT INTO shadow_eligibility(
                        shadow_id,channel_slug,daypart_slug,eligibility_source,weight,enabled
                    ) VALUES (?,?,?,?,?,1)
                    """,
                    [
                        (shadow_id, item.channel_slug, item.daypart_slug, item.source, item.weight)
                        for item in classification.eligibility
                    ],
                )

        existing = {
            str(row["shadow_relative_path"]): int(row["id"])
            for row in self.connection.execute("SELECT id,shadow_relative_path FROM shadow_files")
        }
        removed = 0
        for relative, shadow_id in existing.items():
            if relative not in seen:
                self.connection.execute("DELETE FROM shadow_files WHERE id=?", (shadow_id,))
                removed += 1
        self.connection.commit()
        return ShadowRefreshResult(len(seen), ready, review, removed)

    @staticmethod
    def _media_tail(relative: str) -> tuple[str, PurePosixPath]:
        parts = PurePosixPath(relative).parts
        for index, part in enumerate(parts):
            media_type = MEDIA_TYPES.get(slugify(part))
            if media_type and index + 1 < len(parts):
                return media_type, PurePosixPath(*parts[index + 1:])
        raise ValueError(f"Cannot determine the media category for {relative}")

    @staticmethod
    def _destination_prefix(
        channel: str, scope: str, value: str, media_type: str
    ) -> PurePosixPath:
        channel_slug = slugify(channel)
        value_slug = slugify(value)
        if not channel_slug or not value_slug:
            raise ValueError("A channel and destination are required")
        folder = MEDIA_FOLDERS.get(media_type)
        if not folder:
            raise ValueError(f"Unsupported shadow media type: {media_type}")
        if scope == "daypart":
            return PurePosixPath(channel_slug, value_slug, folder)
        if scope == "weekly":
            return PurePosixPath(channel_slug, "specials", "weekly", value_slug, folder)
        if scope == "holiday":
            return PurePosixPath(channel_slug, "specials", "holidays", value_slug, folder)
        raise ValueError("Invalid shadow destination type")

    def _source_assets(
        self, *, asset_id: int | None = None, folder: str | None = None
    ) -> list[sqlite3.Row]:
        if asset_id is not None:
            rows = self.connection.execute(
                """
                SELECT a.*,h.working_sha256 FROM assets a
                LEFT JOIN asset_content_hashes h ON h.asset_id=a.id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND a.id=? AND a.integrity_status IN ('ready','not_scanned')
                """,
                (asset_id,),
            ).fetchall()
        else:
            normalized = PurePosixPath(str(folder or "").replace("\\", "/"))
            if not str(normalized) or normalized.is_absolute() or ".." in normalized.parts:
                raise ValueError("Invalid source folder")
            prefix = normalized.as_posix().rstrip("/") + "/"
            rows = self.connection.execute(
                """
                SELECT a.*,h.working_sha256 FROM assets a
                LEFT JOIN asset_content_hashes h ON h.asset_id=a.id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE logical_name='media' AND source_kind='managed') AND a.source_relative_path LIKE ? ESCAPE '\\'
                  AND a.integrity_status IN ('ready','not_scanned')
                ORDER BY a.source_relative_path
                """,
                (prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
            shadow_rows = self.connection.execute(
                """
                SELECT a.*,h.working_sha256 FROM shadow_files s
                JOIN assets a ON a.id=s.target_asset_id
                LEFT JOIN asset_content_hashes h ON h.asset_id=a.id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE logical_name='media' AND source_kind='managed') AND s.shadow_relative_path LIKE ? ESCAPE '\\'
                  AND s.status='ready'
                  AND a.integrity_status IN ('ready','not_scanned')
                ORDER BY s.shadow_relative_path
                """,
                (prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            for row in shadow_rows:
                by_id.setdefault(int(row["id"]), row)
            rows = list(by_id.values())
        if not rows:
            raise ValueError("No indexed media was found for that source")
        return rows

    def create(
        self,
        *,
        channel: str,
        scope: str,
        value: str,
        asset_id: int | None = None,
        folder: str | None = None,
    ) -> dict[str, object]:
        rows = self._source_assets(asset_id=asset_id, folder=folder)
        options = self.options()
        allowed_channels = {str(item["slug"]) for item in options["channels"]} | {"shared"}
        channel_slug = slugify(channel)
        value_slug = slugify(value)
        if channel_slug not in allowed_channels:
            raise ValueError("Unknown shadow destination channel")
        if scope == "daypart" and value_slug not in options["dayparts"]:
            raise ValueError("Unknown shadow destination daypart")
        if scope == "weekly" and not any(
            item["channel"] == channel_slug and item["slug"] == value_slug
            for item in options["weekly"]
        ):
            raise ValueError("That recurring block is not configured for this channel")
        if scope == "holiday" and value_slug not in options["holidays"]:
            raise ValueError("Unknown shadow holiday collection")

        created: list[str] = []
        skipped: list[str] = []
        conflicts: list[str] = []
        plans: list[tuple[Path, PurePosixPath, str]] = []
        for row in rows:
            media_type = str(row["media_type"] or "")
            if media_type not in PROGRAM_MEDIA_TYPES | {
                "commercial", "bumper", "promo", "station_id", "filler"
            }:
                skipped.append(str(row["source_relative_path"]))
                continue
            _, tail = self._media_tail(str(row["source_relative_path"]))
            prefix = self._destination_prefix(channel, scope, value, media_type)
            destination_relative = PurePosixPath(
                prefix, *tail.parts[:-1], tail.name + SHADOW_SUFFIX
            )
            channel_root = resolve_channel_root(self.media_root, channel_slug)
            actual_prefix = PurePosixPath(
                channel_root.relative_to(self.media_root).as_posix()
            )
            destination_relative = PurePosixPath(
                actual_prefix,
                *destination_relative.parts[1:],
            )
            destination = (
                self.media_root / Path(destination_relative.as_posix())
            ).resolve()
            destination.relative_to(self.media_root)
            payload = {
                "version": 1,
                "target": str(row["source_relative_path"]).replace("\\", "/"),
                "sha256": row["working_sha256"],
            }
            encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            classification = self._classification(destination_relative.as_posix())
            if classification.review_reasons:
                raise ValueError(
                    f"Invalid shadow destination {destination_relative.as_posix()}: "
                    + "; ".join(item[1] for item in classification.review_reasons)
                )
            if classification.media_type != media_type:
                raise ValueError("Shadow destination changed the target media type")
            plans.append((destination, destination_relative, encoded))

        for destination, destination_relative, encoded in plans:
            if destination.exists():
                try:
                    if destination.read_text(encoding="utf-8") == encoded:
                        skipped.append(destination_relative.as_posix())
                    else:
                        conflicts.append(destination_relative.as_posix())
                except OSError:
                    conflicts.append(destination_relative.as_posix())
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".partial")
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, destination)
            created.append(destination_relative.as_posix())
        refresh = self.refresh()
        return {
            "created": created,
            "skipped": skipped,
            "conflicts": conflicts,
            "refresh": refresh.__dict__,
        }

    def inventory(self) -> dict[str, object]:
        rows = self.connection.execute(
            """
            SELECT s.*,a.display_title AS target_title,a.original_filename,
                   a.source_relative_path AS current_target_path
            FROM shadow_files s LEFT JOIN assets a ON a.id=s.target_asset_id
            ORDER BY s.status DESC,s.channel_slug,s.shadow_relative_path
            """
        ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "ready": sum(item["status"] == "ready" for item in items),
                "review": sum(item["status"] == "review" for item in items),
            },
        }

    @staticmethod
    def _normalize_shadow_ids(shadow_ids: object) -> list[int]:
        if not isinstance(shadow_ids, list) or not shadow_ids:
            raise ValueError("Choose at least one shadow file")
        if len(shadow_ids) > 10000:
            raise ValueError("Too many shadow files were selected")
        try:
            result = sorted({int(value) for value in shadow_ids})
        except (TypeError, ValueError) as exc:
            raise ValueError("Shadow identifiers must be whole numbers") from exc
        if any(value <= 0 for value in result):
            raise ValueError("Shadow identifiers must be positive")
        return result

    def _deletion_rows(self, shadow_ids: object) -> list[sqlite3.Row]:
        identifiers = self._normalize_shadow_ids(shadow_ids)
        placeholders = ",".join("?" for _ in identifiers)
        rows = self.connection.execute(
            f"""
            SELECT s.*,a.display_title AS target_title,a.original_filename
            FROM shadow_files s
            LEFT JOIN assets a ON a.id=s.target_asset_id
            WHERE s.id IN ({placeholders})
            ORDER BY s.id
            """,
            tuple(identifiers),
        ).fetchall()
        if len(rows) != len(identifiers):
            raise ValueError(
                "The selected shadows changed. Refresh the inventory and preview again."
            )
        return rows

    def _validated_shadow_path(self, row: sqlite3.Row) -> Path:
        relative = PurePosixPath(str(row["shadow_relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("A shadow path escapes the media library")
        path = (self.media_root / Path(relative.as_posix())).resolve()
        try:
            path.relative_to(self.media_root)
        except ValueError as exc:
            raise ValueError("A shadow path escapes the media library") from exc
        if not path.name.casefold().endswith(SHADOW_SUFFIX):
            raise ValueError("A selected database record is not a shadow sidecar")
        if path.exists() and not path.is_file():
            raise ValueError(f"Shadow path is not a file: {relative.as_posix()}")
        return path

    def _deletion_token(self, rows: list[sqlite3.Row]) -> str:
        identity = []
        for row in rows:
            path = self._validated_shadow_path(row)
            stat = path.stat() if path.is_file() else None
            identity.append({
                "id": int(row["id"]),
                "path": str(row["shadow_relative_path"]),
                "size": stat.st_size if stat is not None else None,
                "modified": stat.st_mtime_ns if stat is not None else None,
                "target": row["target_asset_id"],
            })
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def deletion_preview(self, shadow_ids: object) -> dict[str, object]:
        rows = self._deletion_rows(shadow_ids)
        titles = {
            str(row["series_title"] or row["target_title"] or row["original_filename"])
            for row in rows
        }
        channels = {str(row["channel_slug"] or "shared") for row in rows}
        locations = {
            (
                f"{row['collection_type']}: {row['collection_slug']}"
                if row["collection_type"]
                else str(row["daypart_slug"] or "all-day")
            )
            for row in rows
        }
        return {
            "shadow_ids": [int(row["id"]) for row in rows],
            "token": self._deletion_token(rows),
            "count": len(rows),
            "title": ", ".join(sorted(titles)) if titles else "Shadow files",
            "channel": ", ".join(sorted(channels)),
            "location": ", ".join(sorted(locations)),
        }

    def delete(self, shadow_ids: object, token: object) -> dict[str, object]:
        rows = self._deletion_rows(shadow_ids)
        if not isinstance(token, str) or token != self._deletion_token(rows):
            raise ValueError(
                "The selected shadows changed. Refresh the inventory and preview again."
            )

        paths: list[tuple[sqlite3.Row, Path]] = []
        for row in rows:
            paths.append((row, self._validated_shadow_path(row)))

        deleted: list[str] = []
        missing: list[str] = []
        failed: list[dict[str, str]] = []
        for row, path in paths:
            relative = str(row["shadow_relative_path"])
            if not path.exists():
                missing.append(relative)
                continue
            try:
                path.unlink()
                deleted.append(relative)
            except OSError as exc:
                failed.append({"path": relative, "error": str(exc)})

        self.refresh()
        return {
            "deleted": deleted,
            "already_missing": missing,
            "failed": failed,
            "original_media_untouched": True,
        }

    def source_folders(self) -> list[dict[str, object]]:
        counts: dict[str, int] = {}
        rows = self.connection.execute(
            """
            SELECT source_relative_path FROM assets
            WHERE source_root_id IN (SELECT id FROM storage_roots WHERE logical_name='media' AND source_kind='managed') AND integrity_status IN ('ready','not_scanned')
            ORDER BY source_relative_path
            """
        ).fetchall()
        for row in rows:
            parent = PurePosixPath(str(row["source_relative_path"])).parent
            while str(parent) not in {"", "."}:
                counts[parent.as_posix()] = counts.get(parent.as_posix(), 0) + 1
                parent = parent.parent
        return [
            {"path": path, "count": count}
            for path, count in sorted(counts.items(), key=lambda item: item[0].casefold())
        ]

    def options(self) -> dict[str, object]:
        channels = [
            {"slug": str(row["slug"]), "name": str(row["display_name"])}
            for row in self.connection.execute(
                "SELECT slug,display_name FROM channels WHERE enabled=1 ORDER BY display_name"
            )
        ]
        channels.append({"slug": "shared", "name": "Shared"})
        dayparts = [
            str(item["slug"])
            for item in self.config.data.get("scheduling", {}).get("dayparts", [])
        ]
        dayparts.extend(value for value in ("daytime", "all-day") if value not in dayparts)
        weekly = [
            {
                "channel": str(row["channel_slug"]),
                "slug": str(row["collection_slug"]),
                "label": str(row["guide_label"] or row["collection_slug"]),
            }
            for row in self.connection.execute(
                """
                SELECT DISTINCT channel_slug,collection_slug,guide_label
                FROM calendar_rules
                WHERE rule_type='weekly' AND enabled=1
                ORDER BY channel_slug,collection_slug
                """
            )
        ]
        holidays = sorted(self._known_holidays())
        return {
            "channels": channels,
            "dayparts": dayparts,
            "weekly": weekly,
            "holidays": holidays,
        }
