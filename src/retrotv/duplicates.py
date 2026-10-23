from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from retrotv.asset_paths import (
    resolve_asset_path, is_external, require_managed_asset, checked_unlink, asset_root,
)
from retrotv.config import AppConfig
from retrotv.db import ensure_storage_root, utc_now
from retrotv.probe import probe_media


RECIPE_FIELDS = (
    "analysis_status", "detected_mode", "proposed_mode", "override_mode",
    "confidence", "crop_x", "crop_y", "crop_width", "crop_height",
    "sample_results_json", "analysis_detail", "analyzed_at",
)


@dataclass(frozen=True)
class HashBatchResult:
    processed: int
    hashed: int
    reused_analysis: int
    errors: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DuplicateService:
    """Exact source identity, duplicate review, and safe normalization reuse."""

    def __init__(self, connection, config: AppConfig):
        self.connection = connection
        self.config = config

    def normalization_signature(self) -> str:
        settings = self.config.data.get("normalization", {})
        relevant = {
            key: settings.get(key)
            for key in (
                "canvas_width", "canvas_height", "allow_upscale", "video_crf",
                "video_preset", "audio_bitrate_kbps", "maximum_output_size_ratio",
                "size_budget_safety", "size_retry_attempts",
            )
        }
        return hashlib.sha256(
            json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def record_asset(
        self,
        asset_id: int,
        path: Path | None = None,
        *,
        sha256: str | None = None,
    ) -> bool:
        row = self.connection.execute(
            "SELECT * FROM assets WHERE id=?", (asset_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Asset {asset_id} does not exist")
        source = path or (
            resolve_asset_path(self.connection, self.config, asset_id)
        )
        if not source.is_file():
            raise FileNotFoundError(f"Media file is missing: {source}")
        stat = source.stat()
        existing = self.connection.execute(
            "SELECT * FROM asset_content_hashes WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if (
            existing is not None
            and int(existing["working_file_size"]) == stat.st_size
            and int(existing["working_modified_ns"]) == stat.st_mtime_ns
        ):
            return False

        working_hash = sha256 or sha256_file(source)
        promotion = self.connection.execute(
            """
            SELECT original_sha256, corrected_sha256
            FROM normalization_promotions
            WHERE asset_id=? AND status='active'
            ORDER BY id DESC LIMIT 1
            """,
            (asset_id,),
        ).fetchone()
        audio_promotion = self.connection.execute(
            """
            SELECT original_sha256, corrected_sha256
            FROM audio_normalization_promotions
            WHERE asset_id=? AND status='active'
            ORDER BY id DESC LIMIT 1
            """,
            (asset_id,),
        ).fetchone()
        managed_replacement = promotion is not None or audio_promotion is not None
        original_hash = (
            str(existing["original_sha256"])
            if existing is not None and managed_replacement
            else str(promotion["original_sha256"])
            if promotion is not None
            else working_hash
        )
        original_size = (
            int(existing["original_file_size"])
            if existing is not None and managed_replacement
            else stat.st_size
        )
        self.connection.execute(
            """
            INSERT INTO asset_content_hashes(
                asset_id, original_sha256, working_sha256, original_file_size,
                working_file_size, working_modified_ns, hashed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                original_sha256=excluded.original_sha256,
                working_sha256=excluded.working_sha256,
                original_file_size=excluded.original_file_size,
                working_file_size=excluded.working_file_size,
                working_modified_ns=excluded.working_modified_ns,
                hashed_at=excluded.hashed_at
            """,
            (
                asset_id, original_hash, working_hash, original_size,
                stat.st_size, stat.st_mtime_ns, utc_now(),
            ),
        )
        # Older versions put quick-fingerprint matches in general library
        # review. Exact duplicates now have a dedicated, non-blocking queue.
        self.connection.execute(
            """
            UPDATE review_items SET status='resolved', resolved_at=?
            WHERE asset_id=? AND reason_code='possible_duplicate' AND status='open'
            """,
            (utc_now(), asset_id),
        )
        remaining = int(self.connection.execute(
            "SELECT COUNT(*) AS count FROM review_items WHERE asset_id=? AND status='open'",
            (asset_id,),
        ).fetchone()["count"])
        if remaining == 0 and row["review_status"] == "open":
            self.connection.execute(
                """
                UPDATE assets SET review_status='ready', enabled_for_scheduling=1
                WHERE id=? AND integrity_status IN ('ready','not_scanned')
                """,
                (asset_id,),
            )
        self.connection.commit()
        return True

    def hash_pending(self, *, limit: int = 25) -> HashBatchResult:
        rows = self.connection.execute(
            """
            SELECT a.id, a.source_relative_path
            FROM assets a
            LEFT JOIN asset_content_hashes h ON h.asset_id=a.id
            WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed')
              AND a.integrity_status IN ('ready','not_scanned')
              AND a.metadata_status='ready'
              AND (
                h.asset_id IS NULL OR h.working_file_size <> a.file_size
                OR h.working_modified_ns <> a.modified_ns
              )
            ORDER BY h.asset_id IS NOT NULL, a.id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        hashed = reused = errors = 0
        for row in rows:
            try:
                self.record_asset(int(row["id"]))
                hashed += 1
                reused += int(self.apply_normalization_recipe(int(row["id"])))
            except (OSError, ValueError):
                errors += 1
        return HashBatchResult(len(rows), hashed, reused, errors)

    def store_normalization_recipe(self, asset_id: int) -> bool:
        from retrotv.asset_paths import require_managed_asset
        require_managed_asset(self.connection, self.config, asset_id)
        row = self.connection.execute(
            """
            SELECT h.original_sha256, n.*
            FROM asset_content_hashes h
            JOIN normalization_profiles n ON n.asset_id=h.asset_id
            WHERE h.asset_id=? AND n.analysis_status IN ('analyzed','review') AND n.review_requested=0
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            return False
        profile = {field: row[field] for field in RECIPE_FIELDS}
        self.connection.execute(
            """
            INSERT INTO content_normalization_recipes(
                original_sha256, normalization_signature, profile_json,
                source_asset_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(original_sha256) DO UPDATE SET
                normalization_signature=excluded.normalization_signature,
                profile_json=excluded.profile_json,
                source_asset_id=excluded.source_asset_id,
                updated_at=excluded.updated_at
            """,
            (
                row["original_sha256"], self.normalization_signature(),
                json.dumps(profile), asset_id, utc_now(),
            ),
        )
        self.connection.commit()
        return True

    def apply_normalization_recipe(self, asset_id: int) -> bool:
        from retrotv.asset_paths import require_managed_asset
        require_managed_asset(self.connection, self.config, asset_id)
        row = self.connection.execute(
            """
            SELECT n.analysis_status, n.review_requested, r.profile_json, m.width, m.height
            FROM asset_content_hashes h
            JOIN normalization_profiles n ON n.asset_id=h.asset_id
            JOIN asset_media_info m ON m.asset_id=h.asset_id
            JOIN content_normalization_recipes r
              ON r.original_sha256=h.original_sha256
            WHERE h.asset_id=? AND r.normalization_signature=?
            """,
            (asset_id, self.normalization_signature()),
        ).fetchone()
        if row is None or row["review_requested"] or row["analysis_status"] not in {"pending", "error"}:
            return False
        profile = json.loads(row["profile_json"])
        crop_values = (
            profile.get("crop_x"), profile.get("crop_y"),
            profile.get("crop_width"), profile.get("crop_height"),
        )
        if all(value is not None for value in crop_values):
            x, y, width, height = (int(value) for value in crop_values)
            if (
                x < 0 or y < 0 or width <= 0 or height <= 0
                or x + width > int(row["width"])
                or y + height > int(row["height"])
            ):
                return False
        elif profile.get("detected_mode") in {
            "pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"
        }:
            return False
        assignments = ", ".join(f"{field}=?" for field in RECIPE_FIELDS)
        self.connection.execute(
            f"""
            UPDATE normalization_profiles SET {assignments},
                proxy_status='not_created', proxy_error=NULL
            WHERE asset_id=?
            """,
            (*[profile.get(field) for field in RECIPE_FIELDS], asset_id),
        )
        self.connection.commit()
        return True

    def prepare_reusable_proxy(self, asset_id: int) -> int | None:
        from retrotv.asset_paths import require_managed_asset
        require_managed_asset(self.connection, self.config, asset_id)
        """Stage a verified corrected copy from an exact duplicate donor."""
        target = self.connection.execute(
            """
            SELECT h.original_sha256, r.normalization_signature
            FROM asset_content_hashes h
            JOIN content_normalization_recipes r
              ON r.original_sha256=h.original_sha256
            WHERE h.asset_id=?
            """,
            (asset_id,),
        ).fetchone()
        if target is None or target["normalization_signature"] != self.normalization_signature():
            return None
        donor = self.connection.execute(
            """
            SELECT p.asset_id, p.corrected_sha256, a.source_relative_path
            FROM normalization_promotions p
            JOIN assets a ON a.id=p.asset_id
            WHERE p.original_sha256=? AND p.status='active' AND p.asset_id<>?
            ORDER BY p.id DESC LIMIT 1
            """,
            (target["original_sha256"], asset_id),
        ).fetchone()
        if donor is None:
            return None
        donor_path = self.config.storage_root("media") / Path(donor["source_relative_path"])
        if not donor_path.is_file() or sha256_file(donor_path) != donor["corrected_sha256"]:
            return None

        from retrotv.normalization import NormalizationService

        normalizer = NormalizationService(self.connection, self.config)
        asset = normalizer._asset(asset_id)
        relative = normalizer._relative_proxy_path(asset)
        output = self.config.storage_root("normalized") / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(f".{output.name}.duplicate.partial")
        partial.unlink(missing_ok=True)
        shutil.copy2(donor_path, partial)
        if sha256_file(partial) != donor["corrected_sha256"]:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Duplicate corrected-copy verification failed")
        probe = probe_media(partial, ffprobe=self.config.ffprobe, timeout_seconds=30.0)
        if not probe.ok:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"Duplicate corrected-copy probe failed: {probe.error_detail}")
        os.replace(partial, output)
        root_id = ensure_storage_root(self.connection, "normalized")
        self.connection.execute(
            """
            UPDATE normalization_profiles SET proxy_root_id=?, proxy_relative_path=?,
                proxy_status='ready', proxy_duration_seconds=?, proxy_error=NULL,
                proxy_created_at=? WHERE asset_id=?
            """,
            (
                root_id, relative.as_posix(), probe.metadata.get("duration_seconds"),
                utc_now(), asset_id,
            ),
        )
        self.connection.commit()
        return int(donor["asset_id"])

    def groups(self) -> dict[str, Any]:
        grouped = self.connection.execute(
            """
            SELECT h.original_sha256, COUNT(*) AS member_count,
                   MIN(h.original_file_size) AS original_file_size,
                   r.status AS stored_status, r.reviewed_member_count
            FROM asset_content_hashes h
            JOIN assets a ON a.id=h.asset_id
            LEFT JOIN duplicate_reviews r ON r.original_sha256=h.original_sha256
            WHERE a.integrity_status IN ('ready','not_scanned')
            GROUP BY h.original_sha256
            HAVING COUNT(*) > 1
            ORDER BY member_count DESC, h.original_sha256
            """
        ).fetchall()
        items: list[dict[str, Any]] = []
        for group in grouped:
            members = self.connection.execute(
                """
                SELECT a.id, a.source_relative_path, a.channel_slug, a.daypart_slug,
                       a.collection_type, a.collection_slug, a.display_title,
                       a.file_size, n.analysis_status, n.proxy_status,
                       r.source_kind
                FROM asset_content_hashes h
                JOIN assets a ON a.id=h.asset_id
                JOIN storage_roots r ON r.id=a.source_root_id
                LEFT JOIN normalization_profiles n ON n.asset_id=a.id
                WHERE h.original_sha256=?
                  AND a.integrity_status IN ('ready','not_scanned')
                ORDER BY a.source_relative_path
                """,
                (group["original_sha256"],),
            ).fetchall()
            intentional = (
                group["stored_status"] == "intentional"
                and int(group["reviewed_member_count"] or 0) == int(group["member_count"])
            )
            items.append(
                {
                    "sha256": group["original_sha256"],
                    "member_count": int(group["member_count"]),
                    "status": "intentional" if intentional else "pending",
                    "duplicate_bytes": int(group["original_file_size"] or 0)
                    * (int(group["member_count"]) - 1),
                    "members": [dict(member) for member in members],
                }
            )
        hashed = int(self.connection.execute(
            "SELECT COUNT(*) AS count FROM asset_content_hashes"
        ).fetchone()["count"])
        total = int(self.connection.execute(
            "SELECT COUNT(*) AS count FROM assets WHERE integrity_status IN ('ready','not_scanned')"
        ).fetchone()["count"])
        return {
            "hashed": hashed,
            "total": total,
            "groups": items,
            "pending": sum(item["status"] == "pending" for item in items),
            "intentional": sum(item["status"] == "intentional" for item in items),
            "duplicate_bytes": sum(item["duplicate_bytes"] for item in items),
        }

    def deletion_preview(self, asset_id: int, sha256: str) -> dict[str, Any]:
        """Confirm one physical copy, never a hash-wide or placement-wide delete."""
        if not re_full_sha256(sha256):
            raise ValueError("Invalid duplicate hash")
        rows = self.connection.execute(
            """SELECT a.id,a.source_relative_path,h.working_sha256
               FROM assets a JOIN asset_content_hashes h ON h.asset_id=a.id
               WHERE h.original_sha256=? AND a.integrity_status IN ('ready','not_scanned')
               ORDER BY a.id""", (sha256,),
        ).fetchall()
        selected = next((row for row in rows if row['id'] == asset_id), None)
        if selected is None or len(rows) < 2:
            raise ValueError("Duplicate set changed. Refresh duplicates and try again.")
        path = require_managed_asset(self.connection, self.config, asset_id)
        root, _ = asset_root(self.connection, self.config, asset_id)
        logical_path = root / selected['source_relative_path']
        if os.path.normcase(os.path.abspath(logical_path)) != os.path.normcase(str(path)):
            raise ValueError("This placement uses a junction or symbolic link. Remove the alias manually instead.")
        if not path.is_file():
            raise ValueError("This copy is missing. Rescan the library first.")
        identities, remaining = [], []
        for row in rows:
            other = resolve_asset_path(self.connection, self.config, int(row['id']))
            if row['id'] != asset_id and other == path:
                raise ValueError("These placements point to the same physical file; remove the alias manually instead.")
            stat = other.stat() if other.is_file() else None
            identities.append((row['id'], str(other), row['working_sha256'],
                               stat.st_size if stat else None, stat.st_mtime_ns if stat else None))
            if row['id'] != asset_id and stat:
                remaining.append({'id': row['id'], 'path': str(other)})
        if not remaining:
            raise ValueError("No other accessible copy remains. Restore or reconnect another copy first.")
        shadows = [dict(row) for row in self.connection.execute(
            "SELECT id,shadow_relative_path FROM shadow_files WHERE target_asset_id=? ORDER BY id",
            (asset_id,),
        ).fetchall()]
        identity = {'asset_id': asset_id, 'files': identities, 'shadows': shadows}
        return {
            'asset_id': asset_id, 'sha256': sha256, 'path': str(path),
            'relative_path': selected['source_relative_path'],
            'remaining': remaining, 'shadows': shadows,
            'token': hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        }

    def delete_copy(self, asset_id: int, sha256: str, token: str) -> dict[str, Any]:
        """Retain history and backups; retire only the explicitly confirmed file."""
        # Reserve the writer before filesystem deletion: a busy DB must never
        # produce a removed file with an unmodified catalog.
        if self.connection.in_transaction:
            raise ValueError("Finish the current database transaction before deleting media")
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            preview = self.deletion_preview(asset_id, sha256)
            if not isinstance(token, str) or token != preview['token']:
                raise ValueError("The files or placements changed. Preview deletion again.")
            self.connection.execute(
                """UPDATE assets SET integrity_status='missing',review_status='ignored',
                   enabled_for_scheduling=0 WHERE id=?""", (asset_id,),
            )
            self.connection.execute(
                "UPDATE review_items SET status='resolved',resolved_at=? WHERE asset_id=? AND status='open'",
                (utc_now(), asset_id),
            )
            self.connection.execute(
                """UPDATE shadow_files SET status='review',detail='Target media was deleted during duplicate review'
                   WHERE target_asset_id=?""", (asset_id,),
            )
            # Recheck ownership at the actual write boundary, including aliases
            # into registered external roots. Do not touch backups or sidecars.
            path = require_managed_asset(self.connection, self.config, asset_id)
            checked_unlink(self.connection, path)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return {'deleted': preview['path'], 'asset_id': asset_id,
                'affected_shadows': len(preview['shadows'])}

    def review(self, sha256: str, action: str) -> None:
        if not re_full_sha256(sha256):
            raise ValueError("Invalid duplicate hash")
        if action not in {"intentional", "pending"}:
            raise ValueError("Invalid duplicate review action")
        count = int(self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM asset_content_hashes h
            JOIN assets a ON a.id=h.asset_id
            WHERE h.original_sha256=? AND a.integrity_status IN ('ready','not_scanned')
            """,
            (sha256,),
        ).fetchone()["count"])
        if count < 2:
            raise ValueError("Duplicate group no longer exists")
        self.connection.execute(
            """
            INSERT INTO duplicate_reviews(
                original_sha256, status, reviewed_member_count, reviewed_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(original_sha256) DO UPDATE SET status=excluded.status,
                reviewed_member_count=excluded.reviewed_member_count,
                reviewed_at=excluded.reviewed_at
            """,
            (sha256, action, count, utc_now()),
        )
        self.connection.commit()


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
