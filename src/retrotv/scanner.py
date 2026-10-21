from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from retrotv.classification import CHANNEL_DAYPARTS, Classification, classify_path, slugify
from retrotv.config import AppConfig
from retrotv.commercial_review import CommercialReviewService
from retrotv.db import ensure_storage_root, transaction, utc_now
from retrotv.duplicates import DuplicateService, sha256_file
from retrotv.graphics import GraphicsService
from retrotv.holiday_review import HolidayReviewService
from retrotv.shadows import ShadowService
from retrotv.probe import (
    DecodeResult,
    ProbeResult,
    decode_sample,
    fingerprint_file,
    probe_media,
)

RESERVED_TOP_LEVEL_FOLDERS = {"_channel_template", "_guide"}
NON_CHANNEL_TOP_LEVEL_FOLDERS = RESERVED_TOP_LEVEL_FOLDERS | {"shared"}


class MediaInspector(Protocol):
    def probe(self, path: Path) -> ProbeResult: ...

    def sample(self, path: Path, start_seconds: float) -> DecodeResult: ...

    def fingerprint(self, path: Path) -> tuple[str, str]: ...


class FFmpegInspector:
    def __init__(self, config: AppConfig) -> None:
        scanner = config.data.get("scanner", {})
        self.ffprobe = config.ffprobe
        self.ffmpeg = config.ffmpeg
        self.probe_timeout = float(scanner.get("probe_timeout_seconds", 20))
        self.decode_timeout = float(scanner.get("decode_timeout_seconds", 30))
        self.sample_seconds = float(scanner.get("sample_seconds", 2))
        self.fingerprint_mode = str(scanner.get("fingerprint_mode", "quick"))
        self.quick_bytes = int(scanner.get("quick_fingerprint_bytes", 1024 * 1024))

    def probe(self, path: Path) -> ProbeResult:
        return probe_media(
            path,
            ffprobe=self.ffprobe,
            timeout_seconds=self.probe_timeout,
        )

    def sample(self, path: Path, start_seconds: float) -> DecodeResult:
        return decode_sample(
            path,
            ffmpeg=self.ffmpeg,
            start_seconds=start_seconds,
            sample_seconds=self.sample_seconds,
            timeout_seconds=self.decode_timeout,
        )

    def fingerprint(self, path: Path) -> tuple[str, str]:
        return fingerprint_file(
            path,
            mode=self.fingerprint_mode,
            quick_bytes=self.quick_bytes,
        )


@dataclass
class ScanSummary:
    scan_run_id: int
    discovered: int = 0
    ready: int = 0
    review: int = 0
    errors: int = 0
    unchanged: int = 0
    missing: int = 0
    channels_created: int = 0
    channels_enabled: int = 0
    channels_disabled: int = 0
    shadows_ready: int = 0
    shadows_review: int = 0
    shadows_removed: int = 0


class LibraryScanner:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: AppConfig,
        *,
        inspector: MediaInspector | None = None,
    ) -> None:
        self.connection = connection
        self.config = config
        self.inspector = inspector or FFmpegInspector(config)
        library = config.data.get("library", {})
        seasonal = config.data.get("seasonal", {})
        self.known_channels = {
            slugify(value) for value in library.get("channels", [])
        }
        raw_windows = seasonal.get("windows", [])
        windows = raw_windows.values() if isinstance(raw_windows, dict) else raw_windows
        self.known_channels.update(
            slugify(str(item["channel"]))
            for item in windows
            if item.get("channel")
        )
        self.known_holidays = {
            slugify(value) for value in library.get("holidays", [])
        }
        raw_holidays = seasonal.get("holidays", [])
        if isinstance(raw_holidays, dict):
            self.known_holidays.update(slugify(str(value)) for value in raw_holidays)
        else:
            self.known_holidays.update(
                slugify(str(item["slug"]))
                for item in raw_holidays
                if item.get("slug")
            )
        configured_dayparts = {
            slugify(str(item["slug"]))
            for item in config.data.get("scheduling", {}).get("dayparts", [])
            if isinstance(item, dict) and item.get("slug")
        }
        self.known_dayparts = (
            configured_dayparts | {"all-day", "daytime"}
            if configured_dayparts else CHANNEL_DAYPARTS
        )
        self.extensions = {
            str(value).lower()
            for value in config.data.get("scanner", {}).get(
                "video_extensions",
                [
                    ".mp4",
                    ".mkv",
                    ".m4v",
                    ".avi",
                    ".mov",
                    ".webm",
                    ".wmv",
                    ".ts",
                    ".mpg",
                    ".mpeg",
                ],
            )
        }
        scanner_config = config.data.get("scanner", {})
        self.sample_seconds = float(scanner_config.get("sample_seconds", 2))
        self.end_margin = float(
            scanner_config.get("sample_end_margin_seconds", 3)
        )

    def _sync_channels_from_folders(self, root: Path) -> tuple[int, int, int]:
        library = self.config.data.get("library", {})
        if not bool(library.get("auto_register_channels", True)):
            return 0, 0, 0

        discovered: dict[str, str] = {}
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_dir():
                continue
            folded = path.name.casefold()
            if (
                folded in NON_CHANNEL_TOP_LEVEL_FOLDERS
                or path.name.startswith(".")
                or path.name.startswith("_")
            ):
                continue
            slug = slugify(path.name)
            if not slug:
                continue
            previous = discovered.get(slug)
            if previous is not None and previous != path.name:
                raise ValueError(
                    f"Channel folders {previous!r} and {path.name!r} "
                    f"both normalize to {slug!r}"
                )
            discovered[slug] = path.name.strip()

        rows = self.connection.execute(
            "SELECT id, slug, channel_number, enabled FROM channels"
        ).fetchall()
        existing = {str(row["slug"]): row for row in rows}
        guide = self.config.data.get("guide", {})
        configured_numbers = {
            slugify(str(slug)): int(number)
            for slug, number in guide.get("channel_numbers", {}).items()
        }
        used_numbers = {
            int(row["channel_number"])
            for row in rows
            if row["channel_number"] is not None
        }
        used_numbers.update(configured_numbers.values())
        used_numbers.add(int(guide.get("channel_number", 2)))
        next_number = max(1, int(library.get("auto_channel_number_start", 10)))

        def allocate_number() -> int:
            nonlocal next_number
            while next_number in used_numbers:
                next_number += 1
            result = next_number
            used_numbers.add(result)
            next_number += 1
            return result

        created = enabled = 0
        for slug, display_name in discovered.items():
            configured_number = configured_numbers.get(slug)
            row = existing.get(slug)
            if row is None:
                number = (
                    configured_number
                    if configured_number is not None
                    else allocate_number()
                )
                self.connection.execute(
                    """
                    INSERT INTO channels(
                        slug, display_name, channel_number, enabled
                    ) VALUES (?, ?, ?, 1)
                    """,
                    (slug, display_name, number),
                )
                created += 1
            else:
                if not bool(row["enabled"]):
                    enabled += 1
                self.connection.execute(
                    """
                    UPDATE channels
                    SET enabled = 1,
                        channel_number = COALESCE(?, channel_number)
                    WHERE id = ?
                    """,
                    (configured_number, row["id"]),
                )

        disabled = 0
        if bool(library.get("auto_disable_missing_channels", True)):
            external_channels = {row[0] for row in self.connection.execute("SELECT channel_slug FROM external_assignments e JOIN storage_roots r ON r.id=e.root_id WHERE e.enabled=1 AND r.enabled=1")}
            external_channels.update(row[0] for row in self.connection.execute("SELECT a.channel_slug FROM iptv_assignments a JOIN iptv_feeds f ON f.id=a.feed_id WHERE f.enabled=1"))
            missing = [
                row for slug, row in existing.items() if slug not in discovered and slug not in external_channels
            ]
            for row in missing:
                if bool(row["enabled"]):
                    disabled += 1
                self.connection.execute(
                    "UPDATE channels SET enabled = 0 WHERE id = ?",
                    (row["id"],),
                )

        self.connection.commit()
        self.known_channels.update(discovered)
        return created, enabled, disabled

    def discover(self, root: Path) -> Iterable[Path]:
        for path in sorted(root.rglob("*")):
            relative_parts = path.relative_to(root).parts
            if (
                relative_parts
                and relative_parts[0].casefold() in RESERVED_TOP_LEVEL_FOLDERS
            ):
                continue
            if any(part.casefold() == "graphics" for part in relative_parts):
                continue
            # Weekly-special presentation overrides are cataloged separately by
            # GraphicsService. Video backgrounds here are not library programs.
            folded_parts = tuple(part.casefold() for part in relative_parts)
            if (
                len(folded_parts) >= 6
                and folded_parts[1:3] == ("specials", "weekly")
                and folded_parts[4] in {"up-next", "interstitials"}
            ):
                continue
            if (
                len(folded_parts) >= 8
                and folded_parts[1:3] == ("specials", "holidays")
                and folded_parts[4] == "weekly"
                and folded_parts[6] in {"up-next", "interstitials"}
            ):
                continue
            if (
                len(folded_parts) >= 9
                and folded_parts[1:3] == ("specials", "holidays")
                and folded_parts[4:6] == ("specials", "weekly")
                and folded_parts[7] in {"up-next", "interstitials"}
            ):
                continue
            # Audio normalization writes its staging output beside the source so
            # replacement remains atomic.  A concurrent scan must never mistake
            # that temporary MP4 for a new episode.
            if ".retrotv-audio.partial" in path.name.casefold():
                continue
            if path.is_file() and path.suffix.lower() in self.extensions:
                from retrotv.asset_paths import require_writable_path
                try:
                    require_writable_path(self.connection, path)
                except ValueError:
                    continue
                yield path

    def _backfill_normalization_profiles(self) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO normalization_profiles(asset_id)
            SELECT a.id
            FROM assets a
            LEFT JOIN normalization_profiles n ON n.asset_id = a.id
            WHERE n.asset_id IS NULL AND a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed')
            """
        )
        self.connection.commit()
        return max(0, int(cursor.rowcount))

    def _repair_managed_normalization_profiles(self) -> int:
        """Restore profiles for verified corrected working copies.

        A drive copy can change timestamps without changing bytes. Older scans
        reset those profiles and then reused the original HD crop recipe against
        the already-corrected 640x480 working file. The promotion hashes provide
        an unambiguous way to restore the intended post-promotion state.
        """
        rows = self.connection.execute(
            """
            SELECT DISTINCT n.asset_id
            FROM normalization_profiles n
            JOIN asset_content_hashes h ON h.asset_id=n.asset_id
            JOIN normalization_promotions p
              ON p.asset_id=n.asset_id AND p.status='active'
            LEFT JOIN audio_normalization_promotions ap
              ON ap.asset_id=n.asset_id AND ap.status='active'
            WHERE n.asset_id IN (SELECT a.id FROM assets a JOIN storage_roots r ON r.id=a.source_root_id WHERE r.source_kind='managed') AND n.review_requested=0 AND (h.working_sha256=p.corrected_sha256
               OR (
                    ap.original_sha256=p.corrected_sha256
                AND h.working_sha256=ap.corrected_sha256
               ))
            """
        ).fetchall()
        asset_ids = [int(row["asset_id"]) for row in rows]
        if not asset_ids:
            return 0
        self.connection.executemany(
            """
            UPDATE normalization_profiles SET
                analysis_status='analyzed', detected_mode='native_4_3',
                proposed_mode='native_4_3', override_mode='auto', confidence=1.0,
                crop_x=NULL, crop_y=NULL, crop_width=NULL, crop_height=NULL,
                analysis_detail='Corrected working copy restored from verified promotion history.',
                proxy_root_id=NULL, proxy_relative_path=NULL,
                proxy_status='promoted', proxy_error=NULL
            WHERE asset_id=?
            """,
            [(asset_id,) for asset_id in asset_ids],
        )
        self.connection.commit()
        return len(asset_ids)

    def _repair_invalid_normalization_geometry(self) -> int:
        """Requeue stored crops that cannot fit the asset's current frame."""
        rows = self.connection.execute(
            """
            SELECT n.asset_id, h.working_sha256, h.working_file_size,
                   p.id AS video_promotion_id
            FROM normalization_profiles n
            JOIN asset_media_info m ON m.asset_id=n.asset_id
            LEFT JOIN asset_content_hashes h ON h.asset_id=n.asset_id
            LEFT JOIN normalization_promotions p
              ON p.asset_id=n.asset_id AND p.status='active'
            WHERE n.asset_id IN (SELECT a.id FROM assets a JOIN storage_roots r ON r.id=a.source_root_id WHERE r.source_kind='managed') AND n.crop_width IS NOT NULL AND n.crop_height IS NOT NULL
              AND (
                   n.crop_x < 0 OR n.crop_y < 0
                OR n.crop_width <= 0 OR n.crop_height <= 0
                OR n.crop_x + n.crop_width > m.width
                OR n.crop_y + n.crop_height > m.height
              )
            """
        ).fetchall()
        now = utc_now()
        for row in rows:
            asset_id = int(row["asset_id"])
            if row["video_promotion_id"] is not None:
                # The current bytes no longer match the corrected generation;
                # close the obsolete chain before new analysis is stored.
                self.connection.execute(
                    """UPDATE normalization_promotions
                       SET status='restored', restored_at=?
                       WHERE asset_id=? AND status='active'""",
                    (now, asset_id),
                )
                self.connection.execute(
                    """UPDATE audio_normalization_promotions
                       SET status='restored', restored_at=?
                       WHERE asset_id=? AND status='active'""",
                    (now, asset_id),
                )
                if row["working_sha256"]:
                    self.connection.execute(
                        """
                        UPDATE asset_content_hashes SET
                            original_sha256=working_sha256,
                            original_file_size=working_file_size
                        WHERE asset_id=?
                        """,
                        (asset_id,),
                    )
            self.connection.execute(
                """
                UPDATE normalization_profiles SET
                    analysis_status='pending', detected_mode=NULL,
                    proposed_mode=NULL, override_mode='auto', confidence=NULL,
                    crop_x=NULL, crop_y=NULL, crop_width=NULL, crop_height=NULL,
                    sample_results_json=NULL,
                    analysis_detail='Stored crop did not fit the current media frame; reanalysis required.',
                    analyzed_at=NULL, proxy_root_id=NULL, proxy_relative_path=NULL,
                    proxy_status='not_created', proxy_error=NULL
                WHERE asset_id=?
                """,
                (asset_id,),
            )
        self.connection.commit()
        return len(rows)

    def _backfill_eligibility(self, root_id: int) -> int:
        """Restore derived folder eligibility without re-probing unchanged media."""
        rows = self.connection.execute(
            """
            SELECT a.id, a.source_relative_path
            FROM assets a
            WHERE a.source_root_id = ?
              AND a.integrity_status <> 'missing'
              AND NOT EXISTS (
                  SELECT 1 FROM asset_eligibility e WHERE e.asset_id = a.id
              )
            ORDER BY a.id
            """,
            (root_id,),
        ).fetchall()
        for row in rows:
            classification = classify_path(
                str(row["source_relative_path"]),
                known_channels=self.known_channels,
                known_holidays=self.known_holidays,
                known_dayparts=self.known_dayparts,
            )
            self._replace_eligibility(int(row["id"]), classification)
        self.connection.commit()
        return len(rows)

    def _prepare_normalization(
        self,
        asset_id: int,
        *,
        reset: bool,
        working_sha256: str | None = None,
    ) -> None:
        promotion = self.connection.execute(
            """
            SELECT p.corrected_sha256 AS video_corrected,
                   ap.original_sha256 AS audio_original,
                   ap.corrected_sha256 AS audio_corrected
            FROM normalization_promotions p
            LEFT JOIN audio_normalization_promotions ap
              ON ap.asset_id=p.asset_id AND ap.status='active'
            WHERE p.asset_id=? AND p.status='active'
            ORDER BY p.id DESC LIMIT 1
            """,
            (asset_id,),
        ).fetchone()
        managed_corrected = bool(
            working_sha256
            and promotion is not None
            and (
                working_sha256 == promotion["video_corrected"]
                or (
                    promotion["audio_original"] == promotion["video_corrected"]
                    and working_sha256 == promotion["audio_corrected"]
                )
            )
        )
        if managed_corrected:
            self.connection.execute(
                """
                UPDATE normalization_profiles SET
                    analysis_status='analyzed', detected_mode='native_4_3',
                    proposed_mode='native_4_3', override_mode='auto', confidence=1.0,
                    crop_x=NULL, crop_y=NULL, crop_width=NULL, crop_height=NULL,
                    analysis_detail='Corrected working copy restored from verified promotion history.',
                    proxy_root_id=NULL, proxy_relative_path=NULL,
                    proxy_status='promoted', proxy_error=NULL
                WHERE asset_id=?
                """,
                (asset_id,),
            )
            return
        if not reset:
            self.connection.execute(
                """
                INSERT INTO normalization_profiles(asset_id)
                VALUES (?)
                ON CONFLICT(asset_id) DO NOTHING
                """,
                (asset_id,),
            )
            return
        if promotion is not None:
            # The file no longer matches any verified corrected generation. It
            # is a user replacement or restore, so its old promotion chain must
            # not make duplicate analysis reuse the previous source geometry.
            now = utc_now()
            self.connection.execute(
                """UPDATE normalization_promotions
                   SET status='restored', restored_at=?
                   WHERE asset_id=? AND status='active'""",
                (now, asset_id),
            )
            self.connection.execute(
                """UPDATE audio_normalization_promotions
                   SET status='restored', restored_at=?
                   WHERE asset_id=? AND status='active'""",
                (now, asset_id),
            )
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(asset_id)
            VALUES (?)
            ON CONFLICT(asset_id) DO UPDATE SET
                analysis_status = 'pending',
                detected_mode = NULL,
                proposed_mode = NULL,
                override_mode = 'auto',
                confidence = NULL,
                crop_x = NULL,
                crop_y = NULL,
                crop_width = NULL,
                crop_height = NULL,
                sample_results_json = NULL,
                analysis_detail = NULL,
                analyzed_at = NULL,
                proxy_status = CASE
                    WHEN proxy_status IN ('ready', 'promoted') THEN 'stale'
                    ELSE proxy_status
                END
            """,
            (asset_id,),
        )

    def scan(
        self,
        *,
        logical_root: str = "media",
        limit: int | None = None,
        force: bool = False,
        skip_samples: bool = False,
        progress: Callable[[int, Path, str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> ScanSummary:
        root = self.config.storage_root(logical_root)
        if not root.is_dir():
            raise FileNotFoundError(f"Media root does not exist: {root}")
        root_id = ensure_storage_root(self.connection, logical_root)
        channel_counts = (
            self._sync_channels_from_folders(root)
            if logical_root == "media"
            else (0, 0, 0)
        )
        if logical_root == "media":
            GraphicsService(self.connection, self.config).ensure_all()
            self._backfill_eligibility(root_id)
        self._backfill_normalization_profiles()
        self._repair_managed_normalization_profiles()
        self._repair_invalid_normalization_geometry()
        started = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO scan_runs(root_id, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (root_id, started),
        )
        scan_run_id = int(cursor.lastrowid)
        self.connection.commit()
        summary = ScanSummary(scan_run_id)
        (
            summary.channels_created,
            summary.channels_enabled,
            summary.channels_disabled,
        ) = channel_counts

        try:
            cancelled = False
            for path in self.discover(root):
                if cancel_requested is not None and cancel_requested():
                    cancelled = True
                    break
                if limit is not None and summary.discovered >= limit:
                    break
                summary.discovered += 1
                outcome = self._scan_one(
                    root,
                    root_id,
                    path,
                    scan_run_id,
                    force=force,
                    skip_samples=skip_samples,
                )
                if outcome == "ready":
                    summary.ready += 1
                elif outcome == "unchanged":
                    summary.unchanged += 1
                elif outcome == "review":
                    summary.review += 1
                else:
                    summary.errors += 1
                if progress is not None:
                    progress(summary.discovered, path, outcome)
            if limit is None and not cancelled:
                summary.missing = self._mark_missing(root, root_id, scan_run_id)
                if logical_root == "media":
                    shadows = ShadowService(self.connection, self.config).refresh()
                    summary.shadows_ready = shadows.ready
                    summary.shadows_review = shadows.review
                    summary.shadows_removed = shadows.removed
            if logical_root == "media":
                CommercialReviewService(self.connection, self.config).reconcile()
                HolidayReviewService(self.connection, self.config).reconcile()
            status = "cancelled" if cancelled else "completed"
        except Exception:
            status = "failed"
            raise
        finally:
            self.connection.execute(
                """
                UPDATE scan_runs SET
                    completed_at = ?,
                    status = ?,
                    discovered_count = ?,
                    ready_count = ?,
                    review_count = ?,
                    error_count = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    summary.discovered,
                    summary.ready + summary.unchanged,
                    summary.review,
                    summary.errors,
                    scan_run_id,
                ),
            )
            self.connection.commit()
        return summary

    def _mark_missing(self, root: Path, root_id: int, scan_run_id: int) -> int:
        rows = self.connection.execute(
            """
            SELECT id, source_relative_path, integrity_status, review_status
            FROM assets
            WHERE source_root_id = ?
              AND (last_scan_run_id IS NULL OR last_scan_run_id <> ?)
            """,
            (root_id, scan_run_id),
        ).fetchall()
        missing = [
            row
            for row in rows
            if not (root / Path(row["source_relative_path"])).is_file()
        ]
        now = utc_now()
        newly_retired = 0
        for row in missing:
            was_already_missing = row["integrity_status"] == "missing"
            self.connection.execute(
                """
                UPDATE assets
                SET integrity_status = 'missing', review_status = 'ignored',
                    enabled_for_scheduling = 0
                WHERE id = ?
                """,
                (row["id"],),
            )
            # Treat a file removed from an available media root as an
            # intentional library deletion. Retain its row for historical
            # schedules and fingerprint-based move detection, but do not leave
            # a permanent review task behind.
            self._replace_review_items(int(row["id"]), [], now)
            if not was_already_missing:
                newly_retired += 1
        self.connection.commit()
        return newly_retired

    def _scan_one(
        self,
        root: Path,
        root_id: int,
        path: Path,
        scan_run_id: int,
        *,
        force: bool,
        skip_samples: bool,
    ) -> str:
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        existing = self.connection.execute(
            """
            SELECT * FROM assets
            WHERE source_root_id = ? AND source_relative_path = ?
            """,
            (root_id, relative),
        ).fetchone()
        holiday_scope_changed = False
        if existing is not None and "/specials/holidays/" in f"/{relative}":
            current_scope = classify_path(
                relative,
                known_channels=self.known_channels,
                known_holidays=self.known_holidays,
                known_dayparts=self.known_dayparts,
            )
            holiday_scope_changed = any(
                existing[column] != getattr(current_scope, column)
                for column in ("media_type", "daypart_slug", "holiday_weekly_slug")
            )
        shared_episode_rule_changed = False
        path_parts = relative.split("/")
        if (
            existing is not None
            and len(path_parts) >= 4
            and slugify(path_parts[0]) == "shared"
            and ((slugify(path_parts[1]) in self.known_dayparts
                  and slugify(path_parts[2]) == "episodes")
                 or "/specials/holidays/" in f"/{relative}")
        ):
            old_shared_rule = self.connection.execute(
                """SELECT 1 FROM review_items
                   WHERE asset_id=? AND reason_code IN ('shared_daypart_type', 'shared_program_requires_channels')
                     AND status='open' LIMIT 1""",
                (int(existing["id"]),),
            ).fetchone()
            if old_shared_rule is not None:
                new_classification = classify_path(
                    relative,
                    known_channels=self.known_channels,
                    known_holidays=self.known_holidays,
                    known_dayparts=self.known_dayparts,
                )
                shared_episode_rule_changed = new_classification.is_ready
        if (
            existing is not None
            and not force
            and int(existing["file_size"]) == stat.st_size
            and int(existing["modified_ns"]) == stat.st_mtime_ns
            and existing["metadata_status"] == "ready"
            and existing["integrity_status"] in {"ready", "not_scanned"}
            and not holiday_scope_changed
            and not shared_episode_rule_changed
        ):
            self.connection.execute(
                """
                UPDATE assets SET last_seen_at = ?, last_scan_run_id = ?
                WHERE id = ?
                """,
                (utc_now(), scan_run_id, existing["id"]),
            )
            self.connection.commit()
            return "unchanged"

        classification = classify_path(
            relative,
            known_channels=self.known_channels,
            known_holidays=self.known_holidays,
            known_dayparts=self.known_dayparts,
        )
        # Preserve exact source identity before normalization can replace the
        # working file. The existing fast fingerprint remains move-detection data.
        exact_sha256 = sha256_file(path)
        fingerprint, algorithm = self.inspector.fingerprint(path)
        existing_id = int(existing["id"]) if existing is not None else None
        if existing_id is None:
            existing_id = self._find_moved_asset(
                root,
                root_id,
                relative,
                fingerprint,
            )
        prior_hash = (
            self.connection.execute(
                "SELECT working_sha256 FROM asset_content_hashes WHERE asset_id=?",
                (existing_id,),
            ).fetchone()
            if existing_id is not None
            else None
        )
        content_unchanged = bool(
            prior_hash is not None and prior_hash["working_sha256"] == exact_sha256
        )
        probe = self.inspector.probe(path)
        start_sample = DecodeResult(False, "not_scanned", "Sample scan skipped")
        end_sample = DecodeResult(False, "not_scanned", "Sample scan skipped")
        if probe.ok and not skip_samples:
            start_sample = self.inspector.sample(path, 0.0)
            duration = float(probe.metadata["duration_seconds"])
            end_start = max(
                0.0,
                duration - self.end_margin - self.sample_seconds,
            )
            end_sample = self.inspector.sample(path, end_start)

        if not probe.ok:
            classification.review_reasons.append(
                (
                    "probe_failed",
                    f"{probe.error_category}: {probe.error_detail}",
                )
            )
        if probe.ok and not skip_samples and not start_sample.ok:
            classification.review_reasons.append(
                (
                    "beginning_decode_failed",
                    f"{start_sample.error_category}: {start_sample.error_detail}",
                )
            )
        if probe.ok and not skip_samples and not end_sample.ok:
            classification.review_reasons.append(
                (
                    "ending_decode_failed",
                    f"{end_sample.error_category}: {end_sample.error_detail}",
                )
            )

        duration = (
            float(probe.metadata["duration_seconds"]) if probe.ok else None
        )
        duration_class = self._duration_class(duration)
        metadata_status = "ready" if probe.ok else "error"
        integrity_status = (
            "not_scanned"
            if skip_samples and probe.ok
            else "ready"
            if probe.ok and start_sample.ok and end_sample.ok
            else "error"
        )
        review_status = "open" if classification.review_reasons else "ready"
        enabled = int(review_status == "ready")
        try:
            internet_job = self.connection.execute(
                """SELECT j.state FROM remote_episodes e
                   JOIN internet_preparation_jobs j ON j.episode_id=e.id
                   WHERE e.local_path=? AND e.retained=1""",
                (str(path.resolve()),),
            ).fetchone()
        except sqlite3.OperationalError:
            internet_job = None
        if internet_job is not None and internet_job["state"] != "ready":
            enabled = 0
        now = utc_now()

        with transaction(self.connection):
            asset_id = self._upsert_asset(
                existing_id=existing_id,
                root_id=root_id,
                relative=relative,
                path=path,
                stat=stat,
                fingerprint=fingerprint,
                algorithm=algorithm,
                classification=classification,
                duration_class=duration_class,
                metadata_status=metadata_status,
                integrity_status=integrity_status,
                review_status=review_status,
                enabled=enabled,
                scan_run_id=scan_run_id,
                now=now,
            )
            self.connection.execute(
                "UPDATE assets SET holiday_weekly_slug=? WHERE id=?",
                (classification.holiday_weekly_slug, asset_id),
            )
            self._replace_eligibility(asset_id, classification)
            self._replace_media_info(asset_id, probe)
            self._prepare_normalization(
                asset_id,
                reset=existing_id is not None and not content_unchanged,
                working_sha256=exact_sha256,
            )
            self._record_integrity(
                asset_id,
                scan_run_id,
                probe,
                start_sample,
                end_sample,
                skip_samples=skip_samples,
            )
            self._replace_review_items(
                asset_id,
                classification.review_reasons,
                now,
            )

        duplicates = DuplicateService(self.connection, self.config)
        duplicates.record_asset(asset_id, path, sha256=exact_sha256)
        duplicates.apply_normalization_recipe(asset_id)

        return "review" if review_status == "open" else "ready"

    def _find_moved_asset(
        self,
        root: Path,
        root_id: int,
        relative: str,
        fingerprint: str,
    ) -> int | None:
        rows = self.connection.execute(
            """
            SELECT id, source_relative_path FROM assets
            WHERE source_root_id = ? AND fingerprint = ?
              AND source_relative_path <> ?
            """,
            (root_id, fingerprint, relative),
        ).fetchall()
        missing = [
            row
            for row in rows
            if not (root / str(row["source_relative_path"])).exists()
        ]
        return int(missing[0]["id"]) if len(missing) == 1 else None

    def _find_existing_duplicate(
        self,
        root_id: int,
        relative: str,
        fingerprint: str,
        *,
        exclude_id: int | None,
    ) -> sqlite3.Row | None:
        query = """
            SELECT id, source_relative_path FROM assets
            WHERE source_root_id = ? AND fingerprint = ?
              AND source_relative_path <> ?
        """
        params: list[object] = [root_id, fingerprint, relative]
        if exclude_id is not None:
            query += " AND id <> ?"
            params.append(exclude_id)
        return self.connection.execute(query, params).fetchone()

    def _upsert_asset(
        self,
        *,
        existing_id: int | None,
        root_id: int,
        relative: str,
        path: Path,
        stat: object,
        fingerprint: str,
        algorithm: str,
        classification: Classification,
        duration_class: str | None,
        metadata_status: str,
        integrity_status: str,
        review_status: str,
        enabled: int,
        scan_run_id: int,
        now: str,
    ) -> int:
        values = (
            root_id,
            relative,
            path.name,
            fingerprint,
            algorithm,
            stat.st_size,
            stat.st_mtime_ns,
            classification.media_type,
            duration_class,
            classification.display_title,
            classification.series_title,
            classification.channel_slug,
            classification.daypart_slug,
            classification.collection_type,
            classification.collection_slug,
            classification.holiday_slug,
            metadata_status,
            integrity_status,
            review_status,
            enabled,
            now,
            scan_run_id,
        )
        if existing_id is None:
            cursor = self.connection.execute(
                """
                INSERT INTO assets(
                    source_root_id, source_relative_path, original_filename,
                    fingerprint, fingerprint_algorithm, file_size, modified_ns,
                    media_type, duration_class, display_title, series_title,
                    channel_slug, daypart_slug, collection_type, collection_slug,
                    holiday_slug, metadata_status, integrity_status, review_status,
                    enabled_for_scheduling, first_seen_at, last_seen_at,
                    last_scan_run_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (*values[:-2], now, *values[-2:]),
            )
            return int(cursor.lastrowid)

        self.connection.execute(
            """
            UPDATE assets SET
                source_root_id = ?, source_relative_path = ?,
                original_filename = ?, fingerprint = ?,
                fingerprint_algorithm = ?, file_size = ?, modified_ns = ?,
                media_type = ?, duration_class = ?, display_title = ?,
                series_title = ?, channel_slug = ?, daypart_slug = ?,
                collection_type = ?, collection_slug = ?, holiday_slug = ?,
                metadata_status = ?, integrity_status = ?, review_status = ?,
                enabled_for_scheduling = ?, last_seen_at = ?,
                last_scan_run_id = ?
            WHERE id = ?
            """,
            (*values, existing_id),
        )
        return existing_id

    def _replace_eligibility(
        self,
        asset_id: int,
        classification: Classification,
    ) -> None:
        self.connection.execute(
            "DELETE FROM asset_eligibility WHERE asset_id = ?",
            (asset_id,),
        )
        self.connection.executemany(
            """
            INSERT INTO asset_eligibility(
                asset_id, channel_slug, daypart_slug, eligibility_source,
                weight, enabled
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            [
                (
                    asset_id,
                    item.channel_slug,
                    item.daypart_slug,
                    item.source,
                    item.weight,
                )
                for item in classification.eligibility
            ],
        )

    def _replace_media_info(
        self,
        asset_id: int,
        probe: ProbeResult,
    ) -> None:
        self.connection.execute(
            "DELETE FROM asset_media_info WHERE asset_id = ?",
            (asset_id,),
        )
        if not probe.ok:
            return
        metadata = probe.metadata
        self.connection.execute(
            """
            INSERT INTO asset_media_info(
                asset_id, container_format, duration_seconds, bit_rate,
                video_codec, audio_codec, width, height, frame_rate,
                sample_aspect_ratio, display_aspect_ratio, pixel_format,
                field_order, audio_sample_rate, audio_channels, raw_probe_json,
                probe_completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                metadata.get("container_format"),
                metadata.get("duration_seconds"),
                metadata.get("bit_rate"),
                metadata.get("video_codec"),
                metadata.get("audio_codec"),
                metadata.get("width"),
                metadata.get("height"),
                metadata.get("frame_rate"),
                metadata.get("sample_aspect_ratio"),
                metadata.get("display_aspect_ratio"),
                metadata.get("pixel_format"),
                metadata.get("field_order"),
                metadata.get("audio_sample_rate"),
                metadata.get("audio_channels"),
                probe.raw_json,
                utc_now(),
            ),
        )

    def _record_integrity(
        self,
        asset_id: int,
        scan_run_id: int,
        probe: ProbeResult,
        start_sample: DecodeResult,
        end_sample: DecodeResult,
        *,
        skip_samples: bool,
    ) -> None:
        now = utc_now()
        scans = [
            (
                "probe",
                "ok" if probe.ok else "failed",
                probe.error_category,
                probe.error_detail,
            )
        ]
        if not skip_samples and probe.ok:
            scans.extend(
                [
                    (
                        "sample_beginning",
                        "ok" if start_sample.ok else "failed",
                        start_sample.error_category,
                        start_sample.error_detail,
                    ),
                    (
                        "sample_end",
                        "ok" if end_sample.ok else "failed",
                        end_sample.error_category,
                        end_sample.error_detail,
                    ),
                ]
            )
        self.connection.executemany(
            """
            INSERT INTO integrity_scans(
                asset_id, scan_run_id, scan_mode, status, error_category,
                error_detail, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    asset_id,
                    scan_run_id,
                    mode,
                    status,
                    category,
                    detail,
                    now,
                    now,
                )
                for mode, status, category, detail in scans
            ],
        )

    def _replace_review_items(
        self,
        asset_id: int,
        reasons: list[tuple[str, str]],
        now: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE review_items SET status = 'resolved', resolved_at = ?
            WHERE asset_id = ? AND status = 'open'
            """,
            (now, asset_id),
        )
        self.connection.executemany(
            """
            INSERT INTO review_items(
                asset_id, reason_code, detail, status, created_at
            ) VALUES (?, ?, ?, 'open', ?)
            """,
            [(asset_id, code, detail, now) for code, detail in reasons],
        )

    @staticmethod
    def _duration_class(duration: float | None) -> str | None:
        if duration is None:
            return None
        if duration < 10 * 60:
            return "short_form"
        if duration >= 50 * 60:
            return "long_duration"
        return "standard_program"
