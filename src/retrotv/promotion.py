from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import ensure_storage_root, transaction, utc_now
from retrotv.normalization import NormalizationService
from retrotv.probe import fingerprint_file, probe_media


@dataclass(frozen=True)
class PromotionResult:
    asset_id: int
    source_path: Path
    backup_path: Path
    promotion_id: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PromotionService:
    def __init__(self, connection, config: AppConfig):
        self.connection = connection
        self.config = config
        self.normalization = NormalizationService(connection, config)

    def _asset(self, asset_id: int):
        row = self.connection.execute(
            """
            SELECT a.*, n.proxy_status, n.proxy_relative_path
            FROM assets a
            LEFT JOIN normalization_profiles n ON n.asset_id = a.id
            WHERE a.id = ?
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Asset {asset_id} does not exist")
        return row

    def replace_with_backup(self, asset_id: int) -> PromotionResult:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "normalization")
        row = self._asset(asset_id)
        if row["proxy_status"] != "ready" or not row["proxy_relative_path"]:
            result = self.normalization.create_proxy(asset_id)
            if not result.ok:
                raise RuntimeError(f"Corrected staging file failed: {result.error}")
            row = self._asset(asset_id)

        media_root = self.config.storage_root("media").resolve()
        normalized_root = self.config.storage_root("normalized").resolve()
        backup_root = self.config.storage_root("originals_backup").resolve()
        source_relative = Path(row["source_relative_path"])
        source = (media_root / source_relative).resolve()
        proxy = (normalized_root / Path(row["proxy_relative_path"])).resolve()
        try:
            source.relative_to(media_root)
            proxy.relative_to(normalized_root)
        except ValueError as exc:
            raise ValueError("Asset or corrected path escaped its configured root") from exc
        if not source.is_file():
            raise FileNotFoundError(f"Working source is missing: {source}")
        if not proxy.is_file():
            raise FileNotFoundError(f"Corrected staging file is missing: {proxy}")

        corrected_relative = source_relative.with_suffix(".mp4")
        target = (media_root / corrected_relative).resolve()
        target.relative_to(media_root)
        if target != source and target.exists():
            raise FileExistsError(
                f"Corrected destination already exists and will not be overwritten: {target}"
            )
        duplicate = self.connection.execute(
            """
            SELECT id FROM assets
            WHERE source_root_id = ? AND source_relative_path = ? AND id <> ?
            """,
            (row["source_root_id"], corrected_relative.as_posix(), asset_id),
        ).fetchone()
        if duplicate:
            raise FileExistsError(
                f"Asset {duplicate['id']} already owns {corrected_relative.as_posix()}"
            )

        backup = (backup_root / source_relative).resolve()
        backup.relative_to(backup_root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        original_size = source.stat().st_size
        original_hash = sha256_file(source)
        if backup.exists():
            if not backup.is_file() or sha256_file(backup) != original_hash:
                raise FileExistsError(
                    f"Backup path already exists with different content: {backup}"
                )
        else:
            backup_partial = backup.with_name(backup.name + ".partial")
            if backup_partial.exists():
                backup_partial.unlink()
            shutil.copy2(source, backup_partial)
            if (
                backup_partial.stat().st_size != source.stat().st_size
                or sha256_file(backup_partial) != original_hash
            ):
                backup_partial.unlink(missing_ok=True)
                raise RuntimeError("Original backup verification failed")
            os.replace(backup_partial, backup)

        corrected_hash = sha256_file(proxy)
        target.parent.mkdir(parents=True, exist_ok=True)
        install_staging = target.with_name(
            f".{target.stem}.retrotv-{asset_id}.partial{target.suffix}"
        )
        if install_staging.exists():
            install_staging.unlink()
        shutil.copy2(proxy, install_staging)
        if sha256_file(install_staging) != corrected_hash:
            install_staging.unlink(missing_ok=True)
            raise RuntimeError("Corrected working-copy verification failed")

        probe = probe_media(
            install_staging,
            ffprobe=self.config.ffprobe,
            timeout_seconds=30.0,
        )
        if not probe.ok:
            install_staging.unlink(missing_ok=True)
            raise RuntimeError(f"Corrected working-copy probe failed: {probe.error_detail}")

        installed = False
        try:
            os.replace(install_staging, target)
            installed = True
            if target != source:
                source.unlink()
            stat = target.stat()
            fingerprint, algorithm = fingerprint_file(target, mode="quick")
            metadata = probe.metadata
            backup_root_id = ensure_storage_root(
                self.connection, "originals_backup", storage_type="archive"
            )
            self.connection.commit()
            with transaction(self.connection):
                # A verified original may have been manually restored and
                # intentionally reprocessed. Close any prior active promotion
                # before recording the new corrected working copy.
                self.connection.execute(
                    """
                    UPDATE normalization_promotions
                    SET status = 'restored', restored_at = ?
                    WHERE asset_id = ? AND status = 'active'
                    """,
                    (utc_now(), asset_id),
                )
                self.connection.execute(
                    """
                    UPDATE assets SET
                        source_relative_path = ?, original_filename = ?,
                        fingerprint = ?, fingerprint_algorithm = ?,
                        file_size = ?, modified_ns = ?,
                        metadata_status = 'ready', integrity_status = 'ready',
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        corrected_relative.as_posix(),
                        target.name,
                        fingerprint,
                        algorithm,
                        stat.st_size,
                        stat.st_mtime_ns,
                        utc_now(),
                        asset_id,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE asset_media_info SET
                        container_format = ?, duration_seconds = ?, bit_rate = ?,
                        video_codec = ?, audio_codec = ?, width = ?, height = ?,
                        frame_rate = ?, sample_aspect_ratio = ?,
                        display_aspect_ratio = ?, pixel_format = ?,
                        field_order = ?, audio_sample_rate = ?, audio_channels = ?,
                        raw_probe_json = ?, probe_completed_at = ?
                    WHERE asset_id = ?
                    """,
                    (
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
                        asset_id,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE normalization_profiles SET
                        analysis_status = 'analyzed',
                        detected_mode = 'native_4_3',
                        proposed_mode = 'native_4_3',
                        override_mode = 'auto',
                        confidence = 1.0,
                        crop_x = NULL, crop_y = NULL,
                        crop_width = NULL, crop_height = NULL,
                        analysis_detail = 'Corrected working copy promoted after validation.',
                        proxy_root_id = NULL, proxy_relative_path = NULL,
                        proxy_status = 'promoted', proxy_error = NULL
                    WHERE asset_id = ?
                    """,
                    (asset_id,),
                )
                self.connection.execute("""UPDATE remote_episodes SET local_path=?,
                    aspect_mode=NULL,crop_json=NULL,updated_at=? WHERE asset_id=? AND retained=1""",
                    (str(target), utc_now(), asset_id))
                promotion = self.connection.execute(
                    """
                    INSERT INTO normalization_promotions(
                        asset_id, original_source_relative_path,
                        corrected_source_relative_path, backup_root_id,
                        backup_relative_path, original_sha256, corrected_sha256,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        asset_id,
                        source_relative.as_posix(),
                        corrected_relative.as_posix(),
                        backup_root_id,
                        source_relative.as_posix(),
                        original_hash,
                        corrected_hash,
                        utc_now(),
                    ),
                )
                promotion_id = int(promotion.lastrowid)
                self.connection.execute(
                    """
                    INSERT INTO asset_content_hashes(
                        asset_id, original_sha256, working_sha256,
                        original_file_size, working_file_size,
                        working_modified_ns, hashed_at
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
                        asset_id, original_hash, corrected_hash, original_size,
                        stat.st_size, stat.st_mtime_ns, utc_now(),
                    ),
                )
        except Exception:
            if installed:
                restore_staging = source.with_name(
                    f".{source.name}.retrotv-restore.partial"
                )
                shutil.copy2(backup, restore_staging)
                os.replace(restore_staging, source)
                if target != source:
                    target.unlink(missing_ok=True)
            raise
        proxy.unlink(missing_ok=True)
        return PromotionResult(asset_id, target, backup, promotion_id)
