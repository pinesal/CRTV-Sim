from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import zlib
from datetime import datetime
from pathlib import Path
from struct import pack
from zoneinfo import ZoneInfo

from retrotv.classification import resolve_channel_root, slugify
from retrotv.db import utc_now


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm"}
GRAPHIC_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
PURPOSES = ("up-next", "interstitials")
DEFAULT_HOLIDAYS = ("halloween", "christmas")
_AUTO_WEEKLY = object()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return pack(">I", len(data)) + kind + data + pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    row = bytes((0,)) + bytes(rgb) * width
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


class GraphicsService:
    """Owns channel-local graphics without exposing them to the media scanner."""

    def __init__(self, connection, config):
        self.connection = connection
        self.config = config
        self.settings = config.data.get("graphics", {})
        self.media_root = config.storage_root("media")

    def ensure_all(self) -> dict[str, int]:
        from retrotv.settings import SettingsService

        settings_service = SettingsService(self.config)
        settings_service._ensure_weekly_collection_directories(
            {}, self.config.data.get("seasonal", {}).get("windows", {}), existing_channels_only=True
        )
        settings_service.ensure_holiday_directories(
            self.config.data.get("seasonal", {}).get("windows", {}),
            self.config.data.get("seasonal", {}).get("holidays", {}),
            self.config.data.get("scheduling", {}).get("dayparts", []),
            existing_channels_only=True,
        )
        created_folders = created_placeholders = 0
        channels = self.connection.execute(
            "SELECT id, slug, display_name FROM channels WHERE enabled = 1"
        ).fetchall()
        folder_map = {
            slugify(path.name): path
            for path in self.media_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
        for row in channels:
            channel_root = folder_map.get(str(row["slug"]), self.media_root / str(row["slug"]))
            if not channel_root.is_dir():
                # Missing channel roots require a deliberate recreate/delete
                # choice; catalog maintenance must not silently recreate them.
                continue
            counts = self.ensure_structure(
                channel_root,
                str(row["display_name"]),
                channel_slug=str(row["slug"]),
            )
            created_folders += counts[0]
            created_placeholders += counts[1]
            for collection in self._configured_weekly_collections(str(row["slug"])):
                collection_root = channel_root / "specials" / "weekly" / collection
                for purpose in PURPOSES:
                    folder = collection_root / purpose
                    if folder.exists():
                        if not folder.is_dir():
                            raise ValueError(
                                f"Weekly graphic path is not a directory: {folder}"
                            )
                        continue
                    folder.mkdir(parents=True)
                    created_folders += 1
        template = self.media_root / "_channel_template"
        counts = self.ensure_structure(template, "YOUR CHANNEL")
        return {
            "folders_created": created_folders + counts[0],
            "placeholders_created": created_placeholders + counts[1],
            "cataloged": self.scan_catalog(),
        }

    def ensure_structure(
        self,
        channel_root: Path,
        display_name: str,
        *,
        channel_slug: str | None = None,
    ) -> tuple[int, int]:
        folders = [
            channel_root / "graphics" / purpose for purpose in PURPOSES
        ]
        for holiday in self._holidays():
            folders.extend(
                channel_root / "graphics" / "holidays" / holiday / purpose
                for purpose in PURPOSES
            )
        overrides = self._daypart_overrides(channel_slug or slugify(channel_root.name))
        for daypart_slug in overrides:
            folders.extend(
                channel_root / "graphics" / daypart_slug / purpose
                for purpose in PURPOSES
            )
            for holiday in self._holidays():
                folders.extend(
                    channel_root / "graphics" / daypart_slug / "holidays" / holiday / purpose
                    for purpose in PURPOSES
                )
        created_folders = created_placeholders = 0
        for folder in folders:
            if not folder.is_dir():
                folder.mkdir(parents=True, exist_ok=True)
                created_folders += 1
            placeholder = folder / "placeholder.png"
            if not placeholder.exists():
                parts = [part.casefold() for part in folder.parts]
                override_name = display_name
                for daypart_slug, values in overrides.items():
                    if daypart_slug in parts:
                        override_name = str(values.get("display_name", display_name))
                        break
                self._write_placeholder(placeholder, override_name, folder)
                created_placeholders += 1
        return created_folders, created_placeholders

    def regenerate_placeholder(
        self,
        channel_slug: str,
        purpose: str,
        holiday: str | None = None,
        daypart_slug: str | None = None,
    ) -> Path:
        if purpose not in PURPOSES:
            raise ValueError(f"Invalid graphic purpose: {purpose}")
        row = self.connection.execute(
            "SELECT display_name FROM channels WHERE slug = ?", (channel_slug,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown channel: {channel_slug}")
        root = self._channel_root(channel_slug) / "graphics"
        scope = root / slugify(daypart_slug) if daypart_slug else root
        folder = scope / purpose if not holiday else scope / "holidays" / slugify(holiday) / purpose
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "placeholder.png"
        identity = self.identity_name(
            channel_slug, str(row["display_name"]), daypart_slug
        )
        self._write_placeholder(target, identity, folder, overwrite=True)
        self.scan_catalog()
        return target

    def _write_placeholder(
        self, target: Path, channel_name: str, folder: Path, *, overwrite: bool = False
    ) -> None:
        if target.exists() and not overwrite:
            return
        color = self._channel_color(slugify(channel_name))
        rgb = tuple(int(color[index:index + 2], 16) for index in (0, 2, 4))
        is_up_next = folder.name.casefold() == "up-next"
        background_template = (
            self.config.project_root
            / "assets"
            / "templates"
            / "up-next-background-template.png"
        )
        if is_up_next and background_template.is_file():
            shutil.copy2(background_template, target)
        else:
            target.write_bytes(_solid_png(640, 480, rgb))
        font = self.config.project_root / "assets" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
        if not font.exists():
            font = self.config.project_root / "assets" / "fonts" / "DejaVuSansMono.ttf"
        if not font.is_file():
            return
        parts = [part.casefold() for part in folder.parts]
        purpose = "UP NEXT" if is_up_next else "STATION INTERSTITIAL"
        holiday = ""
        if "holidays" in parts:
            index = parts.index("holidays")
            if index + 1 < len(folder.parts):
                holiday = f"{folder.parts[index + 1].upper()}  "
        label = f"{holiday}{purpose}"
        escaped_font = font.as_posix().replace(":", "\\:").replace("'", "\\'")
        escaped_channel = channel_name.upper().replace("'", "\\'").replace(":", "\\:")
        escaped_label = label.replace("'", "\\'").replace(":", "\\:")
        temporary = target.with_name(f".{target.stem}-branded.png")
        if is_up_next:
            logo_size = max(14, min(24, 300 // max(1, len(channel_name))))
            filter_value = (
                f"drawtext=fontfile='{escaped_font}':text='{escaped_channel}':"
                f"fontcolor=white:fontsize={logo_size}:x=(w-text_w)/2:"
                "y=(80-text_h)/2:borderw=2:bordercolor=black"
            )
        else:
            filter_value = (
                f"drawtext=fontfile='{escaped_font}':text='{escaped_channel}':"
                "fontcolor=white:fontsize=48:x=(w-text_w)/2:y=170:"
                "borderw=3:bordercolor=black,"
                f"drawtext=fontfile='{escaped_font}':text='{escaped_label}':"
                "fontcolor=white:fontsize=24:x=(w-text_w)/2:y=250:"
                "borderw=2:bordercolor=black"
            )
        try:
            completed = hidden_run(
                [self.config.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-i", os.fspath(target), "-vf", filter_value,
                 "-frames:v", "1", os.fspath(temporary)],
                capture_output=True, text=True, timeout=60,
            )
            if completed.returncode == 0 and temporary.is_file():
                try:
                    os.replace(temporary, target)
                except PermissionError:
                    # Windows can briefly retain the input image handle after
                    # FFmpeg exits. This file is a generated placeholder, so a
                    # direct replacement retry is safe.
                    target.unlink(missing_ok=True)
                    os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def scan_catalog(self) -> int:
        now = utc_now()
        seen: set[str] = set()
        seen_audio: set[str] = set()
        # Collect paths, stats and FFprobe results before acquiring SQLite's
        # writer reservation. Network folders and probes can be slow.
        updates = []
        rows = self.connection.execute(
            "SELECT id, slug FROM channels WHERE enabled = 1"
        ).fetchall()
        for channel in rows:
            channel_root = self._channel_root(str(channel["slug"]))
            for root, weekly_collection, variant_holiday in self._catalog_roots(channel_root):
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.suffix.casefold() not in GRAPHIC_EXTENSIONS:
                        continue
                    if (variant_holiday and weekly_collection is None
                            and path.relative_to(root).parts[0].casefold() == "holidays"):
                        continue
                    if (
                        weekly_collection
                        and path.relative_to(root).parts[0].casefold() not in PURPOSES
                    ):
                        continue
                    parsed = self._classify_graphic(root, path)
                    if parsed is None:
                        continue
                    purpose, holiday, daypart = parsed
                    holiday = variant_holiday or holiday
                    relative = path.relative_to(self.media_root).as_posix()
                    stat = path.stat()
                    kind = "image" if path.suffix.casefold() in IMAGE_EXTENSIONS else "video"
                    duration = self._video_duration(path) if kind == "video" else None
                    provenance = (
                        "placeholder" if path.name.casefold() == "placeholder.png"
                        else "ai" if path.name.casefold().startswith("ai-")
                        else "user"
                    )
                    updates.append((str(channel['slug']),
                        """
                        INSERT INTO graphic_assets(
                            channel_id,relative_path,purpose,holiday_slug,daypart_slug,
                            weekly_collection_slug,source_kind,duration_seconds,file_size,
                            modified_ns,validation_status,provenance,provenance_json,
                            detail,first_seen_at,last_seen_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,'valid',?,'{}',NULL,?,?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            channel_id=excluded.channel_id,purpose=excluded.purpose,
                            holiday_slug=excluded.holiday_slug,
                            daypart_slug=excluded.daypart_slug,
                            weekly_collection_slug=excluded.weekly_collection_slug,
                            source_kind=excluded.source_kind,
                            duration_seconds=excluded.duration_seconds,
                            file_size=excluded.file_size,modified_ns=excluded.modified_ns,
                            validation_status='valid',provenance=excluded.provenance,
                            detail=NULL,last_seen_at=excluded.last_seen_at
                        """,
                        (
                            int(channel["id"]), relative, purpose, holiday, daypart,
                            weekly_collection, kind, duration, stat.st_size,
                            stat.st_mtime_ns, provenance, now, now,
                        ),
                    ))
                    seen.add(relative)
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
                        continue
                    if (variant_holiday and weekly_collection is None
                            and path.relative_to(root).parts[0].casefold() == "holidays"):
                        continue
                    if (
                        weekly_collection
                        and path.relative_to(root).parts[0].casefold() not in PURPOSES
                    ):
                        continue
                    parsed = self._classify_audio(root, path)
                    if parsed is None:
                        continue
                    purpose, holiday, daypart = parsed
                    holiday = variant_holiday or holiday
                    relative = path.relative_to(self.media_root).as_posix()
                    stat = path.stat()
                    updates.append((str(channel['slug']),
                        """
                        INSERT INTO graphic_audio_assets(
                            channel_id,relative_path,purpose,holiday_slug,daypart_slug,
                            weekly_collection_slug,duration_seconds,file_size,modified_ns,
                            validation_status,provenance,provenance_json,detail,
                            first_seen_at,last_seen_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,'valid','user','{}',NULL,?,?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            channel_id=excluded.channel_id,purpose=excluded.purpose,
                            holiday_slug=excluded.holiday_slug,
                            daypart_slug=excluded.daypart_slug,
                            weekly_collection_slug=excluded.weekly_collection_slug,
                            duration_seconds=excluded.duration_seconds,
                            file_size=excluded.file_size,modified_ns=excluded.modified_ns,
                            validation_status='valid',detail=NULL,last_seen_at=excluded.last_seen_at
                        """,
                        (
                            int(channel["id"]), relative, purpose, holiday, daypart,
                            weekly_collection, self._video_duration(path), stat.st_size,
                            stat.st_mtime_ns, now, now,
                        ),
                    ))
                    seen_audio.add(relative)
        return self._publish_catalog(updates, seen, seen_audio)

    def _publish_catalog(self, updates, seen, seen_audio) -> int:
        count = 0
        with self.connection:
            # Reserve the writer before rechecking channel identities, so a
            # deletion cannot race publication. No filesystem work happens here.
            self.connection.execute('UPDATE graphic_assets SET id=id WHERE 0')
            live = {row['id']: row['slug'] for row in self.connection.execute(
                'SELECT id,slug FROM channels WHERE enabled=1')}
            for slug, sql, parameters in updates:
                if live.get(parameters[0]) == slug:
                    count += self.connection.execute(sql, parameters).rowcount
            for table, paths in (('graphic_assets', seen), ('graphic_audio_assets', seen_audio)):
                if paths:
                    placeholders = ','.join('?' for _ in paths)
                    self.connection.execute(
                        f"UPDATE {table} SET validation_status='missing', detail='File was not found during graphics scan' WHERE relative_path NOT IN ({placeholders})",
                        tuple(sorted(paths)),
                    )
                else:
                    self.connection.execute(
                        f"UPDATE {table} SET validation_status='missing', detail='File was not found during graphics scan'"
                    )
        return count

    def inventory(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT ga.*, c.slug AS channel_slug, c.display_name AS channel_name
            FROM graphic_assets ga JOIN channels c ON c.id=ga.channel_id
            WHERE ga.provenance!='placeholder' OR NOT EXISTS (
                SELECT 1 FROM graphic_assets replacement
                WHERE replacement.channel_id=ga.channel_id
                  AND replacement.purpose=ga.purpose
                  AND replacement.weekly_collection_slug IS ga.weekly_collection_slug
                  AND replacement.holiday_slug IS ga.holiday_slug
                  AND replacement.daypart_slug IS ga.daypart_slug
                  AND replacement.validation_status='valid'
                  AND replacement.provenance IN ('user','ai')
            )
            ORDER BY c.channel_number, c.display_name, ga.weekly_collection_slug,
                     ga.holiday_slug, ga.purpose,
                     ga.provenance, ga.relative_path
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["id"] = f"g-{item['id']}"
            item["catalog_kind"] = "graphic"
            result.append(item)
        audio_rows = self.connection.execute(
            """
            SELECT gaa.*, c.slug AS channel_slug, c.display_name AS channel_name,
                   'audio' AS source_kind
            FROM graphic_audio_assets gaa JOIN channels c ON c.id=gaa.channel_id
            ORDER BY c.channel_number,c.display_name,gaa.weekly_collection_slug,
                     gaa.holiday_slug,gaa.purpose,
                     gaa.relative_path
            """
        ).fetchall()
        for row in audio_rows:
            item = dict(row)
            item["id"] = f"a-{item['id']}"
            item["catalog_kind"] = "music"
            item["provenance"] = item.get("provenance", "user")
            result.append(item)
        return result

    def placeholder_todo(self) -> list[dict]:
        """Return exact graphic scopes still served only by a generated placeholder."""
        rows = self.connection.execute(
            """
            SELECT p.id,p.relative_path,p.purpose,p.holiday_slug,p.daypart_slug,
                   p.weekly_collection_slug,
                   p.source_kind,p.provenance,c.slug AS channel_slug,
                   c.display_name AS channel_name
            FROM graphic_assets p
            JOIN channels c ON c.id=p.channel_id
            WHERE c.enabled=1 AND p.provenance='placeholder'
              AND p.validation_status='valid'
              AND NOT EXISTS (
                    SELECT 1 FROM graphic_assets replacement
                    WHERE replacement.channel_id=p.channel_id
                      AND replacement.purpose=p.purpose
                      AND replacement.weekly_collection_slug IS p.weekly_collection_slug
                      AND replacement.holiday_slug IS p.holiday_slug
                      AND replacement.daypart_slug IS p.daypart_slug
                      AND replacement.validation_status='valid'
                      AND replacement.provenance IN ('user','ai')
              )
            ORDER BY c.channel_number IS NULL,c.channel_number,c.display_name,
                     p.daypart_slug,p.holiday_slug,p.purpose
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["id"] = f"g-{item['id']}"
            item["replacement_folder"] = str(Path(item["relative_path"]).parent)
            result.append(item)
        return result

    def select(
        self,
        channel_slug: str,
        purpose: str,
        when: datetime,
        *,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None | object = _AUTO_WEEKLY,
    ) -> Path | None:
        row = self.select_asset(
            channel_slug, purpose, when, daypart_slug=daypart_slug,
            weekly_collection_slug=weekly_collection_slug,
        )
        if row is None:
            return None
        path = self.media_root / Path(row["relative_path"])
        return path if path.is_file() else None

    def select_asset(
        self,
        channel_slug: str,
        purpose: str,
        when: datetime,
        *,
        rng: random.Random | None = None,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None | object = _AUTO_WEEKLY,
    ):
        channel = self.connection.execute(
            "SELECT id FROM channels WHERE slug=? AND enabled=1", (channel_slug,)
        ).fetchone()
        if channel is None:
            return None
        weekly_collection = (self.weekly_collection_at(channel_slug, when)
                             if weekly_collection_slug is _AUTO_WEEKLY
                             else slugify(weekly_collection_slug or ""))
        placeholder_fallback = None
        if weekly_collection:
            active_holiday = self._active_holiday(when)
            rows = self.connection.execute(
                """
                SELECT * FROM graphic_assets
                WHERE channel_id=? AND purpose=? AND weekly_collection_slug=?
                  AND (holiday_slug IS ? OR holiday_slug IS NULL)
                  AND validation_status='valid'
                ORDER BY CASE WHEN holiday_slug IS ? THEN 0 ELSE 1 END,
                         CASE provenance WHEN 'user' THEN 0 WHEN 'ai' THEN 1 ELSE 2 END,
                         relative_path
                """,
                (int(channel["id"]), purpose, weekly_collection, active_holiday, active_holiday),
            ).fetchall()
            rows = [
                row for row in rows
                if (self.media_root / Path(row["relative_path"])).is_file()
            ]
            if rows:
                # Authored standard weekly art also beats a holiday placeholder.
                # If this collection has only placeholders, defer it until all
                # applicable channel artwork has been checked.
                authored = [row for row in rows if row["provenance"] != "placeholder"]
                rows = authored or rows
                if active_holiday and any(row["holiday_slug"] == active_holiday for row in rows):
                    rows = [row for row in rows if row["holiday_slug"] == active_holiday]
                else:
                    rows = [row for row in rows if row["holiday_slug"] is None]
                best_rank = min(
                    {"user": 0, "ai": 1, "placeholder": 2}.get(str(row["provenance"]), 3)
                    for row in rows
                )
                choices = [
                    row for row in rows
                    if {"user": 0, "ai": 1, "placeholder": 2}.get(str(row["provenance"]), 3)
                    == best_rank
                ]
                chosen = (rng or random.SystemRandom()).choice(choices)
                if authored:
                    return chosen
                placeholder_fallback = chosen
        holiday = self._active_holiday(when)
        daypart = daypart_slug or self.daypart_slug_at(when)
        scopes: list[tuple[str | None, str | None]] = []
        if daypart:
            if holiday:
                scopes.append((daypart, holiday))
        if holiday:
            scopes.append((None, holiday))
        if daypart:
            scopes.append((daypart, None))
        scopes.append((None, None))
        for desired_daypart, desired_holiday in scopes:
            rows = self.connection.execute(
                """
                SELECT * FROM graphic_assets
                WHERE channel_id=? AND purpose=? AND holiday_slug IS ?
                  AND daypart_slug IS ?
                  AND weekly_collection_slug IS NULL
                  AND validation_status='valid'
                ORDER BY CASE provenance WHEN 'user' THEN 0 WHEN 'ai' THEN 1 ELSE 2 END,
                         relative_path
                """,
                (int(channel["id"]), purpose, desired_holiday, desired_daypart),
            ).fetchall()
            rows = [
                row for row in rows
                if (self.media_root / Path(row["relative_path"])).is_file()
            ]
            if rows:
                authored = [row for row in rows if row["provenance"] != "placeholder"]
                if not authored:
                    # Automatically seeded placeholders must not hide real
                    # channel-wide holiday art or real standard fallback art.
                    if placeholder_fallback is None:
                        placeholder_fallback = (rng or random.SystemRandom()).choice(rows)
                    continue
                rows = authored
                best_rank = min(
                    {"user": 0, "ai": 1, "placeholder": 2}.get(str(row["provenance"]), 3)
                    for row in rows
                )
                choices = [
                    row for row in rows
                    if {"user": 0, "ai": 1, "placeholder": 2}.get(str(row["provenance"]), 3)
                    == best_rank
                ]
                # Legacy holiday roots remain a read-only fallback. Prefer an
                # equally authored graphic in the canonical graphics tree so
                # duplicate layouts cannot compete randomly.
                canonical = [item for item in choices
                             if Path(item["relative_path"]).parts[1] == "graphics"]
                if canonical:
                    choices = canonical
                return (rng or random.SystemRandom()).choice(choices)
        return placeholder_fallback

    def select_music(
        self,
        channel_slug: str,
        purpose: str,
        when: datetime,
        *,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None | object = _AUTO_WEEKLY,
    ) -> Path | None:
        channel = self.connection.execute(
            "SELECT id FROM channels WHERE slug=? AND enabled=1", (channel_slug,)
        ).fetchone()
        if channel is None:
            return None
        weekly_collection = (self.weekly_collection_at(channel_slug, when)
                             if weekly_collection_slug is _AUTO_WEEKLY
                             else slugify(weekly_collection_slug or ""))
        if weekly_collection:
            active_holiday = self._active_holiday(when)
            rows = self.connection.execute(
                """
                SELECT * FROM graphic_audio_assets
                WHERE channel_id=? AND purpose=? AND weekly_collection_slug=?
                  AND (holiday_slug IS ? OR holiday_slug IS NULL)
                  AND validation_status='valid'
                ORDER BY CASE WHEN holiday_slug IS ? THEN 0 ELSE 1 END,relative_path
                """,
                (int(channel["id"]), purpose, weekly_collection, active_holiday, active_holiday),
            ).fetchall()
            if active_holiday and any(row["holiday_slug"] == active_holiday for row in rows):
                rows = [row for row in rows if row["holiday_slug"] == active_holiday]
            else:
                rows = [row for row in rows if row["holiday_slug"] is None]
            paths = [
                self.media_root / Path(row["relative_path"])
                for row in rows
                if (self.media_root / Path(row["relative_path"])).is_file()
            ]
            if paths:
                return random.SystemRandom().choice(paths)
        holiday = self._active_holiday(when)
        daypart = daypart_slug or self.daypart_slug_at(when)
        scopes: list[tuple[str | None, str | None, str | None]] = []
        if daypart:
            if holiday:
                scopes.append((daypart, purpose, holiday))
            scopes.extend(
                ((daypart, purpose, None), (daypart, None, None))
            )
        if holiday:
            scopes.append((None, purpose, holiday))
        scopes.extend(((None, purpose, None), (None, None, None)))
        for desired_daypart, desired_purpose, desired_holiday in scopes:
            rows = self.connection.execute(
                """
                SELECT * FROM graphic_audio_assets
                WHERE channel_id=? AND purpose IS ? AND holiday_slug IS ?
                  AND daypart_slug IS ?
                  AND weekly_collection_slug IS NULL
                  AND validation_status='valid'
                ORDER BY relative_path
                """,
                (
                    int(channel["id"]), desired_purpose, desired_holiday,
                    desired_daypart,
                ),
            ).fetchall()
            paths = [
                self.media_root / Path(row["relative_path"])
                for row in rows
                if (self.media_root / Path(row["relative_path"])).is_file()
            ]
            if paths:
                return random.SystemRandom().choice(paths)
        return None

    def render_interstitial(
        self,
        graphic_asset_id: int,
        *,
        duration_seconds: float | None = None,
        force: bool = False,
        when: datetime | None = None,
    ) -> Path:
        row = self.connection.execute(
            """
            SELECT ga.*,c.slug AS channel_slug FROM graphic_assets ga
            JOIN channels c ON c.id=ga.channel_id
            WHERE ga.id=? AND ga.validation_status='valid'
            """,
            (graphic_asset_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Graphic asset {graphic_asset_id} is unavailable")
        if row["provenance"] == "placeholder":
            # Existing schedules can still refer to a placeholder chosen before
            # real art was added. Upgrade the presentation at render time without
            # changing schedule rows, timing, or an already-playing screen.
            replacement = self.select_asset(str(row["channel_slug"]), str(row["purpose"]),
                when or datetime.now().astimezone())
            if replacement is not None and replacement["provenance"] != "placeholder":
                graphic_asset_id = int(replacement["id"])
                row = self.connection.execute("""SELECT ga.*,c.slug AS channel_slug FROM graphic_assets ga
                    JOIN channels c ON c.id=ga.channel_id WHERE ga.id=?""", (graphic_asset_id,)).fetchone()
        source = self.media_root / Path(row["relative_path"])
        if not source.is_file():
            raise FileNotFoundError(source)
        duration = max(
            1.0,
            float(
                duration_seconds
                if duration_seconds is not None
                else self.settings.get("interstitial_duration_seconds", 10)
            ),
        )
        music = self.select_music(
            str(row["channel_slug"]), "interstitials", when or datetime.now().astimezone()
        )
        music_identity = ""
        if music is not None:
            stat = music.stat()
            music_identity = f"{music.relative_to(self.media_root).as_posix()}|{stat.st_mtime_ns}"
        identity = hashlib.sha256(
            f"{row['relative_path']}|{row['modified_ns']}|{duration}|{music_identity}|3".encode()
        ).hexdigest()[:16]
        output_root = Path(str(self.settings.get("output_dir", "./data/generated/graphics")))
        if not output_root.is_absolute():
            output_root = (self.config.project_root / output_root).resolve()
        target = output_root / f"graphic-{graphic_asset_id}-{identity}.mp4"
        if target.is_file() and not force:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial.mp4")
        if str(row["source_kind"]) == "image":
            source_args = ["-loop", "1", "-framerate", "30", "-i", os.fspath(source)]
        else:
            source_args = ["-stream_loop", "-1", "-i", os.fspath(source)]
        if music is None:
            audio_args = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_filter = []
        else:
            audio_args = ["-stream_loop", "-1", "-i", os.fspath(music)]
            audio_filter = ["-af", f"volume={float(self.settings.get('music_volume', 0.28)):g}"]
        command = [
            self.config.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            *source_args,
            *audio_args,
            "-vf", "scale=640:480:force_original_aspect_ratio=increase,crop=640:480",
            "-map", "0:v", "-map", "1:a", *audio_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
            "-t", f"{duration:g}", os.fspath(partial),
        ]
        completed = hidden_run(command, capture_output=True, text=True, timeout=300)
        if completed.returncode:
            partial.unlink(missing_ok=True)
            raise RuntimeError(completed.stderr.strip() or "Graphic render failed")
        os.replace(partial, target)
        return target

    def _active_holiday(self, when: datetime) -> str | None:
        from retrotv.seasonal import SeasonalRuleEngine

        if self.config.data.get("seasonal", {}).get("holidays"):
            timezone = when.tzinfo or ZoneInfo(
                str(self.config.data.get("application", {}).get("timezone", "UTC"))
            )
            rules = SeasonalRuleEngine(
                self.connection, self.config, timezone
            ).active_holidays(when.date())
            return rules[0].slug if rules else None
        if when.month == 10 and "halloween" in self._holidays():
            return "halloween"
        if when.month == 12 and "christmas" in self._holidays():
            return "christmas"
        return None

    def _holidays(self) -> tuple[str, ...]:
        values = self.settings.get("holidays", DEFAULT_HOLIDAYS)
        return tuple(dict.fromkeys(slugify(str(value)) for value in values if str(value).strip()))

    def daypart_slug_at(self, when: datetime) -> str | None:
        minute = when.hour * 60 + when.minute
        raw_dayparts = self.config.data.get("scheduling", {}).get("dayparts", [])
        matches: list[str] = []
        for item in raw_dayparts:
            slug = slugify(str(item.get("slug", "")))
            try:
                start_hour, start_minute = map(int, str(item["start"]).split(":"))
                end_hour, end_minute = map(int, str(item["end"]).split(":"))
            except (KeyError, TypeError, ValueError):
                continue
            start = start_hour * 60 + start_minute
            end = end_hour * 60 + end_minute
            active = start <= minute < end if start < end else (
                minute >= start or minute < end
            )
            if active and slug:
                matches.append(slug)
        return matches[0] if len(matches) == 1 else None

    def branding_override(
        self, channel_slug: str, daypart_slug: str | None
    ) -> dict:
        if not daypart_slug:
            return {}
        return dict(
            self._daypart_overrides(channel_slug).get(slugify(daypart_slug), {})
        )

    def identity_name(
        self, channel_slug: str, default_name: str, daypart_slug: str | None
    ) -> str:
        return str(
            self.branding_override(channel_slug, daypart_slug).get(
                "display_name", default_name
            )
        )

    def _daypart_overrides(self, channel_slug: str) -> dict[str, dict]:
        configured = self.config.data.get("daypart_branding", {})
        channel = configured.get(slugify(channel_slug), {})
        if not isinstance(channel, dict):
            return {}
        return {
            slugify(str(daypart)): dict(values)
            for daypart, values in channel.items()
            if isinstance(values, dict) and slugify(str(daypart))
        }

    def _configured_weekly_collections(self, channel_slug: str) -> tuple[str, ...]:
        raw = self.config.data.get("seasonal", {}).get("windows", {})
        windows = raw.values() if isinstance(raw, dict) else raw
        collections = {
            slugify(str(item.get("collection", item.get("slug", ""))))
            for item in windows
            if isinstance(item, dict)
            and slugify(str(item.get("channel", ""))) == slugify(channel_slug)
        }
        return tuple(sorted(collection for collection in collections if collection))

    def _channel_root(self, slug: str) -> Path:
        return resolve_channel_root(self.media_root, slug)

    def destination_folder(
        self,
        channel_slug: str,
        purpose: str,
        *,
        holiday_slug: str | None = None,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> Path:
        """Return the canonical user/AI graphic folder for an exact scope."""
        if purpose not in PURPOSES:
            raise ValueError("Invalid graphics purpose")
        channel_root = self._channel_root(channel_slug)
        weekly = slugify(weekly_collection_slug or "")
        holiday = slugify(holiday_slug or "")
        if weekly:
            root = channel_root / "specials"
            if holiday:
                root = root / "holidays" / holiday / "specials"
            root = root / "weekly" / weekly
            return root / purpose
        root = channel_root / "graphics"
        if daypart_slug:
            root = root / slugify(daypart_slug)
        return root / purpose if not holiday else root / "holidays" / holiday / purpose

    @staticmethod
    def _catalog_roots(channel_root: Path) -> list[tuple[Path, str | None, str | None]]:
        """Return the normal graphics root and optional weekly override roots."""
        roots: list[tuple[Path, str | None, str | None]] = [(channel_root / "graphics", None, None)]
        weekly_root = channel_root / "specials" / "weekly"
        if weekly_root.is_dir():
            roots.extend(
                (collection, slugify(collection.name), None)
                for collection in sorted(
                    weekly_root.iterdir(), key=lambda item: item.name.casefold()
                )
                if collection.is_dir()
                and not collection.name.startswith(".")
                and slugify(collection.name)
            )
        holiday_root = channel_root / "specials" / "holidays"
        if holiday_root.is_dir():
            for holiday in holiday_root.iterdir():
                if not holiday.is_dir() or not slugify(holiday.name):
                    continue
                roots.append((holiday / "graphics", None, slugify(holiday.name)))
                for weekly_variants in (holiday / "specials" / "weekly", holiday / "weekly"):
                    if weekly_variants.is_dir():
                        roots.extend(
                            (collection, slugify(collection.name), slugify(holiday.name))
                            for collection in weekly_variants.iterdir()
                            if collection.is_dir() and slugify(collection.name)
                        )
        return roots

    def weekly_collection_at(
        self, channel_slug: str, when: datetime
    ) -> str | None:
        """Return the active weekly owner's collection without adding settings."""
        from retrotv.seasonal import SeasonalRuleEngine

        try:
            timezone = when.tzinfo or ZoneInfo(
                str(self.config.data.get("application", {}).get("timezone", "UTC"))
            )
            moment = when if when.tzinfo else when.replace(tzinfo=timezone)
            owner = SeasonalRuleEngine(
                self.connection, self.config, timezone
            ).active_owner(slugify(channel_slug), moment)
        except (KeyError, TypeError, ValueError):
            return None
        return owner.collection_slug if owner is not None else None

    @staticmethod
    def _classify_graphic(
        root: Path, path: Path
    ) -> tuple[str, str | None, str | None] | None:
        parts = [part.casefold() for part in path.relative_to(root).parts[:-1]]
        if len(parts) == 1 and parts[0] in PURPOSES:
            return parts[0], None, None
        if len(parts) == 3 and parts[0] == "holidays" and parts[2] in PURPOSES:
            return parts[2], slugify(parts[1]), None
        if len(parts) == 2 and parts[1] in PURPOSES:
            return parts[1], None, slugify(parts[0])
        if (
            len(parts) == 4
            and parts[1] == "holidays"
            and parts[3] in PURPOSES
        ):
            return parts[3], slugify(parts[2]), slugify(parts[0])
        return None

    @staticmethod
    def _classify_audio(
        root: Path, path: Path
    ) -> tuple[str | None, str | None, str | None] | None:
        parts = [part.casefold() for part in path.relative_to(root).parts[:-1]]
        # Accept both the compact legacy layout (tracks directly in a scope
        # folder) and an explicit music subfolder beneath that scope.
        if parts and parts[-1] == "music":
            parts = parts[:-1]
        if not parts:
            return None, None, None
        if len(parts) == 1 and parts[0] in PURPOSES:
            return parts[0], None, None
        if len(parts) == 3 and parts[0] == "holidays" and parts[2] in PURPOSES:
            return parts[2], slugify(parts[1]), None
        if len(parts) == 1:
            return None, None, slugify(parts[0])
        if len(parts) == 2 and parts[1] in PURPOSES:
            return parts[1], None, slugify(parts[0])
        if (
            len(parts) == 4
            and parts[1] == "holidays"
            and parts[3] in PURPOSES
        ):
            return parts[3], slugify(parts[2]), slugify(parts[0])
        return None

    def _video_duration(self, path: Path) -> float | None:
        try:
            result = hidden_run(
                [self.config.ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", os.fspath(path)],
                capture_output=True, text=True, timeout=30, check=True,
            )
            return float(result.stdout.strip())
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            return None

    def _channel_color(self, slug: str) -> str:
        configured = (
            self.config.data.get("announcements", {})
            .get("channels", {}).get(slug, {}).get("background_color")
        )
        if configured:
            return str(configured).removeprefix("0x").removeprefix("#")[-6:]
        palette = ("123B68", "315D9A", "A64B21", "385B3A", "6D397C", "8A2635")
        return palette[int(hashlib.sha256(slug.encode()).hexdigest()[:4], 16) % len(palette)]
from retrotv.processes import hidden_run
