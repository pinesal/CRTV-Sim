"""Conservative, restartable removal of empty black tails on commercials."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from retrotv.asset_paths import checked_replace, checked_unlink
from retrotv.db import ensure_storage_root, transaction, utc_now
from retrotv.probe import fingerprint_file, probe_media
from retrotv.promotion import sha256_file


POLICY_VERSION = 1
_BLACK = re.compile(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)\s+black_duration:(\d+(?:\.\d+)?)")


class CommercialTailService:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    def pending_assets(self, *, limit: int = 25):
        from retrotv.processing import enabled
        if not enabled(self.config, "commercial_tails"):
            return []
        return self.connection.execute("""
            SELECT a.id,a.source_relative_path,a.fingerprint,a.file_size,a.modified_ns,
                   m.duration_seconds
            FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
            LEFT JOIN commercial_tail_analyses t ON t.asset_id=a.id
            WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND a.media_type='commercial' AND a.metadata_status='ready'
              AND a.integrity_status='ready' AND m.duration_seconds>=8
              AND (t.asset_id IS NULL OR t.policy_version!=?
                   OR t.source_fingerprint!=COALESCE(a.fingerprint,'')
                   OR t.status='interrupted')
            ORDER BY a.id LIMIT ?
        """, (POLICY_VERSION, max(1, limit))).fetchall()

    def _record(self, asset_id: int, fingerprint: str, status: str, *,
                trim_at: float | None = None, original_duration: float | None = None,
                corrected_duration: float | None = None, backup: str | None = None,
                original_hash: str | None = None, corrected_hash: str | None = None,
                detail: str = "") -> None:
        self.connection.execute("""
            INSERT INTO commercial_tail_analyses
            (asset_id,policy_version,source_fingerprint,status,trim_at_seconds,
             original_duration_seconds,corrected_duration_seconds,backup_relative_path,
             original_sha256,corrected_sha256,detail,analyzed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_id) DO UPDATE SET
                policy_version=excluded.policy_version,
                source_fingerprint=excluded.source_fingerprint,status=excluded.status,
                trim_at_seconds=excluded.trim_at_seconds,
                original_duration_seconds=excluded.original_duration_seconds,
                corrected_duration_seconds=excluded.corrected_duration_seconds,
                backup_relative_path=excluded.backup_relative_path,
                original_sha256=excluded.original_sha256,
                corrected_sha256=excluded.corrected_sha256,
                detail=excluded.detail,analyzed_at=excluded.analyzed_at
        """, (asset_id, POLICY_VERSION, fingerprint, status, trim_at,
              original_duration, corrected_duration, backup, original_hash,
              corrected_hash, detail[:2000], utc_now()))
        self.connection.commit()

    def _tail_start(self, source: Path, duration: float) -> tuple[float | None, str]:
        # Requiring virtually every pixel to be dark rejects centered logos or
        # lettering on black. Analyze only the end of the file, never opening
        # fades or black scenes elsewhere in a program.
        offset = max(0.0, duration - 35.0)
        command = [str(self.config.ffmpeg), "-hide_banner", "-nostdin", "-ss",
                   f"{offset:.3f}", "-i", str(source), "-vf",
                   "blackdetect=d=3:pix_th=0.06:pic_th=0.99999",
                   "-an", "-f", "null", "-"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:] or "Black-tail analysis failed")
        candidates = [(offset + float(start), offset + float(end), float(length))
                      for start, end, length in _BLACK.findall(result.stderr)]
        terminal = [(start, end) for start, end, length in candidates
                    if end >= duration - 0.35 and 3.0 <= length <= 20.0
                    and start >= 5.0]
        if not terminal:
            return None, "No high-confidence empty black tail at EOF"
        start, end = terminal[-1]
        # Leave a small amount of already-black video to avoid removing the
        # final visible frame at a fade boundary.
        trim_at = min(duration - 0.5, start + 0.15)
        if duration - trim_at < 2.75:
            return None, "Trailing black is too short to trim safely"
        return trim_at, f"Empty black tail from {start:.3f}s to {end:.3f}s"

    def analyze_asset(self, asset_id: int) -> str:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "commercial_tails")
        from retrotv.asset_paths import require_managed_asset
        require_managed_asset(self.connection, self.config, asset_id)
        row = self.connection.execute("""
            SELECT a.*,m.duration_seconds FROM assets a
            JOIN asset_media_info m ON m.asset_id=a.id WHERE a.id=?
        """, (asset_id,)).fetchone()
        if row is None or row["media_type"] != "commercial":
            raise ValueError("Only indexed commercials can be trimmed")
        media_root = self.config.storage_root("media").resolve()
        relative = Path(str(row["source_relative_path"]))
        source = (media_root / relative).resolve()
        source.relative_to(media_root)
        if not source.is_file():
            raise FileNotFoundError(source)
        fingerprint = str(row["fingerprint"] or "")
        duration = float(row["duration_seconds"])
        try:
            trim_at, detail = self._tail_start(source, duration)
            if trim_at is None:
                self._record(asset_id, fingerprint, "skipped", original_duration=duration,
                             detail=detail)
                return "skipped"
            return self._trim(row, source, relative, fingerprint, duration, trim_at, detail)
        except Exception as exc:
            self._record(asset_id, fingerprint, "failed", original_duration=duration,
                         detail=str(exc))
            raise

    def _trim(self, row, source: Path, relative: Path, fingerprint: str,
              duration: float, trim_at: float, detail: str) -> str:
        asset_id = int(row["id"])
        media_root = self.config.storage_root("media").resolve()
        backup_root = self.config.storage_root("originals_backup").resolve()
        target_relative = relative.with_suffix(".mp4")
        target = (media_root / target_relative).resolve()
        target.relative_to(media_root)
        if target != source and target.exists():
            raise FileExistsError(f"Corrected commercial destination exists: {target}")
        if target != source and self.connection.execute("""
            SELECT 1 FROM assets WHERE source_root_id=? AND source_relative_path=?
              AND id!=? LIMIT 1
        """, (row["source_root_id"], target_relative.as_posix(), asset_id)).fetchone():
            raise FileExistsError(f"Another asset owns {target_relative}")
        work = source.parent / f".{source.stem}.tail-{asset_id}-{uuid.uuid4().hex}.mp4"
        install = target.parent / f".{target.stem}.tail-install-{asset_id}.mp4"
        original_hash = sha256_file(source)
        backup_relative = Path("_commercial_pre_trim") / relative.parent / (
            f"{relative.stem}.source-{original_hash[:12]}{relative.suffix}")
        backup = (backup_root / backup_relative).resolve()
        backup.relative_to(backup_root)
        try:
            command = [str(self.config.ffmpeg), "-hide_banner", "-nostdin", "-y",
                       "-i", str(source), "-t", f"{trim_at:.3f}",
                       "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
                       "-preset", "fast", "-crf", "19", "-c:a", "aac",
                       "-b:a", "160k", "-movflags", "+faststart", str(work)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
            if result.returncode:
                raise RuntimeError(result.stderr[-1500:] or "Commercial tail conversion failed")
            probe = probe_media(work, ffprobe=self.config.ffprobe, timeout_seconds=30.0)
            if not probe.ok:
                raise RuntimeError(f"Corrected commercial failed probe: {probe.error_detail}")
            corrected_duration = float(probe.metadata["duration_seconds"])
            if not (duration - 21 < corrected_duration < duration - 2.5
                    and abs(corrected_duration - trim_at) < 0.5):
                raise RuntimeError("Corrected commercial has an unexpected duration")
            # Decode the staged file before publishing it.
            decoded = subprocess.run([str(self.config.ffmpeg), "-v", "error", "-nostdin",
                                      "-i", str(work), "-f", "null", "-"],
                                     capture_output=True, text=True, timeout=1800)
            if decoded.returncode:
                raise RuntimeError(decoded.stderr[-1500:] or "Corrected decode failed")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                if sha256_file(backup) != original_hash:
                    raise FileExistsError(f"Original backup hash differs: {backup}")
            else:
                partial = backup.with_name(backup.name + ".partial")
                shutil.copy2(source, partial)
                if sha256_file(partial) != original_hash:
                    checked_unlink(self.connection, partial, missing_ok=True)
                    raise RuntimeError("Original commercial backup verification failed")
                checked_replace(self.connection, partial, backup)
            corrected_hash = sha256_file(work)
            shutil.copy2(work, install)
            if sha256_file(install) != corrected_hash:
                raise RuntimeError("Corrected commercial install verification failed")
            checked_replace(self.connection, install, target)
            try:
                stat = target.stat()
                corrected_fingerprint, algorithm = fingerprint_file(target, mode="quick")
                backup_root_id = ensure_storage_root(
                    self.connection, "originals_backup", storage_type="archive")
                self.connection.commit()
                with transaction(self.connection):
                    self.connection.execute("""
                        UPDATE normalization_promotions SET status='restored',restored_at=?
                        WHERE asset_id=? AND status='active'
                    """, (utc_now(), asset_id))
                    self.connection.execute("""
                        UPDATE assets SET source_relative_path=?,original_filename=?,
                            fingerprint=?,fingerprint_algorithm=?,file_size=?,modified_ns=?,
                            metadata_status='ready',integrity_status='ready',last_seen_at=?
                        WHERE id=?
                    """, (target_relative.as_posix(), target.name, corrected_fingerprint,
                          algorithm, stat.st_size, stat.st_mtime_ns, utc_now(), asset_id))
                    fields = probe.metadata
                    self.connection.execute("""
                        UPDATE asset_media_info SET container_format=?,duration_seconds=?,
                            bit_rate=?,video_codec=?,audio_codec=?,width=?,height=?,
                            frame_rate=?,sample_aspect_ratio=?,display_aspect_ratio=?,
                            pixel_format=?,field_order=?,audio_sample_rate=?,
                            audio_channels=?,raw_probe_json=?,probe_completed_at=?
                        WHERE asset_id=?
                    """, (fields.get("container_format"), corrected_duration,
                          fields.get("bit_rate"), fields.get("video_codec"),
                          fields.get("audio_codec"), fields.get("width"),fields.get("height"),
                          fields.get("frame_rate"),fields.get("sample_aspect_ratio"),
                          fields.get("display_aspect_ratio"),fields.get("pixel_format"),
                          fields.get("field_order"),fields.get("audio_sample_rate"),
                          fields.get("audio_channels"),probe.raw_json,utc_now(),asset_id))
                    self.connection.execute("""
                        INSERT INTO normalization_promotions
                        (asset_id,original_source_relative_path,corrected_source_relative_path,
                         backup_root_id,backup_relative_path,original_sha256,corrected_sha256,
                         status,created_at)
                        VALUES (?,?,?,?,?,?,?,'active',?)
                    """, (asset_id,relative.as_posix(),target_relative.as_posix(),
                          backup_root_id,backup_relative.as_posix(),original_hash,
                          corrected_hash,utc_now()))
                    self.connection.execute("""
                        INSERT INTO commercial_tail_analyses
                        (asset_id,policy_version,source_fingerprint,status,trim_at_seconds,
                         original_duration_seconds,corrected_duration_seconds,
                         backup_relative_path,original_sha256,corrected_sha256,detail,analyzed_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(asset_id) DO UPDATE SET
                          policy_version=excluded.policy_version,
                          source_fingerprint=excluded.source_fingerprint,status=excluded.status,
                          trim_at_seconds=excluded.trim_at_seconds,
                          original_duration_seconds=excluded.original_duration_seconds,
                          corrected_duration_seconds=excluded.corrected_duration_seconds,
                          backup_relative_path=excluded.backup_relative_path,
                          original_sha256=excluded.original_sha256,
                          corrected_sha256=excluded.corrected_sha256,
                          detail=excluded.detail,analyzed_at=excluded.analyzed_at
                    """, (asset_id,POLICY_VERSION,corrected_fingerprint,"trimmed",trim_at,
                          duration,corrected_duration,backup_relative.as_posix(),
                          original_hash,corrected_hash,detail,utc_now()))
                if target != source:
                    try:
                        checked_unlink(self.connection, source)
                    except OSError:
                        # The corrected copy and DB are already committed. A
                        # later scan can flag this leftover original safely.
                        pass
            except Exception:
                if target == source:
                    restore = source.with_name(f".{source.name}.tail-restore.partial")
                    shutil.copy2(backup, restore)
                    checked_replace(self.connection, restore, source)
                else:
                    checked_unlink(self.connection, target, missing_ok=True)
                raise
            return "trimmed"
        finally:
            checked_unlink(self.connection, work, missing_ok=True)
            checked_unlink(self.connection, install, missing_ok=True)
