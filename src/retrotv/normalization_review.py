"""Explicitly reopen normalization review without replacing an unverified source."""
from __future__ import annotations

import os
import json
import shutil
import uuid
from pathlib import Path

from retrotv.db import transaction, utc_now
from retrotv.normalization import NormalizationService
from retrotv.probe import fingerprint_file, probe_media
from retrotv.promotion import sha256_file


class NormalizationReviewService:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    def _journal_root(self) -> Path:
        return self.config.database_path.parent / "normalization-review-recovery"

    def recover_pending(self) -> int:
        root = self.config.storage_root("media").resolve()
        recovered = 0
        for journal in self._journal_root().glob("*.json"):
            data = json.loads(journal.read_text(encoding="utf-8"))
            source, target, rollback = [(root / data[key]).resolve()
                                        for key in ("source", "target", "rollback")]
            if not all(path.is_relative_to(root) for path in (source, target, rollback)):
                raise ValueError("Re-review recovery path escapes the media root")
            row = self.connection.execute("""SELECT a.source_relative_path,n.review_requested
                FROM assets a LEFT JOIN normalization_profiles n ON n.asset_id=a.id WHERE a.id=?""",
                (data["asset_id"],)).fetchone()
            committed = bool(row and row["source_relative_path"] == data["target"] and row["review_requested"])
            if rollback.is_file():
                if sha256_file(rollback) != data["working_sha256"]:
                    raise ValueError("Re-review rollback copy failed verification; files were preserved")
                if committed:
                    if not target.is_file() or sha256_file(target) != data["original_sha256"]:
                        raise ValueError("Restored file changed before recovery; files were preserved")
                    rollback.unlink()
                else:
                    if source != target and source.exists():
                        raise ValueError("Recovery source is occupied; files were preserved")
                    if target.exists():
                        if sha256_file(target) != data["original_sha256"]:
                            raise ValueError("Recovery destination changed; files were preserved")
                        target.unlink()
                    os.replace(rollback, source)
            journal.unlink()
            recovered += 1
        return recovered

    def reopen(self, asset_id: int, *, active_path: str | None = None) -> dict:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "normalization")
        row = self.connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise ValueError("The playing asset is no longer indexed")
        root = self.config.storage_root("media").resolve()
        source = (root / row["source_relative_path"]).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("The working file must exist inside the media library")
        if active_path and source == Path(active_path).resolve():
            raise ValueError("The player must release this file before re-review")
        backup_root = self.config.storage_root("originals_backup").resolve()
        # The oldest available promotion backup precedes later conversions.
        # Never infer an original from an unrelated file with the same filename.
        backups = self.connection.execute("""
            SELECT original_source_relative_path, backup_relative_path, original_sha256, created_at, id
            FROM normalization_promotions WHERE asset_id=?
            UNION ALL
            SELECT source_relative_path, backup_relative_path, original_sha256, created_at, id
            FROM audio_normalization_promotions WHERE asset_id=?
            ORDER BY created_at, id
        """, (asset_id, asset_id)).fetchall()
        backup = None
        selected = None
        for record in backups:
            candidate = (backup_root / record["backup_relative_path"]).resolve()
            if not candidate.is_relative_to(backup_root):
                raise ValueError("Backup path escapes the configured backup root")
            if candidate.is_file():
                if sha256_file(candidate) != record["original_sha256"]:
                    raise ValueError("Original backup verification failed; the working file was not changed")
                backup, selected = candidate, record
                break
        target = source
        staging = rollback = journal = None
        probe = None
        if backup:
            target = source.with_suffix(Path(selected["original_source_relative_path"]).suffix)
            if target != source and target.exists():
                raise FileExistsError(f"Original destination already exists: {target}")
            relative = target.relative_to(root).as_posix()
            owner = self.connection.execute("""SELECT id FROM assets
                WHERE source_root_id=? AND source_relative_path=? AND id<>?""",
                (row["source_root_id"], relative, asset_id)).fetchone()
            if owner:
                raise ValueError("Another indexed asset owns the original destination")
            token = uuid.uuid4().hex
            staging = target.with_name(f".{target.name}.{token}.review-partial")
            rollback = source.with_name(f".{source.name}.{token}.review-rollback")
            try:
                shutil.copy2(backup, staging)
                if sha256_file(staging) != selected["original_sha256"]:
                    raise ValueError("Restored copy verification failed")
                probe = probe_media(staging, ffprobe=self.config.ffprobe, timeout_seconds=30)
                if not probe.ok:
                    raise ValueError(f"Original backup probe failed: {probe.error_detail}")
                self._journal_root().mkdir(parents=True, exist_ok=True)
                journal = self._journal_root() / f"{asset_id}-{token}.json"
                journal_partial = journal.with_suffix(".partial")
                journal_partial.write_text(json.dumps({"asset_id": asset_id,
                    "source": source.relative_to(root).as_posix(),
                    "target": target.relative_to(root).as_posix(),
                    "rollback": rollback.relative_to(root).as_posix(),
                    "working_sha256": sha256_file(source), "original_sha256": selected["original_sha256"]}), encoding="utf-8")
                os.replace(journal_partial, journal)
                os.replace(source, rollback)
                try:
                    os.replace(staging, target)
                except Exception:
                    os.replace(rollback, source)
                    journal.unlink(missing_ok=True)
                    raise
            finally:
                staging.unlink(missing_ok=True)
        try:
            NormalizationService(self.connection, self.config).discard_previews((asset_id,))
            fingerprint, algorithm = fingerprint_file(target, mode="quick")
            working_hash = sha256_file(target)
            stat = target.stat()
            self.connection.commit()
            with transaction(self.connection):
                if backup:
                    self.connection.execute("""UPDATE assets SET source_relative_path=?,
                        original_filename=?, file_size=?, modified_ns=?, fingerprint=?,
                        fingerprint_algorithm=? WHERE id=?""",
                        (target.relative_to(root).as_posix(), target.name, stat.st_size,
                         stat.st_mtime_ns, fingerprint, algorithm, asset_id))
                    fields = ("container_format", "duration_seconds", "bit_rate", "video_codec",
                              "audio_codec", "width", "height", "frame_rate", "sample_aspect_ratio",
                              "display_aspect_ratio", "pixel_format", "field_order",
                              "audio_sample_rate", "audio_channels")
                    self.connection.execute("UPDATE asset_media_info SET " +
                        ",".join(f"{field}=?" for field in fields) +
                        ",raw_probe_json=?,probe_completed_at=? WHERE asset_id=?",
                        (*[probe.metadata.get(field) for field in fields], probe.raw_json, utc_now(), asset_id))
                    self.connection.execute("DELETE FROM audio_normalization_profiles WHERE asset_id=?", (asset_id,))
                    self.connection.execute("""UPDATE remote_episodes SET local_path=?,
                        aspect_mode=NULL,crop_json=NULL,updated_at=? WHERE asset_id=? AND retained=1""",
                        (str(target), utc_now(), asset_id))
                for table in ("normalization_promotions", "audio_normalization_promotions"):
                    self.connection.execute(f"UPDATE {table} SET status='restored',restored_at=? "
                        "WHERE asset_id=? AND status='active'", (utc_now(), asset_id))
                # Corrections approved from this review belong to these source
                # bytes, including when the old original is no longer available.
                self.connection.execute("""INSERT INTO asset_content_hashes
                    (asset_id,original_sha256,working_sha256,original_file_size,
                     working_file_size,working_modified_ns,hashed_at) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(asset_id) DO UPDATE SET original_sha256=excluded.original_sha256,
                    working_sha256=excluded.working_sha256,original_file_size=excluded.original_file_size,
                    working_file_size=excluded.working_file_size,working_modified_ns=excluded.working_modified_ns,
                    hashed_at=excluded.hashed_at""",
                    (asset_id, working_hash, working_hash, stat.st_size, stat.st_size, stat.st_mtime_ns, utc_now()))
                self.connection.execute("DELETE FROM normalization_profiles WHERE asset_id=?", (asset_id,))
                self.connection.execute("""INSERT INTO normalization_profiles
                    (asset_id,analysis_status,review_requested,analysis_detail)
                    VALUES (?,'pending',1,'User requested a fresh normalization review.')""", (asset_id,))
        except Exception:
            if rollback and rollback.exists():
                target.unlink(missing_ok=True)
                os.replace(rollback, source)
                journal.unlink(missing_ok=True)
            raise
        if rollback:
            rollback.unlink(missing_ok=True)
        if journal:
            journal.unlink(missing_ok=True)
        normalization = NormalizationService(self.connection, self.config)
        normalization.discard_previews((asset_id,))
        result = normalization.analyze_asset(asset_id)
        return {"asset_id": asset_id, "backup_restored": bool(backup), "path": str(target),
                "error": result.detail if result.status == "error" else None,
                "review_status": result.status,
                "message": ("Fresh analysis failed; retry analysis in Library." if result.status == "error" else
                            "Original backup restored; fresh aspect review is ready." if backup else
                            "Original backup unavailable; reviewing the current working file.")}
