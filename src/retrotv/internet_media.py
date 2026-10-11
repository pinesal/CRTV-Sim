from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from retrotv.classification import resolve_channel_root, slugify
from retrotv.ai_announcements import verified_ssl_context
from retrotv.config import AppConfig
from retrotv.db import transaction, utc_now
from retrotv.normalization import NormalizationAnalyzer
from retrotv.probe import decode_sample, probe_media


SOURCE_SCHEMA_VERSION = 1
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".ts", ".mpg", ".mpeg"}
DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?P<y>19\d{2}|20\d{2})[-_.](?P<m>0?[1-9]|1[0-2])[-_.](?P<d>0?[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(?P<m>0?[1-9]|1[0-2])[-_.](?P<d>0?[1-9]|[12]\d|3[01])[-_.](?P<y>19\d{2}|20\d{2})(?!\d)"),
)


class InternetMediaError(RuntimeError):
    pass


class PreparationCancelled(InternetMediaError):
    pass


@dataclass(frozen=True)
class CatalogEpisode:
    stable_key: str
    remote_url: str
    remote_filename: str
    display_title: str
    episode_date: str | None
    date_source: str | None
    duration_seconds: float | None
    size_bytes: int | None
    checksum_kind: str | None
    checksum_value: str | None
    metadata_status: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_episode_date(value: str) -> tuple[str | None, str | None]:
    found: set[str] = set()
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(value):
            try:
                parsed = date(
                    int(match.group("y")), int(match.group("m")), int(match.group("d"))
                )
            except ValueError:
                continue
            found.add(parsed.isoformat())
    if len(found) == 1:
        return next(iter(found)), "filename"
    return None, None


def _episode_date(item: dict[str, Any], filename: str) -> tuple[str | None, str | None]:
    parsed, source = parse_episode_date(filename)
    if parsed:
        return parsed, source
    title = str(item.get("title") or "")
    parsed, _ = parse_episode_date(title)
    if parsed:
        return parsed, "file_metadata_title"
    raw = str(item.get("date") or "").strip()
    try:
        return date.fromisoformat(raw).isoformat(), "file_metadata_date"
    except ValueError:
        return None, None


def source_type_for(url: str) -> tuple[str, str | None]:
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError as exc:
        raise ValueError("Source URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source must be an HTTP(S) URL")
    host = (parsed.hostname or "").casefold()
    if host == "archive.org" or host.endswith(".archive.org"):
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"details", "download", "metadata"}:
            return "internet_archive", parts[1]
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix.casefold()
    if suffix not in VIDEO_EXTENSIONS:
        raise ValueError(
            "Direct sources must point to a supported video file; general web pages are not supported"
        )
    return "direct_video", None


def _number(value: object, caster):
    try:
        return caster(value)
    except (TypeError, ValueError):
        return None


def _stable_key(identifier: str, logical_name: str) -> str:
    value = f"{identifier}\0{logical_name.casefold()}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _archive_logical_name(filename: str) -> str:
    path = Path(filename)
    stem = path.stem
    if stem.casefold().endswith(".ia"):
        stem = stem[:-3]
    return re.sub(r"[^a-z0-9]+", "-", stem.casefold()).strip("-")


class InternetCatalogClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.user_agent = "CRTV-Sim/0.9 Internet Media"
        # Internet Archive and direct-media discovery must use the same verified
        # Windows-aware certificate chain as the Announcer provider. Portable
        # Python's bundled OpenSSL roots alone can miss a locally installed
        # corporate or security-product certificate authority.
        self.ssl_context = ssl_context or verified_ssl_context()

    def _open_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=self.ssl_context
            ) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise InternetMediaError(f"Unable to read source metadata: {exc}") from exc
        if not isinstance(payload, dict):
            raise InternetMediaError("Source metadata did not contain an object")
        return payload

    def preview(self, url: str, show_title: str) -> dict[str, object]:
        kind, identifier = source_type_for(url)
        if kind == "internet_archive":
            return self._archive_preview(url, str(identifier), show_title)
        return self._direct_preview(url, show_title)

    def _archive_preview(
        self, source_url: str, identifier: str, show_title: str
    ) -> dict[str, object]:
        payload = self._open_json(
            "https://archive.org/metadata/" + urllib.parse.quote(identifier, safe="")
        )
        if payload.get("is_dark") or payload.get("metadata", {}).get("access-restricted-item"):
            raise InternetMediaError("This Internet Archive item is restricted")
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in payload.get("files") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if Path(name).suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            original = str(item.get("original") or name)
            groups.setdefault(_archive_logical_name(original), []).append(item)
        episodes: list[CatalogEpisode] = []
        for logical_name, variants in groups.items():
            # Archive's h.264 derivative is normally the most interoperable choice.
            variants.sort(
                key=lambda item: (
                    str(item.get("format") or "").casefold() != "h.264 ia",
                    not str(item.get("name") or "").casefold().endswith(".ia.mp4"),
                    _number(item.get("size"), int) or 2**63,
                )
            )
            chosen = variants[0]
            filename = str(chosen["name"])
            episode_date, date_source = _episode_date(chosen, filename)
            file_url = (
                "https://archive.org/download/"
                + urllib.parse.quote(identifier, safe="")
                + "/"
                + urllib.parse.quote(filename)
            )
            checksum_kind = "sha1" if chosen.get("sha1") else "md5" if chosen.get("md5") else None
            checksum = chosen.get(checksum_kind) if checksum_kind else None
            episodes.append(
                CatalogEpisode(
                    _stable_key(identifier, logical_name),
                    file_url,
                    filename,
                    str(chosen.get("title") or f"{show_title} — {episode_date or Path(filename).stem}"),
                    episode_date,
                    date_source,
                    _number(chosen.get("length"), float),
                    _number(chosen.get("size"), int),
                    checksum_kind,
                    str(checksum) if checksum else None,
                    "ready" if episode_date else "review",
                )
            )
        episodes.sort(key=lambda item: (item.episode_date or "9999", item.remote_filename.casefold()))
        return {
            "source_type": "internet_archive",
            "identifier": identifier,
            "source_url": source_url,
            "title": payload.get("metadata", {}).get("title") or show_title,
            "episodes": [episode.as_dict() for episode in episodes],
        }

    def _direct_preview(self, url: str, show_title: str) -> dict[str, object]:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": self.user_agent}
        )
        headers = None
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=self.ssl_context
            ) as response:
                headers = response.headers
        except (OSError, urllib.error.URLError):
            # Some file hosts reject HEAD while still allowing GET.
            pass
        filename = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        episode_date, date_source = parse_episode_date(filename)
        size = _number(headers.get("Content-Length"), int) if headers else None
        episode = CatalogEpisode(
            _stable_key(url, filename),
            url,
            filename,
            f"{show_title} — {episode_date or Path(filename).stem}",
            episode_date,
            date_source,
            None,
            size,
            None,
            None,
            "ready" if episode_date else "review",
        )
        return {
            "source_type": "direct_video",
            "identifier": None,
            "source_url": url,
            "title": show_title,
            "episodes": [episode.as_dict()],
        }


def _safe_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", value).strip().rstrip(".")
    return cleaned[:120] or fallback


class InternetMediaService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: AppConfig,
        *,
        client: InternetCatalogClient | None = None,
    ):
        self.connection = connection
        self.config = config
        self.client = client or InternetCatalogClient(
            timeout_seconds=float(
                config.data.get("internet_media", {}).get("request_timeout_seconds", 30)
            )
        )

    def channels(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT c.id,c.slug,c.display_name,c.channel_number,c.enabled,
                   COALESCE(p.playback_mode,'scheduled') playback_mode,
                   COALESCE(p.storage_mode,'keep') storage_mode,
                   COALESCE(p.prepare_ahead,1) prepare_ahead,
                   COALESCE(p.same_date_order_json,'[]') same_date_order_json
            FROM channels c LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id
            ORDER BY c.channel_number IS NULL,c.channel_number,c.display_name
            """
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["same_date_order"] = json.loads(str(item.pop("same_date_order_json")))
            output.append(item)
        return output

    def configure_channel(self, payload: dict[str, Any]) -> dict[str, object]:
        slug = slugify(str(payload.get("slug") or payload.get("display_name") or ""))
        display = str(payload.get("display_name") or "").strip()
        if not slug or not display:
            raise ValueError("Channel name is required")
        mode = str(payload.get("playback_mode", "scheduled"))
        storage = str(payload.get("storage_mode", "keep"))
        if mode not in {"scheduled", "watch_at_your_pace"}:
            raise ValueError("Unknown channel playback mode")
        if storage not in {"keep", "save_space"}:
            raise ValueError("Unknown storage mode")
        if mode == "scheduled":
            storage = "keep"
        prepare_ahead = max(0, min(20, int(payload.get("prepare_ahead", 1))))
        same_date = payload.get("same_date_order", [])
        if not isinstance(same_date, list):
            raise ValueError("Same-date order must be a list of show names")
        channel_number = payload.get("channel_number")
        if channel_number not in {None, ""}:
            channel_number = int(channel_number)
            if channel_number < 1 or channel_number > 999:
                raise ValueError("Channel number must be between 1 and 999")
        now = utc_now()
        with transaction(self.connection):
            self.connection.execute(
                """INSERT INTO channels(slug,display_name,channel_number,enabled)
                   VALUES (?,?,?,1)
                   ON CONFLICT(slug) DO UPDATE SET display_name=excluded.display_name,
                     channel_number=COALESCE(excluded.channel_number,channels.channel_number),enabled=1""",
                (slug, display, channel_number),
            )
            channel_id = int(
                self.connection.execute("SELECT id FROM channels WHERE slug=?", (slug,)).fetchone()[0]
            )
            self.connection.execute(
                """INSERT INTO channel_playback_preferences(
                     channel_id,playback_mode,storage_mode,prepare_ahead,same_date_order_json,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET playback_mode=excluded.playback_mode,
                     storage_mode=excluded.storage_mode,prepare_ahead=excluded.prepare_ahead,
                     same_date_order_json=excluded.same_date_order_json,updated_at=excluded.updated_at""",
                (channel_id, mode, storage, prepare_ahead, json.dumps(same_date), now),
            )
        channel_root = resolve_channel_root(self.config.storage_root("media"), slug)
        (channel_root / "all-day" / "episodes").mkdir(parents=True, exist_ok=True)
        PreparationService(self.connection, self.config).queue_channel(slug)
        return next(item for item in self.channels() if item["slug"] == slug)

    def preview_source(self, url: str, show_title: str) -> dict[str, object]:
        title = show_title.strip()
        if not title:
            raise ValueError("Show name is required")
        return self.client.preview(url.strip(), title)

    def sync_source_files(self) -> dict[str, int]:
        """Register valid source sidecars dropped into ordinary channel folders."""
        media_root = self.config.storage_root("media")
        totals = {"found": 0, "created": 0, "updated": 0, "errors": 0}
        if not media_root.is_dir():
            return totals
        for path in media_root.rglob("*.source.json"):
            totals["found"] += 1
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("Source file is too large")
                document = json.loads(path.read_text(encoding="utf-8-sig"))
                if document.get("schema_version") != SOURCE_SCHEMA_VERSION:
                    raise ValueError("Unsupported source-file schema version")
                relative = path.relative_to(media_root)
                parts = relative.parts
                if len(parts) < 5 or parts[2].casefold() != "episodes":
                    raise ValueError("Source files must be under channel/daypart/episodes/show")
                channel_slug = slugify(parts[0])
                channel = self.connection.execute(
                    "SELECT id FROM channels WHERE slug=?", (channel_slug,)
                ).fetchone()
                if channel is None:
                    self.connection.execute(
                        "INSERT INTO channels(slug,display_name,enabled) VALUES (?,?,1)",
                        (channel_slug, parts[0]),
                    )
                    channel_id = int(self.connection.execute(
                        "SELECT id FROM channels WHERE slug=?", (channel_slug,)
                    ).fetchone()[0])
                    self.connection.execute(
                        """INSERT INTO channel_playback_preferences(
                             channel_id,playback_mode,storage_mode,prepare_ahead,
                             same_date_order_json,updated_at)
                           VALUES (?,'scheduled','keep',1,'[]',?)""",
                        (channel_id, utc_now()),
                    )
                else:
                    channel_id = int(channel[0])
                source_key = str(document.get("source_key") or "").strip()
                show_title = str(document.get("show_title") or parts[3]).strip()
                url = str(document.get("url") or "").strip()
                enabled = int(bool(document.get("enabled", True)))
                if not source_key or not show_title or not url:
                    raise ValueError("Source file requires source_key, show_title, and url")
                preview = self.preview_source(url, show_title)
                existing = self.connection.execute(
                    "SELECT id FROM internet_sources WHERE source_key=?", (source_key,)
                ).fetchone()
                now = utc_now()
                if existing is None:
                    cursor = self.connection.execute(
                        """INSERT INTO internet_sources(
                             source_key,channel_id,source_type,url,show_title,daypart_slug,
                             source_relative_path,enabled,created_at,refreshed_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            source_key, channel_id, preview["source_type"], url, show_title,
                            parts[1].casefold(), relative.as_posix(), enabled, now, now,
                        ),
                    )
                    source_id = int(cursor.lastrowid)
                    totals["created"] += 1
                else:
                    source_id = int(existing[0])
                    self.connection.execute(
                        """UPDATE internet_sources SET channel_id=?,source_type=?,url=?,show_title=?,
                             daypart_slug=?,source_relative_path=?,enabled=?,refreshed_at=?,refresh_error=NULL
                           WHERE id=?""",
                        (
                            channel_id, preview["source_type"], url, show_title,
                            parts[1].casefold(), relative.as_posix(), enabled, now, source_id,
                        ),
                    )
                    totals["updated"] += 1
                selected = document.get("selected_keys", [])
                if not isinstance(selected, list):
                    raise ValueError("selected_keys must be a list")
                self._upsert_episodes(source_id, preview["episodes"], {str(v) for v in selected})
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                totals["errors"] += 1
        return totals

    def add_source(self, payload: dict[str, Any]) -> dict[str, object]:
        channel_slug = slugify(str(payload.get("channel") or ""))
        row = self.connection.execute(
            "SELECT id FROM channels WHERE slug=? AND enabled=1", (channel_slug,)
        ).fetchone()
        if row is None:
            raise ValueError("Select an existing enabled channel")
        channel_id = int(row[0])
        preferences = self.connection.execute(
            "SELECT playback_mode FROM channel_playback_preferences WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        mode = str(preferences[0]) if preferences else "scheduled"
        if preferences is None:
            self.connection.execute(
                """INSERT INTO channel_playback_preferences(
                     channel_id,playback_mode,storage_mode,prepare_ahead,same_date_order_json,updated_at)
                   VALUES (?,'scheduled','keep',1,'[]',?)""",
                (channel_id, utc_now()),
            )
            self.connection.commit()
        show_title = str(payload.get("show_title") or "").strip()
        preview = self.preview_source(str(payload.get("url") or ""), show_title)
        daypart = str(payload.get("daypart_slug") or "all-day").strip().casefold()
        if mode == "watch_at_your_pace":
            daypart = "all-day"
        allowed_dayparts = set(self.config.data.get("library", {}).get("dayparts", [])) | {"all-day"}
        if daypart not in allowed_dayparts:
            raise ValueError("Unknown daypart")
        source_key = str(payload.get("source_key") or uuid.uuid4())
        show_folder = _safe_component(show_title, "Internet Show")
        channel_root = resolve_channel_root(self.config.storage_root("media"), channel_slug)
        relative = Path(channel_root.name) / daypart / "episodes" / show_folder / f"{source_key}.source.json"
        absolute = self.config.storage_root("media") / relative
        selected_keys = payload.get("selected_keys", [])
        if not isinstance(selected_keys, list):
            raise ValueError("Selected episodes must be a list")
        selected = {str(value) for value in selected_keys}
        source_document = {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "source_key": source_key,
            "url": preview["source_url"],
            "source_type": preview["source_type"],
            "show_title": show_title,
            "enabled": True,
            "selected_keys": sorted(selected),
        }
        absolute.parent.mkdir(parents=True, exist_ok=True)
        temporary = absolute.with_name(f".{absolute.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(source_document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, absolute)
        now = utc_now()
        try:
            with transaction(self.connection):
                cursor = self.connection.execute(
                    """INSERT INTO internet_sources(
                         source_key,channel_id,source_type,url,show_title,daypart_slug,
                         source_relative_path,enabled,created_at,refreshed_at)
                       VALUES (?,?,?,?,?,?,?,1,?,?)""",
                    (
                        source_key,
                        channel_id,
                        preview["source_type"],
                        preview["source_url"],
                        show_title,
                        daypart,
                        relative.as_posix(),
                        now,
                        now,
                    ),
                )
                source_id = int(cursor.lastrowid)
                self._upsert_episodes(source_id, preview["episodes"], selected)
        except Exception:
            absolute.unlink(missing_ok=True)
            raise
        return self.source_detail(source_id)

    def _upsert_episodes(
        self,
        source_id: int,
        episodes: Iterable[dict[str, Any]],
        selected_keys: set[str] | None = None,
    ) -> None:
        now = utc_now()
        incoming: set[str] = set()
        current_date_row = self.connection.execute(
            """SELECT current.episode_date
               FROM internet_sources source
               JOIN watch_progress progress ON progress.channel_id=source.channel_id
               JOIN remote_episodes current ON current.id=progress.current_episode_id
               WHERE source.id=?""",
            (source_id,),
        ).fetchone()
        current_date = str(current_date_row[0]) if current_date_row and current_date_row[0] else None
        for episode in episodes:
            key = str(episode["stable_key"])
            incoming.add(key)
            selected = None if selected_keys is None else int(key in selected_keys)
            existing = self.connection.execute(
                """SELECT id,selected,episode_date,metadata_status,checksum_value,
                          size_bytes,local_path,availability_status
                   FROM remote_episodes WHERE source_id=? AND stable_key=?""",
                (source_id, key),
            ).fetchone()
            if existing is None and episode.get("episode_date"):
                candidates = self.connection.execute(
                    """SELECT id FROM remote_episodes
                       WHERE source_id=? AND episode_date=? AND metadata_status!='excluded'""",
                    (source_id, episode["episode_date"]),
                ).fetchall()
                if len(candidates) == 1:
                    existing = self.connection.execute(
                        """SELECT id,selected,episode_date,metadata_status,checksum_value,
                                  size_bytes,local_path,availability_status
                           FROM remote_episodes WHERE id=?""",
                        (int(candidates[0][0]),),
                    ).fetchone()
                    self.connection.execute(
                        "UPDATE remote_episodes SET stable_key=? WHERE id=?", (key, int(existing["id"]))
                    )
            values = (
                str(episode["remote_url"]),
                str(episode["remote_filename"]),
                str(episode["display_title"]),
                episode.get("episode_date"),
                episode.get("date_source"),
                episode.get("duration_seconds"),
                episode.get("size_bytes"),
                episode.get("checksum_kind"),
                episode.get("checksum_value"),
                str(episode.get("metadata_status") or "review"),
                now,
            )
            if existing is None:
                catchup = (
                    "review"
                    if selected_keys is None
                    and current_date
                    and episode.get("episode_date")
                    and str(episode["episode_date"]) < current_date
                    else "none"
                )
                self.connection.execute(
                    """INSERT INTO remote_episodes(
                         source_id,stable_key,remote_url,remote_filename,display_title,
                         episode_date,date_source,duration_seconds,size_bytes,checksum_kind,
                         checksum_value,metadata_status,selected,catchup_status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_id, key, *values[:-1], selected or 0, catchup, now, now),
                )
            else:
                episode_id = int(existing["id"])
                changed = bool(
                    existing["local_path"]
                    and (
                        (
                            existing["checksum_value"]
                            and episode.get("checksum_value")
                            and str(existing["checksum_value"]).casefold()
                            != str(episode["checksum_value"]).casefold()
                        )
                        or (
                            existing["size_bytes"]
                            and episode.get("size_bytes")
                            and int(existing["size_bytes"]) != int(episode["size_bytes"])
                        )
                    )
                )
                self.connection.execute(
                    """UPDATE remote_episodes SET remote_url=?,remote_filename=?,display_title=?,
                         episode_date=COALESCE(CASE WHEN date_source IN ('manual','manual_excluded') THEN episode_date END,?),
                         date_source=CASE WHEN date_source IN ('manual','manual_excluded') THEN date_source ELSE ? END,
                         duration_seconds=COALESCE(?,duration_seconds),size_bytes=COALESCE(?,size_bytes),
                         checksum_kind=?,checksum_value=?,
                         metadata_status=CASE WHEN date_source IN ('manual','manual_excluded') THEN metadata_status ELSE ? END,
                         selected=COALESCE(?,selected),availability_status=?,updated_at=?
                       WHERE id=?""",
                    (*values[:-1], selected, "changed" if changed else "available", now, episode_id),
                )
        if incoming:
            placeholders = ",".join("?" for _ in incoming)
            self.connection.execute(
                f"UPDATE remote_episodes SET availability_status='missing',updated_at=? WHERE source_id=? AND stable_key NOT IN ({placeholders})",
                (now, source_id, *sorted(incoming)),
            )
        else:
            self.connection.execute(
                "UPDATE remote_episodes SET availability_status='missing',updated_at=? WHERE source_id=?",
                (now, source_id),
            )

    def source_detail(self, source_id: int) -> dict[str, object]:
        source = self.connection.execute(
            """SELECT s.*,c.slug channel_slug,c.display_name channel_name
               FROM internet_sources s JOIN channels c ON c.id=s.channel_id WHERE s.id=?""",
            (source_id,),
        ).fetchone()
        if source is None:
            raise ValueError("Internet source not found")
        episodes = [
            dict(row)
            for row in self.connection.execute(
                """SELECT e.*,COALESCE(j.state,'cataloged') preparation_state,
                          j.bytes_downloaded,j.total_bytes,j.error_detail
                   FROM remote_episodes e LEFT JOIN internet_preparation_jobs j ON j.episode_id=e.id
                   WHERE e.source_id=? ORDER BY e.episode_date IS NULL,e.episode_date,e.manual_order,e.id""",
                (source_id,),
            )
        ]
        result = dict(source)
        result["enabled"] = bool(result["enabled"])
        result["episodes"] = episodes
        return result

    def overview(self) -> dict[str, object]:
        channels = self.channels()
        sources = [
            self.source_detail(int(row[0]))
            for row in self.connection.execute("SELECT id FROM internet_sources ORDER BY id")
        ]
        playlists: list[dict[str, object]] = []
        progress_service = WatchProgressService(self.connection, self.config)
        for channel in channels:
            if channel["playback_mode"] != "watch_at_your_pace":
                continue
            try:
                playlists.append(
                    {
                        "channel": channel["slug"],
                        "display_name": channel["display_name"],
                        "status": progress_service.status(str(channel["slug"])),
                        "episodes": progress_service.playlist(str(channel["slug"])),
                    }
                )
            except ValueError:
                continue
        return {
            "channels": channels,
            "sources": sources,
            "playlists": playlists,
            "cache": PreparationService(self.connection, self.config).cache_status(),
        }

    def refresh_source(self, source_id: int) -> dict[str, object]:
        source = self.connection.execute(
            "SELECT url,show_title FROM internet_sources WHERE id=?", (source_id,)
        ).fetchone()
        if source is None:
            raise ValueError("Internet source not found")
        try:
            preview = self.preview_source(str(source["url"]), str(source["show_title"]))
            with transaction(self.connection):
                self._upsert_episodes(source_id, preview["episodes"])
                self.connection.execute(
                    "UPDATE internet_sources SET refreshed_at=?,refresh_error=NULL WHERE id=?",
                    (utc_now(), source_id),
                )
        except Exception as exc:
            self.connection.execute(
                "UPDATE internet_sources SET refresh_error=? WHERE id=?", (str(exc), source_id)
            )
            self.connection.commit()
            raise
        return self.source_detail(source_id)

    def update_episode(self, episode_id: int, payload: dict[str, Any]) -> dict[str, object]:
        row = self.connection.execute("SELECT * FROM remote_episodes WHERE id=?", (episode_id,)).fetchone()
        if row is None:
            raise ValueError("Episode not found")
        selected = int(bool(payload.get("selected", row["selected"])))
        excluded = bool(payload.get("excluded", False))
        raw_date = payload.get("episode_date", row["episode_date"])
        episode_date = None
        if raw_date:
            try:
                episode_date = date.fromisoformat(str(raw_date)).isoformat()
            except ValueError as exc:
                raise ValueError("Episode date must use YYYY-MM-DD") from exc
        status = "excluded" if excluded else "ready" if episode_date else "review"
        catchup_action = str(payload.get("catchup_action") or "").casefold()
        catchup_status = str(row["catchup_status"])
        if catchup_action in {"accepted", "excluded"}:
            catchup_status = catchup_action
            selected = int(catchup_action == "accepted" and not excluded)
        self.connection.execute(
            """UPDATE remote_episodes SET selected=?,episode_date=?,date_source=?,
                 metadata_status=?,manual_order=?,catchup_status=?,updated_at=? WHERE id=?""",
            (
                selected,
                episode_date,
                "manual" if episode_date else "manual_excluded" if excluded else None,
                status,
                int(payload.get("manual_order", row["manual_order"])),
                catchup_status,
                utc_now(),
                episode_id,
            ),
        )
        self.connection.commit()
        source = self.connection.execute(
            """SELECT e.source_id,c.slug channel_slug FROM remote_episodes e
               JOIN internet_sources s ON s.id=e.source_id
               JOIN channels c ON c.id=s.channel_id WHERE e.id=?""",
            (episode_id,),
        ).fetchone()
        if source:
            self._write_source_selection(int(source["source_id"]))
            PreparationService(self.connection, self.config).queue_channel(
                str(source["channel_slug"])
            )
        return dict(self.connection.execute("SELECT * FROM remote_episodes WHERE id=?", (episode_id,)).fetchone())

    def _write_source_selection(self, source_id: int) -> None:
        source = self.connection.execute(
            "SELECT source_key,url,source_type,show_title,source_relative_path FROM internet_sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if source is None:
            return
        path = self.config.storage_root("media") / Path(source["source_relative_path"])
        if not path.is_file():
            return
        keys = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT stable_key FROM remote_episodes WHERE source_id=? AND selected=1 ORDER BY id",
                (source_id,),
            )
        ]
        document = {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "source_key": source["source_key"],
            "url": source["url"],
            "source_type": source["source_type"],
            "show_title": source["show_title"],
            "enabled": bool(
                self.connection.execute(
                    "SELECT enabled FROM internet_sources WHERE id=?", (source_id,)
                ).fetchone()[0]
            ),
            "selected_keys": keys,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    def disable_source(self, source_id: int) -> None:
        row = self.connection.execute(
            """SELECT s.source_relative_path,c.slug channel_slug FROM internet_sources s
               JOIN channels c ON c.id=s.channel_id WHERE s.id=?""",
            (source_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Internet source not found")
        self.connection.execute("UPDATE internet_sources SET enabled=0 WHERE id=?", (source_id,))
        self.connection.execute(
            """UPDATE internet_preparation_jobs SET
                 cancel_requested=CASE WHEN state IN ('downloading','validating','analyzing','normalizing') THEN 1 ELSE 0 END,
                 state=CASE WHEN state IN ('queued','retry','paused_space') THEN 'cataloged' ELSE state END,
                 retry_after=NULL,updated_at=?
               WHERE episode_id IN (SELECT id FROM remote_episodes WHERE source_id=?)
                 AND state NOT IN ('ready','completed','awaiting_scan')""",
            (utc_now(), source_id),
        )
        self.connection.commit()
        path = self.config.storage_root("media") / Path(str(row["source_relative_path"]))
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            document["enabled"] = False
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(temporary, path)
        PreparationService(self.connection, self.config).queue_channel(
            str(row["channel_slug"])
        )

    def repair_episode(self, episode_id: int) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT source_id FROM remote_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Episode not found")
        source_id = int(row[0])
        self.refresh_source(source_id)
        episode = self.connection.execute(
            "SELECT availability_status FROM remote_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if episode is None or episode["availability_status"] == "missing":
            raise ValueError("The remote episode is still missing")
        now = utc_now()
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE remote_episodes SET availability_status='available',updated_at=? WHERE id=?",
                (now, episode_id),
            )
            self.connection.execute(
                """INSERT INTO internet_preparation_jobs(episode_id,state,priority,created_at,updated_at)
                   VALUES (?,'queued',0,?,?)
                   ON CONFLICT(episode_id) DO UPDATE SET state='queued',priority=0,
                      cancel_requested=0,retry_after=NULL,error_detail=NULL,
                      completed_at=NULL,updated_at=excluded.updated_at""",
                (episode_id, now, now),
            )
        return self.source_detail(source_id)

    def set_watching_position(
        self,
        channel_slug: str,
        episode_id: int | None = None,
        position_seconds: float = 0,
        *,
        start_date: str | None = None,
        start_at_beginning: bool = False,
    ) -> dict[str, object]:
        progress = WatchProgressService(self.connection, self.config)
        playlist = progress.playlist(channel_slug)
        if episode_id is None:
            if start_at_beginning:
                if not playlist:
                    raise ValueError("The channel has no selected episodes")
                episode_id = int(playlist[0]["id"])
            elif not start_date:
                raise ValueError("Choose a starting date or an exact episode")
            else:
                try:
                    requested_date = date.fromisoformat(start_date).isoformat()
                except ValueError as exc:
                    raise ValueError("Starting date must use YYYY-MM-DD") from exc
                match = next(
                    (item for item in playlist if str(item["episode_date"]) >= requested_date),
                    None,
                )
                if match is None:
                    raise ValueError("No selected episode exists on or after that date")
                episode_id = int(match["id"])
        row = self.connection.execute(
            """SELECT c.id,e.duration_seconds FROM channels c
               JOIN internet_sources s ON s.channel_id=c.id
               JOIN remote_episodes e ON e.source_id=s.id
               WHERE c.slug=? AND e.id=? AND e.selected=1 AND e.metadata_status='ready'""",
            (slugify(channel_slug), episode_id),
        ).fetchone()
        if row is None:
            raise ValueError("Selected episode does not belong to this channel")
        channel_id = int(row[0])
        seconds = max(0.0, float(position_seconds))
        if row["duration_seconds"] is not None:
            seconds = min(seconds, max(0.0, float(row["duration_seconds"]) - 0.25))
        self.connection.execute(
            """INSERT INTO watch_progress(
                   channel_id,current_episode_id,position_seconds,state,updated_at,position_confirmed)
               VALUES (?,?,?,'in_progress',?,1)
               ON CONFLICT(channel_id) DO UPDATE SET current_episode_id=excluded.current_episode_id,
                 position_seconds=excluded.position_seconds,state='in_progress',updated_at=excluded.updated_at,
                 position_confirmed=1""",
            (channel_id, episode_id, seconds, utc_now()),
        )
        self.connection.commit()
        PreparationService(self.connection, self.config).queue_channel(channel_slug, priority_base=0)
        return progress.status(channel_slug)


class WatchProgressService:
    def __init__(self, connection: sqlite3.Connection, config: AppConfig):
        self.connection = connection
        self.config = config

    def _channel(self, channel_slug: str):
        row = self.connection.execute(
            """SELECT c.id,c.slug,c.display_name,p.playback_mode,p.storage_mode,p.prepare_ahead,
                      p.same_date_order_json
               FROM channels c JOIN channel_playback_preferences p ON p.channel_id=c.id
               WHERE c.slug=? AND c.enabled=1""",
            (slugify(channel_slug),),
        ).fetchone()
        if row is None or row["playback_mode"] != "watch_at_your_pace":
            raise ValueError("Channel is not configured for Watch at Your Pace")
        return row

    def playlist(self, channel_slug: str) -> list[dict[str, object]]:
        channel = self._channel(channel_slug)
        priorities = {
            str(value).casefold(): index
            for index, value in enumerate(json.loads(channel["same_date_order_json"] or "[]"))
        }
        rows = self.connection.execute(
            """SELECT e.*,s.show_title,COALESCE(j.state,'cataloged') preparation_state,
                      j.error_detail
               FROM remote_episodes e JOIN internet_sources s ON s.id=e.source_id
               LEFT JOIN internet_preparation_jobs j ON j.episode_id=e.id
               WHERE s.channel_id=? AND s.enabled=1 AND e.selected=1
                 AND e.metadata_status='ready' AND e.availability_status='available'
               ORDER BY e.episode_date,e.manual_order,e.id""",
            (int(channel["id"]),),
        ).fetchall()
        result = [dict(row) for row in rows]
        result.sort(
            key=lambda item: (
                str(item["episode_date"]),
                priorities.get(str(item["show_title"]).casefold(), len(priorities)),
                int(item["manual_order"]),
                int(item["id"]),
            )
        )
        return result

    def _ensure_progress(self, channel_slug: str):
        channel = self._channel(channel_slug)
        row = self.connection.execute(
            "SELECT * FROM watch_progress WHERE channel_id=?", (int(channel["id"]),)
        ).fetchone()
        if row is None:
            playlist = self.playlist(channel_slug)
            episode_id = int(playlist[0]["id"]) if playlist else None
            state = "not_started" if episode_id is not None else "caught_up"
            self.connection.execute(
                """INSERT INTO watch_progress(
                       channel_id,current_episode_id,position_seconds,state,updated_at,position_confirmed)
                   VALUES (?,?,0,?,?,0)""",
                (int(channel["id"]), episode_id, state, utc_now()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM watch_progress WHERE channel_id=?", (int(channel["id"]),)
            ).fetchone()
        return channel, row

    def status(self, channel_slug: str) -> dict[str, object]:
        channel, progress = self._ensure_progress(channel_slug)
        playlist = self.playlist(channel_slug)
        current_id = progress["current_episode_id"]
        index = next((i for i, item in enumerate(playlist) if item["id"] == current_id), None)
        current = playlist[index] if index is not None else None
        upcoming = playlist[index + 1] if index is not None and index + 1 < len(playlist) else None
        return {
            "channel": channel["slug"],
            "state": progress["state"],
            "position_seconds": float(progress["position_seconds"]),
            "position_confirmed": bool(progress["position_confirmed"]),
            "current": current,
            "up_next": upcoming,
            "playlist_count": len(playlist),
        }

    def save_position(self, channel_slug: str, episode_id: int, seconds: float) -> None:
        channel = self._channel(channel_slug)
        self.connection.execute(
            """UPDATE watch_progress SET position_seconds=?,state='in_progress',updated_at=?
               WHERE channel_id=? AND current_episode_id=?""",
            (max(0.0, float(seconds)), utc_now(), int(channel["id"]), episode_id),
        )
        self.connection.commit()

    def complete(self, channel_slug: str, episode_id: int) -> dict[str, object]:
        channel, progress = self._ensure_progress(channel_slug)
        if progress["current_episode_id"] != episode_id:
            raise ValueError("Episode is no longer the channel's current episode")
        playlist = self.playlist(channel_slug)
        index = next((i for i, item in enumerate(playlist) if item["id"] == episode_id), None)
        if index is None:
            raise ValueError("Episode is no longer in the playlist")
        next_id = int(playlist[index + 1]["id"]) if index + 1 < len(playlist) else None
        now = utc_now()
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE remote_episodes SET completed_at=?,updated_at=? WHERE id=?",
                (now, now, episode_id),
            )
            self.connection.execute(
                """UPDATE watch_progress SET current_episode_id=?,position_seconds=0,
                     state=?,updated_at=? WHERE channel_id=?""",
                (next_id, "in_progress" if next_id else "caught_up", now, int(channel["id"])),
            )
            self.connection.execute(
                "UPDATE internet_preparation_jobs SET state='completed',completed_at=?,updated_at=? WHERE episode_id=?",
                (now, now, episode_id),
            )
        PreparationService(self.connection, self.config).queue_channel(channel_slug, priority_base=0)
        return self.status(channel_slug)


class PreparationService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: AppConfig,
        *,
        stop_event: threading.Event | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.connection = connection
        self.config = config
        self.settings = config.data.get("internet_media", {})
        self.stop_event = stop_event
        # Reuse the discovery trust policy for the file download itself, so a
        # catalog that previews successfully can also be prepared on this PC.
        self.ssl_context = ssl_context or verified_ssl_context()

    @property
    def cache_root(self) -> Path:
        raw = str(self.settings.get("cache_directory", "internet-cache"))
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = self.config.storage_root("state") / path
        return path.resolve()

    def recover_interrupted(self) -> int:
        now = utc_now()
        cancelled = self.connection.execute(
            """UPDATE internet_preparation_jobs SET state='cataloged',retry_after=NULL,
                    error_detail='Preparation was cancelled',cancel_requested=0,
                    bytes_downloaded=0,total_bytes=NULL,temporary_path=NULL,final_path=NULL,
                    updated_at=?
               WHERE cancel_requested=1""",
            (now,),
        )
        first = self.connection.execute(
            """UPDATE internet_preparation_jobs SET state='retry',retry_after=NULL,
                    error_detail='Preparation was interrupted; resuming safely',updated_at=?
               WHERE state IN ('downloading','validating','analyzing')""",
            (now,),
        )
        second = self.connection.execute(
            """UPDATE internet_preparation_jobs SET state='retry',retry_after=NULL,
                    error_detail='Compatibility conversion was interrupted; restarting',updated_at=?
               WHERE state='normalizing' AND episode_id IN (
                   SELECT id FROM remote_episodes WHERE retained=0
               )""",
            (now,),
        )
        self.connection.commit()
        return max(0, cancelled.rowcount) + max(0, first.rowcount) + max(0, second.rowcount)

    def cache_status(self) -> dict[str, object]:
        root = self.cache_root
        size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.is_dir() else 0
        usage = shutil.disk_usage(root if root.exists() else root.parent)
        return {
            "directory": str(root),
            "size_bytes": size,
            "limit_bytes": int(float(self.settings.get("cache_limit_gib", 50)) * 1024**3),
            "free_bytes": usage.free,
            "reserve_bytes": int(float(self.settings.get("minimum_free_gib", 5)) * 1024**3),
        }

    def _ordered_episodes(self, channel) -> list[dict[str, object]]:
        priorities = {
            str(value).casefold(): index
            for index, value in enumerate(
                json.loads(str(channel["same_date_order_json"] or "[]"))
            )
        }
        rows = [
            dict(row)
            for row in self.connection.execute(
                """SELECT e.*,s.show_title,COALESCE(j.state,'cataloged') preparation_state
                   FROM remote_episodes e JOIN internet_sources s ON s.id=e.source_id
                   LEFT JOIN internet_preparation_jobs j ON j.episode_id=e.id
                   WHERE s.channel_id=? AND s.enabled=1 AND e.selected=1
                     AND e.metadata_status='ready' AND e.availability_status='available'
                   ORDER BY e.episode_date,e.manual_order,e.id""",
                (int(channel["id"]),),
            )
        ]
        rows.sort(
            key=lambda item: (
                str(item["episode_date"]),
                priorities.get(str(item["show_title"]).casefold(), len(priorities)),
                int(item["manual_order"]),
                int(item["id"]),
            )
        )
        return rows

    def _mark_scheduled_airtimes(self, channel_id: int) -> int:
        now = datetime.now(UTC)
        aired: set[int] = set()
        rows = self.connection.execute(
            """SELECT e.id,se.ends_at FROM remote_episodes e
               JOIN internet_sources s ON s.id=e.source_id
               JOIN schedule_entries se ON se.asset_id=e.asset_id
               WHERE s.channel_id=? AND e.scheduled_aired_at IS NULL
                 AND e.asset_id IS NOT NULL""",
            (channel_id,),
        ).fetchall()
        for row in rows:
            try:
                ends_at = datetime.fromisoformat(str(row["ends_at"]))
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=UTC)
                if ends_at.astimezone(UTC) <= now:
                    aired.add(int(row["id"]))
            except ValueError:
                continue
        if aired:
            placeholders = ",".join("?" for _ in aired)
            self.connection.execute(
                f"""UPDATE remote_episodes SET scheduled_aired_at=COALESCE(scheduled_aired_at,?),
                       updated_at=? WHERE id IN ({placeholders})""",
                (utc_now(), utc_now(), *sorted(aired)),
            )
            self.connection.commit()
        return len(aired)

    def _is_managed_incomplete(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        if not (
            resolved.name.endswith(".partial")
            or resolved.name.endswith(".compat.mp4")
        ):
            return False
        for root in (self.cache_root, self.config.storage_root("media").resolve()):
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _delete_incomplete(self, raw: str | None) -> None:
        if not raw:
            return
        path = Path(raw)
        if self._is_managed_incomplete(path):
            path.unlink(missing_ok=True)

    def queue_channel(self, channel_slug: str, *, priority_base: int = 100) -> int:
        channel = self.connection.execute(
            """SELECT c.id,p.playback_mode,p.prepare_ahead,p.same_date_order_json FROM channels c
               JOIN channel_playback_preferences p ON p.channel_id=c.id WHERE c.slug=?""",
            (slugify(channel_slug),),
        ).fetchone()
        if channel is None:
            return 0
        ordered = self._ordered_episodes(channel)
        if channel["playback_mode"] == "watch_at_your_pace":
            progress = WatchProgressService(self.connection, self.config)
            status = progress.status(channel_slug)
            if not status["position_confirmed"]:
                selected = []
            else:
                current_id = status["current"]["id"] if status["current"] else None
                start = next(
                    (i for i, item in enumerate(ordered) if item["id"] == current_id),
                    len(ordered),
                )
                selected = ordered[start : start + int(channel["prepare_ahead"]) + 1]
        else:
            self._mark_scheduled_airtimes(int(channel["id"]))
            ordered = self._ordered_episodes(channel)
            remaining = [item for item in ordered if not item["scheduled_aired_at"]]
            selected = remaining[: int(channel["prepare_ahead"]) + 1]
        required = {int(item["id"]) for item in selected}
        now = utc_now()
        obsolete = self.connection.execute(
            """SELECT j.id,j.episode_id,j.state,j.temporary_path FROM internet_preparation_jobs j
               JOIN remote_episodes e ON e.id=j.episode_id
               JOIN internet_sources s ON s.id=e.source_id
               WHERE s.channel_id=?""",
            (int(channel["id"]),),
        ).fetchall()
        for job in obsolete:
            episode_id = int(job["episode_id"])
            if episode_id in required or job["state"] in {"ready", "completed", "awaiting_scan"}:
                continue
            if job["state"] in {"downloading", "validating", "analyzing", "normalizing"}:
                self.connection.execute(
                    "UPDATE internet_preparation_jobs SET cancel_requested=1,updated_at=? WHERE id=?",
                    (now, int(job["id"])),
                )
            elif job["state"] in {"queued", "retry", "paused_space", "failed"}:
                self._delete_incomplete(job["temporary_path"])
                self.connection.execute(
                    """UPDATE internet_preparation_jobs SET state='cataloged',cancel_requested=0,
                         retry_after=NULL,error_detail=NULL,bytes_downloaded=0,total_bytes=NULL,
                         temporary_path=NULL,final_path=NULL,updated_at=? WHERE id=?""",
                    (now, int(job["id"])),
                )
        queued = 0
        for index, episode in enumerate(selected):
            cursor = self.connection.execute(
                """INSERT INTO internet_preparation_jobs(
                     episode_id,state,priority,created_at,updated_at)
                   VALUES (?,'queued',?,?,?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                       state=CASE WHEN state IN ('cataloged','retry','paused_space') THEN 'queued' ELSE state END,
                      cancel_requested=0,priority=MIN(priority,excluded.priority),
                      updated_at=excluded.updated_at""",
                (int(episode["id"]), priority_base + index, now, now),
            )
            queued += max(0, cursor.rowcount)
        self.connection.commit()
        return queued

    def queue_all(self, tuned_channel: str | None = None) -> int:
        total = 0
        for row in self.connection.execute("SELECT slug FROM channels WHERE enabled=1"):
            try:
                slug = str(row["slug"])
                total += self.queue_channel(
                    slug,
                    priority_base=0 if tuned_channel and slug == tuned_channel else 100,
                )
            except ValueError:
                continue
        return total

    def reconcile_retained(self) -> int:
        """Mark retained downloads ready only after normal library processing."""
        media_root = self.config.storage_root("media").resolve()
        changed = 0
        rows = self.connection.execute(
            """SELECT j.id job_id,e.id episode_id,e.local_path
               FROM internet_preparation_jobs j JOIN remote_episodes e ON e.id=j.episode_id
               WHERE j.state IN ('awaiting_scan','normalizing') AND e.retained=1"""
        ).fetchall()
        for row in rows:
            path = Path(str(row["local_path"] or "")).resolve()
            try:
                relative = path.relative_to(media_root).as_posix()
            except ValueError:
                self._job(int(row["job_id"]), "failed", error="Retained path left the media root")
                changed += 1
                continue
            asset = self.connection.execute(
                """SELECT a.*,n.analysis_status,n.proxy_status,n.detected_mode,n.proposed_mode,
                          m.width,m.height,m.duration_seconds,m.display_aspect_ratio,m.sample_aspect_ratio
                   FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
                   LEFT JOIN normalization_profiles n ON n.asset_id=a.id
                   WHERE a.source_relative_path=?""",
                (relative,),
            ).fetchone()
            if asset is None:
                continue
            self.connection.execute(
                "UPDATE remote_episodes SET asset_id=?,updated_at=? WHERE id=?",
                (int(asset["id"]), utc_now(), int(row["episode_id"])),
            )
            self.connection.execute(
                "UPDATE assets SET enabled_for_scheduling=0 WHERE id=?",
                (int(asset["id"]),),
            )
            self.connection.commit()
            if asset["review_status"] != "ready" or asset["integrity_status"] != "ready":
                self._job(int(row["job_id"]), "review", error="Downloaded episode needs library review")
                changed += 1
                continue
            from retrotv.processing import enabled
            video_on = enabled(self.config, "normalization")
            if video_on and asset["analysis_status"] in {None, "pending", "analyzing", "error"}:
                continue
            if video_on and asset["analysis_status"] == "review":
                self._job(int(row["job_id"]), "review", error="Downloaded episode needs aspect review")
                changed += 1
                continue
            from retrotv.normalization import NormalizationService

            normalizer = NormalizationService(self.connection, self.config)
            needs_conversion = normalizer.requires_conversion(asset)
            proxy_ready = (self.config.data.get("normalization", {}).get("output_policy") == "proxy" and asset["proxy_status"] == "ready")
            if needs_conversion and asset["proxy_status"] != "promoted" and not proxy_ready:
                self._job(int(row["job_id"]), "normalizing")
                continue
            self.connection.execute(
                "UPDATE assets SET enabled_for_scheduling=1 WHERE id=?",
                (int(asset["id"]),),
            )
            self.connection.commit()
            self._job(int(row["job_id"]), "ready", completed=True)
            changed += 1
        return changed

    def _target_paths(self, row) -> tuple[Path, Path, bool]:
        keep = row["playback_mode"] == "scheduled" or row["storage_mode"] == "keep"
        filename = _safe_component(str(row["remote_filename"]), f"episode-{row['episode_id']}.mp4")
        if Path(filename).suffix.casefold() not in VIDEO_EXTENSIONS:
            filename += ".mp4"
        if keep:
            source_file = Path(str(row["source_relative_path"]))
            final = self.config.storage_root("media") / source_file.parent / filename
        else:
            final = self.cache_root / str(row["channel_slug"]) / f"{int(row['episode_id']):08d}-{filename}"
        temporary = final.with_name(final.name + ".partial")
        return temporary, final, keep

    def _probe_and_decode(self, path: Path):
        probe = probe_media(
            path,
            ffprobe=self.config.ffprobe,
            timeout_seconds=float(self.settings.get("probe_timeout_seconds", 60)),
        )
        if not probe.ok:
            raise InternetMediaError(probe.error_detail or "Downloaded video failed probing")
        sample = decode_sample(
            path,
            ffmpeg=self.config.ffmpeg,
            start_seconds=0,
            sample_seconds=2,
            timeout_seconds=float(self.settings.get("decode_timeout_seconds", 60)),
        )
        if not sample.ok:
            raise InternetMediaError(sample.error_detail or "Downloaded video failed decoding")
        return probe

    def _cancel_requested(self, job_id: int) -> bool:
        row = self.connection.execute(
            "SELECT cancel_requested FROM internet_preparation_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        return row is None or bool(row[0])

    def _check_cancelled(self, job_id: int) -> None:
        if self._cancel_requested(job_id):
            raise PreparationCancelled("Preparation cancelled")

    def _compatibility_convert(self, source: Path, destination: Path) -> None:
        command = [
            self.config.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + float(
            self.config.data.get("normalization", {}).get("encode_timeout_seconds", 21600)
        )
        try:
            while process.poll() is None:
                if self.stop_event is not None and self.stop_event.wait(0.2):
                    process.terminate()
                    raise InternetMediaError("Preparation interrupted by shutdown")
                if getattr(self, "_active_job_id", None) is not None:
                    self._check_cancelled(int(self._active_job_id))
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise InternetMediaError("Compatibility conversion timed out")
                time.sleep(0.2)
            _stdout, stderr = process.communicate()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            destination.unlink(missing_ok=True)
            raise
        if process.returncode != 0 or not destination.is_file():
            destination.unlink(missing_ok=True)
            raise InternetMediaError(
                "Compatibility conversion failed: " + (stderr or "unknown ffmpeg error")[-1600:]
            )

    def run_next(self) -> dict[str, object] | None:
        row = self.connection.execute(
            """SELECT j.id job_id,j.episode_id,j.retry_count,e.*,s.source_relative_path,
                      c.slug channel_slug,p.playback_mode,p.storage_mode
               FROM internet_preparation_jobs j JOIN remote_episodes e ON e.id=j.episode_id
               JOIN internet_sources s ON s.id=e.source_id JOIN channels c ON c.id=s.channel_id
               LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id
               WHERE j.state IN ('queued','retry') AND j.cancel_requested=0 AND s.enabled=1
                 AND (j.retry_after IS NULL OR j.retry_after<=?)
               ORDER BY j.priority,j.created_at LIMIT 1""",
            (utc_now(),),
        ).fetchone()
        if row is None:
            return None
        job_id = int(row["job_id"])
        self._active_job_id = job_id
        temporary, final, keep = self._target_paths(row)
        final.parent.mkdir(parents=True, exist_ok=True)
        expected = int(row["size_bytes"] or 0)
        cache = self.cache_status()
        required = max(0, expected - (temporary.stat().st_size if temporary.is_file() else 0))
        available_free = shutil.disk_usage(final.parent).free if keep else int(cache["free_bytes"])
        if (
            available_free - required < int(cache["reserve_bytes"])
            or (
                not keep
                and int(cache["size_bytes"]) + required > int(cache["limit_bytes"])
            )
        ):
            self._job(job_id, "paused_space", error="Not enough managed cache space")
            self._active_job_id = None
            return {"job_id": job_id, "state": "paused_space"}
        try:
            self._check_cancelled(job_id)
            self._job(job_id, "downloading", temporary_path=str(temporary), final_path=str(final), started=True)
            headers = {"User-Agent": "CRTV-Sim/0.9 Internet Media"}
            existing = temporary.stat().st_size if temporary.is_file() else 0
            if existing and (row["source_etag"] or row["source_last_modified"]):
                headers["Range"] = f"bytes={existing}-"
                if row["source_etag"]:
                    headers["If-Range"] = str(row["source_etag"])
                elif row["source_last_modified"]:
                    headers["If-Range"] = str(row["source_last_modified"])
            elif existing:
                existing = 0
            request = urllib.request.Request(str(row["remote_url"]), headers=headers)
            with urllib.request.urlopen(
                request,
                timeout=float(self.settings.get("request_timeout_seconds", 30)),
                context=self.ssl_context,
            ) as response:
                append = existing > 0 and response.status == 206
                content_range = response.headers.get("Content-Range") or ""
                if append and not content_range.startswith(f"bytes {existing}-"):
                    raise InternetMediaError("Remote server returned an unsafe resume range")
                if existing and not append:
                    existing = 0
                mode = "ab" if append else "wb"
                total = expected
                if response.headers.get("Content-Length"):
                    total = existing + int(response.headers["Content-Length"])
                etag = response.headers.get("ETag")
                modified = response.headers.get("Last-Modified")
                self.connection.execute(
                    "UPDATE remote_episodes SET source_etag=?,source_last_modified=?,updated_at=? WHERE id=?",
                    (etag, modified, utc_now(), int(row["episode_id"])),
                )
                self.connection.commit()
                with temporary.open(mode) as handle:
                    while True:
                        if self.stop_event is not None and self.stop_event.is_set():
                            raise InternetMediaError("Preparation interrupted by shutdown")
                        self._check_cancelled(job_id)
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        existing += len(chunk)
                        self.connection.execute(
                            "UPDATE internet_preparation_jobs SET bytes_downloaded=?,total_bytes=?,updated_at=? WHERE id=?",
                            (existing, total or None, utc_now(), job_id),
                        )
                        self.connection.commit()
            self._job(job_id, "validating")
            self._check_cancelled(job_id)
            if expected and temporary.stat().st_size != expected:
                raise InternetMediaError(
                    f"Download size {temporary.stat().st_size} did not match expected {expected}"
                )
            checksum_kind = row["checksum_kind"]
            checksum_value = row["checksum_value"]
            if checksum_kind in {"md5", "sha1"} and checksum_value:
                digest = hashlib.new(str(checksum_kind))
                with temporary.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        self._check_cancelled(job_id)
                        digest.update(chunk)
                if digest.hexdigest().casefold() != str(checksum_value).casefold():
                    raise InternetMediaError("Downloaded file checksum did not match source metadata")
            try:
                self._check_cancelled(job_id)
                probe = self._probe_and_decode(temporary)
            except PreparationCancelled:
                raise
            except InternetMediaError:
                self._job(job_id, "normalizing")
                compatible = temporary.with_name(temporary.name + ".compat.mp4")
                compatible.unlink(missing_ok=True)
                self._compatibility_convert(temporary, compatible)
                self._check_cancelled(job_id)
                probe = self._probe_and_decode(compatible)
                temporary.unlink(missing_ok=True)
                temporary = compatible
                final = final.with_suffix(".mp4")
                self._job(job_id, "validating", temporary_path=str(temporary), final_path=str(final))
            self._check_cancelled(job_id)
            os.replace(temporary, final)
            aspect_mode = None
            crop_json = None
            from retrotv.processing import enabled
            if not keep and enabled(self.config, "normalization"):
                self._job(job_id, "analyzing")
                analysis = NormalizationAnalyzer(self.config).analyze(final, probe.metadata)
                self._check_cancelled(job_id)
                if analysis.status != "analyzed":
                    self.connection.execute(
                        "UPDATE remote_episodes SET local_path=?,aspect_mode=?,crop_json=?,source_etag=?,source_last_modified=?,updated_at=? WHERE id=?",
                        (
                            str(final),
                            analysis.proposed_mode,
                            json.dumps(analysis.crop.as_dict()) if analysis.crop else None,
                            etag,
                            modified,
                            utc_now(),
                            int(row["episode_id"]),
                        ),
                    )
                    self.connection.commit()
                    self._job(job_id, "review", error=analysis.detail)
                    return {"job_id": job_id, "state": "review", "detail": analysis.detail}
                aspect_mode = analysis.proposed_mode
                crop_json = json.dumps(analysis.crop.as_dict()) if analysis.crop else None
            state = "awaiting_scan" if keep else "ready"
            now = utc_now()
            self.connection.execute(
                """UPDATE remote_episodes SET local_path=?,retained=?,aspect_mode=?,crop_json=?,
                     source_etag=?,source_last_modified=?,duration_seconds=COALESCE(duration_seconds,?),updated_at=?
                   WHERE id=?""",
                (
                    str(final),
                    int(keep),
                    aspect_mode,
                    crop_json,
                    etag,
                    modified,
                    probe.metadata.get("duration_seconds"),
                    now,
                    int(row["episode_id"]),
                ),
            )
            self.connection.commit()
            self._job(job_id, state, completed=not keep)
            return {"job_id": job_id, "state": state, "path": str(final)}
        except PreparationCancelled as exc:
            self._delete_incomplete(str(temporary))
            self._delete_incomplete(str(temporary.with_name(temporary.name + ".compat.mp4")))
            self.connection.execute(
                """UPDATE internet_preparation_jobs SET state='cataloged',cancel_requested=0,
                     bytes_downloaded=0,total_bytes=NULL,temporary_path=NULL,final_path=NULL,
                     retry_after=NULL,error_detail=?,updated_at=? WHERE id=?""",
                (str(exc), utc_now(), job_id),
            )
            self.connection.commit()
            return {"job_id": job_id, "state": "cataloged", "cancelled": True}
        except Exception as exc:
            retry_count = int(row["retry_count"]) + 1
            maximum = max(0, int(self.settings.get("maximum_retries", 3)))
            state = "retry" if retry_count <= maximum else "failed"
            retry_after = (
                datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2 ** (retry_count - 1)))
            ).replace(microsecond=0).isoformat()
            if isinstance(exc, urllib.error.HTTPError):
                guidance = exc.headers.get("Retry-After") if exc.headers else None
                if guidance:
                    try:
                        delay = max(0, int(guidance))
                        retry_after = (datetime.now(UTC) + timedelta(seconds=delay)).replace(
                            microsecond=0
                        ).isoformat()
                    except ValueError:
                        try:
                            advised = parsedate_to_datetime(guidance)
                            if advised.tzinfo is None:
                                advised = advised.replace(tzinfo=UTC)
                            retry_after = advised.astimezone(UTC).replace(microsecond=0).isoformat()
                        except (TypeError, ValueError, OverflowError):
                            pass
            self.connection.execute(
                """UPDATE internet_preparation_jobs SET state=?,retry_count=?,retry_after=?,
                     error_detail=?,updated_at=? WHERE id=?""",
                (state, retry_count, retry_after, str(exc)[:2000], utc_now(), job_id),
            )
            self.connection.commit()
            return {"job_id": job_id, "state": state, "error": str(exc)}
        finally:
            self._active_job_id = None

    def _job(
        self,
        job_id: int,
        state: str,
        *,
        error: str | None = None,
        temporary_path: str | None = None,
        final_path: str | None = None,
        started: bool = False,
        completed: bool = False,
    ) -> None:
        self.connection.execute(
            """UPDATE internet_preparation_jobs SET state=?,error_detail=?,
                 temporary_path=COALESCE(?,temporary_path),final_path=COALESCE(?,final_path),
                 started_at=CASE WHEN ? THEN COALESCE(started_at,?) ELSE started_at END,
                 completed_at=CASE WHEN ? THEN ? ELSE completed_at END,updated_at=? WHERE id=?""",
            (
                state,
                error,
                temporary_path,
                final_path,
                int(started),
                utc_now(),
                int(completed),
                utc_now(),
                utc_now(),
                job_id,
            ),
        )
        self.connection.commit()

    def retry(self, job_id: int) -> None:
        cursor = self.connection.execute(
            """UPDATE internet_preparation_jobs SET state='queued',retry_after=NULL,
                 cancel_requested=0,error_detail=NULL,updated_at=? WHERE id=?
               AND state IN ('failed','retry','review','paused_space')""",
            (utc_now(), job_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("Preparation job cannot be retried")
        self.connection.commit()

    def delete_completed_cache(self, episode_id: int) -> bool:
        row = self.connection.execute(
            """SELECT e.local_path,e.retained,e.completed_at,p.storage_mode
               FROM remote_episodes e JOIN internet_sources s ON s.id=e.source_id
               JOIN channel_playback_preferences p ON p.channel_id=s.channel_id WHERE e.id=?""",
            (episode_id,),
        ).fetchone()
        if row is None or row["retained"] or not row["completed_at"] or row["storage_mode"] != "save_space":
            return False
        path = Path(str(row["local_path"] or "")).resolve()
        root = self.cache_root
        if not path.is_file() or root not in path.parents:
            return False
        path.unlink()
        self.connection.execute(
            "UPDATE remote_episodes SET local_path=NULL,updated_at=? WHERE id=?",
            (utc_now(), episode_id),
        )
        self.connection.commit()
        return True

    def cleanup_completed_cache(self) -> int:
        episode_ids = [
            int(row[0])
            for row in self.connection.execute(
                """SELECT e.id FROM remote_episodes e
                   JOIN internet_sources s ON s.id=e.source_id
                   JOIN channel_playback_preferences p ON p.channel_id=s.channel_id
                   WHERE e.completed_at IS NOT NULL AND e.retained=0
                     AND e.local_path IS NOT NULL AND p.storage_mode='save_space'"""
            )
        ]
        deleted = 0
        for episode_id in episode_ids:
            try:
                deleted += int(self.delete_completed_cache(episode_id))
            except OSError:
                # The player or an antivirus scanner may still have the file open.
                continue
        return deleted

    def preview_empty_cache(self, *, active_path: str | Path | None = None) -> dict[str, object]:
        root = self.cache_root
        protected = self._cache_protected_paths(active_path)
        files: list[Path] = []
        bytes_total = 0
        if root.is_dir():
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                if resolved in protected:
                    continue
                files.append(resolved)
                try:
                    bytes_total += resolved.stat().st_size
                except OSError:
                    continue
        return {
            "directory": str(root),
            "files": len(files),
            "bytes": bytes_total,
            "protected_files": len(protected),
            "paths": [str(path) for path in files],
        }

    def empty_cache(self, *, active_path: str | Path | None = None) -> dict[str, object]:
        preview = self.preview_empty_cache(active_path=active_path)
        removed_paths: list[str] = []
        bytes_removed = 0
        errors: list[str] = []
        for raw in preview["paths"]:
            candidate = Path(str(raw))
            try:
                size = candidate.stat().st_size
                candidate.unlink()
                removed_paths.append(str(candidate))
                bytes_removed += size
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")

        if removed_paths:
            marks = ",".join("?" for _ in removed_paths)
            now = utc_now()
            episode_ids = [
                int(row[0]) for row in self.connection.execute(
                    f"SELECT id FROM remote_episodes WHERE local_path IN ({marks})",
                    removed_paths,
                )
            ]
            self.connection.execute(
                f"UPDATE remote_episodes SET local_path=NULL,updated_at=? WHERE local_path IN ({marks})",
                (now, *removed_paths),
            )
            if episode_ids:
                episode_marks = ",".join("?" for _ in episode_ids)
                self.connection.execute(
                    f"""UPDATE internet_preparation_jobs SET
                            state=CASE WHEN state='ready' THEN 'cataloged' ELSE state END,
                            temporary_path=CASE WHEN state='ready' THEN NULL ELSE temporary_path END,
                            final_path=CASE WHEN state='ready' THEN NULL ELSE final_path END,
                            bytes_downloaded=CASE WHEN state='ready' THEN 0 ELSE bytes_downloaded END,
                            total_bytes=CASE WHEN state='ready' THEN NULL ELSE total_bytes END,
                            retry_after=CASE WHEN state='ready' THEN NULL ELSE retry_after END,
                            cancel_requested=CASE WHEN state='ready' THEN 0 ELSE cancel_requested END,
                            error_detail=CASE WHEN state='ready' THEN 'Managed cache was emptied' ELSE error_detail END,
                            updated_at=? WHERE episode_id IN ({episode_marks})""",
                    (now, *episode_ids),
                )
            self.connection.commit()

        # Remove empty directories but never the cache root itself.
        if self.cache_root.is_dir():
            for directory in sorted(
                (path for path in self.cache_root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts), reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return {
            "state": "complete" if not errors else "failed",
            "directory": str(self.cache_root),
            "files_removed": len(removed_paths),
            "bytes_removed": bytes_removed,
            "protected_files": preview["protected_files"],
            "errors": errors,
        }

    def _cache_protected_paths(self, active_path: str | Path | None) -> set[Path]:
        root = self.cache_root
        protected: set[Path] = set()

        def include(raw: object) -> None:
            if not raw:
                return
            path = Path(str(raw)).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                return
            protected.add(path)

        include(active_path)
        for row in self.connection.execute(
            "SELECT local_path FROM remote_episodes WHERE retained=1 AND local_path IS NOT NULL"
        ):
            include(row["local_path"])
        for row in self.connection.execute(
            "SELECT temporary_path,final_path,state FROM internet_preparation_jobs"
        ):
            state = str(row["state"])
            if state not in {"ready", "completed"}:
                include(row["temporary_path"])
                include(row["final_path"])
        return protected
