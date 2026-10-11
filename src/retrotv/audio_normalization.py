from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from threading import Event
from dataclasses import dataclass
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import ensure_storage_root, transaction, utc_now
from retrotv.probe import fingerprint_file, probe_media
from retrotv.promotion import sha256_file
from retrotv.process_control import run_cancellable


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


@dataclass(frozen=True)
class AudioAnalysisResult:
    asset_id: int
    status: str
    measured_i: float | None
    measured_tp: float | None
    measured_lra: float | None
    needs_correction: bool
    detail: str


@dataclass(frozen=True)
class AudioNormalizationResult:
    asset_id: int
    source_path: Path
    backup_path: Path
    promotion_id: int


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _stream_duration(raw_probe_json: str | None, codec_type: str) -> float | None:
    """Return the first real stream duration from stored FFprobe JSON."""
    if not raw_probe_json:
        return None
    try:
        payload = json.loads(raw_probe_json)
    except (TypeError, json.JSONDecodeError):
        return None
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") != codec_type:
            continue
        if codec_type == "video" and (stream.get("disposition") or {}).get("attached_pic"):
            continue
        duration = _finite(stream.get("duration"))
        if duration is not None and duration > 0:
            return duration
    return None


def _select_backup_path(
    backup: Path, backup_relative: Path, original_hash: str
) -> tuple[Path, Path]:
    """Choose a non-destructive backup path for the current source version."""
    if not backup.exists() or (backup.is_file() and sha256_file(backup) == original_hash):
        return backup, backup_relative
    versioned_name = f"{backup.stem}.source-{original_hash[:12]}{backup.suffix}"
    versioned = backup.with_name(versioned_name)
    versioned_relative = backup_relative.with_name(versioned_name)
    if versioned.exists() and (
        not versioned.is_file() or sha256_file(versioned) != original_hash
    ):
        raise FileExistsError(
            f"Versioned pre-audio backup exists with different content: {versioned}"
        )
    return versioned, versioned_relative


class AudioNormalizationService:
    """Measure EBU-style loudness and normalize audio without encoding video."""

    def __init__(
        self,
        connection,
        config: AppConfig,
        *,
        cancel_event: Event | None = None,
    ):
        self.connection = connection
        self.config = config
        self.cancel_event = cancel_event
        settings = config.data.get("audio_normalization", {})
        self.target_i = float(settings.get("target_lufs", -18.0))
        self.target_tp = float(settings.get("true_peak_dbtp", -1.5))
        self.target_lra = float(settings.get("target_lra", 11.0))
        self.tolerance = max(0.1, float(settings.get("loudness_tolerance_lu", 1.5)))
        self.audio_bitrate = max(48, int(settings.get("audio_bitrate_kbps", 128)))
        self.opus_bitrate = max(32, int(settings.get("opus_bitrate_kbps", 96)))
        self.timeout = max(60.0, float(settings.get("timeout_seconds", 43200)))
        self.duration_tolerance = max(
            0.1, float(settings.get("duration_tolerance_seconds", 1.0))
        )

    @staticmethod
    def source_signature(row) -> str:
        return f"{int(row['file_size'])}:{int(row['modified_ns'])}"

    def _asset(self, asset_id: int):
        row = self.connection.execute(
            """
            SELECT a.*, m.duration_seconds, m.video_codec, m.audio_codec,
                   m.width, m.height, m.raw_probe_json
            FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
            WHERE a.id=?
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Asset {asset_id} does not exist or has no media metadata")
        return row

    def source_path(self, row) -> Path:
        root = self.config.storage_root("media").resolve()
        path = (root / Path(row["source_relative_path"])).resolve()
        path.relative_to(root)
        return path

    def pending_assets(self, *, limit: int | None = None, force: bool = False) -> list:
        from retrotv.processing import enabled
        if not enabled(self.config, "audio_normalization"):
            return []
        values: list[object] = []
        status_sql = ""
        if not force:
            status_sql = """
              AND (
                    p.asset_id IS NULL OR p.analysis_status IN ('pending', 'analyzing', 'error') OR
                    p.source_signature !=
                      (CAST(a.file_size AS TEXT) || ':' || CAST(a.modified_ns AS TEXT))
                  )
            """
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            values.append(max(0, int(limit)))
        return self.connection.execute(
            f"""
            SELECT a.id, a.source_relative_path, a.file_size, a.modified_ns
            FROM assets a
            JOIN asset_media_info m ON m.asset_id=a.id
            LEFT JOIN audio_normalization_profiles p ON p.asset_id=a.id
            WHERE a.review_status='ready'
              AND a.metadata_status='ready'
              AND a.integrity_status IN ('ready', 'not_scanned')
              AND a.enabled_for_scheduling=1
              {status_sql}
            ORDER BY a.id
            {limit_sql}
            """,
            values,
        ).fetchall()

    def analyze_asset(self, asset_id: int) -> AudioAnalysisResult:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "audio_normalization")
        row = self._asset(asset_id)
        signature = self.source_signature(row)
        if not row["audio_codec"]:
            return self._store_analysis(
                asset_id,
                signature,
                status="not_applicable",
                detail="No audio stream was detected.",
            )
        source = self.source_path(row)
        if not source.is_file():
            raise FileNotFoundError(f"Audio source is missing: {source}")
        self.connection.execute(
            """
            INSERT INTO audio_normalization_profiles(
                asset_id, source_signature, analysis_status, target_i, target_tp,
                target_lra, approval_status, correction_status
            ) VALUES (?, ?, 'analyzing', ?, ?, ?, 'pending', 'not_needed')
            ON CONFLICT(asset_id) DO UPDATE SET
                source_signature=excluded.source_signature,
                analysis_status='analyzing', error=NULL
            """,
            (asset_id, signature, self.target_i, self.target_tp, self.target_lra),
        )
        self.connection.commit()
        loudnorm = (
            f"loudnorm=I={self.target_i}:TP={self.target_tp}:LRA={self.target_lra}:"
            "print_format=json"
        )
        args = [
            self.config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            os.fspath(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            loudnorm,
            "-f",
            "null",
            "-",
        ]
        try:
            completed = (
                run_cancellable(args, self.timeout, self.cancel_event)
                if self.cancel_event is not None
                else _run(args, self.timeout)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._analysis_error(asset_id, signature, str(exc))
        if completed.returncode:
            return self._analysis_error(
                asset_id, signature, completed.stderr.strip()[-2000:]
            )
        matches = _LOUDNORM_JSON.findall(completed.stderr)
        if not matches:
            return self._analysis_error(asset_id, signature, "FFmpeg returned no loudness data")
        try:
            measured = json.loads(matches[-1])
        except json.JSONDecodeError as exc:
            return self._analysis_error(asset_id, signature, f"Invalid loudness data: {exc}")
        measured_i = _finite(measured.get("input_i"))
        measured_tp = _finite(measured.get("input_tp"))
        measured_lra = _finite(measured.get("input_lra"))
        measured_thresh = _finite(measured.get("input_thresh"))
        target_offset = _finite(measured.get("target_offset"))
        if None in (measured_i, measured_tp, measured_lra, measured_thresh, target_offset):
            return self._store_analysis(
                asset_id,
                signature,
                status="review",
                detail="Audio was silent or too unusual for a reliable loudness measurement.",
                measured_i=measured_i,
                measured_tp=measured_tp,
                measured_lra=measured_lra,
                measured_thresh=measured_thresh,
                target_offset=target_offset,
            )
        needs = (
            abs(measured_i - self.target_i) > self.tolerance
            or measured_tp > self.target_tp
        )
        detail = (
            f"Measured {measured_i:.1f} LUFS, {measured_tp:.1f} dBTP, "
            f"LRA {measured_lra:.1f}; target {self.target_i:.1f} LUFS."
        )
        return self._store_analysis(
            asset_id,
            signature,
            status="analyzed",
            detail=detail,
            measured_i=measured_i,
            measured_tp=measured_tp,
            measured_lra=measured_lra,
            measured_thresh=measured_thresh,
            target_offset=target_offset,
            needs=needs,
        )

    def _analysis_error(
        self, asset_id: int, signature: str, error: str
    ) -> AudioAnalysisResult:
        self.connection.execute(
            """
            INSERT INTO audio_normalization_profiles(
                asset_id, source_signature, analysis_status, target_i, target_tp,
                target_lra, approval_status, correction_status, error
            ) VALUES (?, ?, 'error', ?, ?, ?, 'pending', 'not_needed', ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                source_signature=excluded.source_signature, analysis_status='error',
                approval_status='pending', correction_status='not_needed',
                error=excluded.error, analyzed_at=?
            """,
            (
                asset_id,
                signature,
                self.target_i,
                self.target_tp,
                self.target_lra,
                error,
                utc_now(),
            ),
        )
        self.connection.commit()
        return AudioAnalysisResult(asset_id, "error", None, None, None, False, error)

    def _store_analysis(
        self,
        asset_id: int,
        signature: str,
        *,
        status: str,
        detail: str,
        measured_i: float | None = None,
        measured_tp: float | None = None,
        measured_lra: float | None = None,
        measured_thresh: float | None = None,
        target_offset: float | None = None,
        needs: bool = False,
    ) -> AudioAnalysisResult:
        approval = "approved" if status == "analyzed" and needs else (
            "skipped" if status in {"analyzed", "not_applicable"} else "review"
        )
        correction = "pending" if approval == "approved" else "not_needed"
        self.connection.execute(
            """
            INSERT INTO audio_normalization_profiles(
                asset_id, source_signature, analysis_status, measured_i,
                measured_tp, measured_lra, measured_thresh, target_offset,
                target_i, target_tp, target_lra, needs_correction,
                approval_status, correction_status, detail, error, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                source_signature=excluded.source_signature,
                analysis_status=excluded.analysis_status,
                measured_i=excluded.measured_i, measured_tp=excluded.measured_tp,
                measured_lra=excluded.measured_lra,
                measured_thresh=excluded.measured_thresh,
                target_offset=excluded.target_offset, target_i=excluded.target_i,
                target_tp=excluded.target_tp, target_lra=excluded.target_lra,
                needs_correction=excluded.needs_correction,
                approval_status=excluded.approval_status,
                correction_status=excluded.correction_status,
                detail=excluded.detail, error=NULL, analyzed_at=excluded.analyzed_at
            """,
            (
                asset_id,
                signature,
                status,
                measured_i,
                measured_tp,
                measured_lra,
                measured_thresh,
                target_offset,
                self.target_i,
                self.target_tp,
                self.target_lra,
                int(needs),
                approval,
                correction,
                detail,
                utc_now(),
            ),
        )
        self.connection.commit()
        return AudioAnalysisResult(
            asset_id, status, measured_i, measured_tp, measured_lra, needs, detail
        )

    def correction_candidates(self, *, limit: int | None = None) -> list:
        from retrotv.processing import enabled
        if not enabled(self.config, "audio_normalization"):
            return []
        values: list[object] = []
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            values.append(max(0, int(limit)))
        return self.connection.execute(
            f"""
            SELECT a.id, a.source_relative_path, p.*
            FROM audio_normalization_profiles p JOIN assets a ON a.id=p.asset_id
            WHERE p.analysis_status='analyzed'
              AND p.needs_correction=1
              AND p.approval_status='approved'
              AND p.correction_status IN ('pending', 'normalizing', 'error', 'stale')
              AND a.integrity_status='ready'
              AND p.source_signature =
                  (CAST(a.file_size AS TEXT) || ':' || CAST(a.modified_ns AS TEXT))
            ORDER BY a.id
            {limit_sql}
            """,
            values,
        ).fetchall()

    def resolve_review(self, asset_id: int, action: str) -> None:
        profile = self.connection.execute(
            "SELECT * FROM audio_normalization_profiles WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if profile is None:
            raise ValueError(f"Asset {asset_id} has no audio analysis")
        if action == "retry":
            self.connection.execute(
                """
                UPDATE audio_normalization_profiles SET analysis_status='pending',
                    approval_status='pending', correction_status='not_needed',
                    error=NULL WHERE asset_id=?
                """,
                (asset_id,),
            )
        elif action == "skip":
            detail = str(profile["detail"] or "Audio review skipped.")
            if "Skipped by user" not in detail:
                detail += " Skipped by user."
            self.connection.execute(
                """
                UPDATE audio_normalization_profiles SET analysis_status='analyzed',
                    needs_correction=0, approval_status='skipped',
                    correction_status='not_needed', detail=?, error=NULL
                WHERE asset_id=?
                """,
                (detail, asset_id),
            )
        else:
            raise ValueError("Audio review action must be retry or skip")
        self.connection.commit()

    def normalize_asset(self, asset_id: int) -> AudioNormalizationResult:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "audio_normalization")
        row = self._asset(asset_id)
        profile = self.connection.execute(
            "SELECT * FROM audio_normalization_profiles WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if profile is None or profile["analysis_status"] != "analyzed":
            raise ValueError("Analyze audio before normalizing it")
        if not profile["needs_correction"] or profile["approval_status"] != "approved":
            raise ValueError("Audio normalization is not approved for this asset")
        if profile["correction_status"] == "promoted":
            raise ValueError("Audio is already normalized")
        if profile["source_signature"] != self.source_signature(row):
            self.connection.execute(
                "UPDATE audio_normalization_profiles SET correction_status='stale' WHERE asset_id=?",
                (asset_id,),
            )
            self.connection.commit()
            raise ValueError("Source changed after audio analysis; analyze it again")
        required = ("measured_i", "measured_tp", "measured_lra", "measured_thresh", "target_offset")
        if any(profile[key] is None for key in required):
            raise ValueError("Stored loudness measurements are incomplete")

        media_root = self.config.storage_root("media").resolve()
        backup_root = self.config.storage_root("originals_backup").resolve()
        source_relative = Path(row["source_relative_path"])
        source = self.source_path(row)
        if not source.is_file():
            raise FileNotFoundError(f"Audio source is missing: {source}")
        backup_relative = Path("_audio_pre_normalization") / source_relative
        backup = (backup_root / backup_relative).resolve()
        backup.relative_to(backup_root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        original_hash = sha256_file(source)
        backup, backup_relative = _select_backup_path(
            backup, backup_relative, original_hash
        )
        if backup.exists():
            # _select_backup_path verified that this is the same source version.
            pass
        else:
            backup_partial = backup.with_name(backup.name + ".partial")
            backup_partial.unlink(missing_ok=True)
            shutil.copy2(source, backup_partial)
            if sha256_file(backup_partial) != original_hash:
                backup_partial.unlink(missing_ok=True)
                raise RuntimeError("Pre-audio backup verification failed")
            os.replace(backup_partial, backup)

        partial = source.with_name(
            f".{source.stem}.retrotv-audio.partial{source.suffix.lower()}"
        )
        partial.unlink(missing_ok=True)
        self.connection.execute(
            "UPDATE audio_normalization_profiles SET correction_status='normalizing', error=NULL WHERE asset_id=?",
            (asset_id,),
        )
        self.connection.commit()
        filter_value = (
            f"loudnorm=I={profile['target_i']}:TP={profile['target_tp']}:"
            f"LRA={profile['target_lra']}:measured_I={profile['measured_i']}:"
            f"measured_TP={profile['measured_tp']}:measured_LRA={profile['measured_lra']}:"
            f"measured_thresh={profile['measured_thresh']}:offset={profile['target_offset']}:"
            "linear=true:print_format=summary"
        )
        previous_aac_failure = "aacenc.c" in str(profile["error"] or "")
        codec_args = (
            self._fallback_audio_codec_args(source.suffix.lower())
            if previous_aac_failure
            else self._audio_codec_args(source.suffix.lower())
        )
        args = [
            self.config.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            os.fspath(source),
            "-map",
            "0",
            "-map",
            "-0:d?",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c",
            "copy",
            "-filter:a:0",
            filter_value,
            *codec_args,
            os.fspath(partial),
        ]
        try:
            completed = (
                run_cancellable(args, self.timeout, self.cancel_event)
                if self.cancel_event is not None
                else _run(args, self.timeout)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._normalization_failed(asset_id, partial, str(exc))
        if (
            completed.returncode
            and not previous_aac_failure
            and "aacenc.c" in completed.stderr
        ):
            # Some malformed/legacy AAC tracks can trip an FFmpeg AAC encoder
            # assertion even though they decode normally. MP3 is valid in the
            # containers handled here and gives these rare files a safe retry.
            partial.unlink(missing_ok=True)
            fallback_codec_args = self._fallback_audio_codec_args(source.suffix.lower())
            fallback_args = args[: -len(codec_args) - 1]
            fallback_args.extend([*fallback_codec_args, os.fspath(partial)])
            try:
                completed = (
                    run_cancellable(fallback_args, self.timeout, self.cancel_event)
                    if self.cancel_event is not None
                    else _run(fallback_args, self.timeout)
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return self._normalization_failed(asset_id, partial, str(exc))
        if completed.returncode:
            return self._normalization_failed(
                asset_id, partial, completed.stderr.strip()[-2000:]
            )
        probe = probe_media(partial, ffprobe=self.config.ffprobe, timeout_seconds=60.0)
        if not probe.ok:
            return self._normalization_failed(
                asset_id, partial, f"Corrected audio probe failed: {probe.error_detail}"
            )
        metadata = probe.metadata
        source_video_duration = _stream_duration(row["raw_probe_json"], "video")
        corrected_video_duration = _stream_duration(probe.raw_json, "video")
        if source_video_duration is not None and corrected_video_duration is not None:
            duration_before = source_video_duration
            duration_after = corrected_video_duration
            duration_kind = "video stream"
        else:
            duration_before = float(row["duration_seconds"])
            duration_after = float(metadata.get("duration_seconds") or 0)
            duration_kind = "container"
        if abs(duration_after - duration_before) > self.duration_tolerance:
            return self._normalization_failed(
                asset_id,
                partial,
                f"Corrected {duration_kind} duration changed from "
                f"{duration_before:.3f}s to {duration_after:.3f}s",
            )
        if (
            metadata.get("video_codec") != row["video_codec"]
            or int(metadata.get("width") or 0) != int(row["width"] or 0)
            or int(metadata.get("height") or 0) != int(row["height"] or 0)
        ):
            return self._normalization_failed(asset_id, partial, "Video stream was not preserved")
        corrected_hash = sha256_file(partial)
        installed = False
        try:
            os.replace(partial, source)
            installed = True
            stat = source.stat()
            fingerprint, algorithm = fingerprint_file(source, mode="quick")
            backup_root_id = ensure_storage_root(
                self.connection, "originals_backup", storage_type="archive"
            )
            self.connection.commit()
            new_signature = f"{stat.st_size}:{stat.st_mtime_ns}"
            with transaction(self.connection):
                self.connection.execute(
                    """
                    UPDATE assets SET fingerprint=?, fingerprint_algorithm=?, file_size=?,
                        modified_ns=?, last_seen_at=? WHERE id=?
                    """,
                    (fingerprint, algorithm, stat.st_size, stat.st_mtime_ns, utc_now(), asset_id),
                )
                self.connection.execute(
                    """
                    UPDATE asset_media_info SET container_format=?, duration_seconds=?,
                        bit_rate=?, video_codec=?, audio_codec=?, width=?, height=?,
                        frame_rate=?, sample_aspect_ratio=?, display_aspect_ratio=?,
                        pixel_format=?, field_order=?, audio_sample_rate=?, audio_channels=?,
                        raw_probe_json=?, probe_completed_at=? WHERE asset_id=?
                    """,
                    (
                        metadata.get("container_format"), metadata.get("duration_seconds"),
                        metadata.get("bit_rate"), metadata.get("video_codec"),
                        metadata.get("audio_codec"), metadata.get("width"),
                        metadata.get("height"), metadata.get("frame_rate"),
                        metadata.get("sample_aspect_ratio"), metadata.get("display_aspect_ratio"),
                        metadata.get("pixel_format"), metadata.get("field_order"),
                        metadata.get("audio_sample_rate"), metadata.get("audio_channels"),
                        probe.raw_json, utc_now(), asset_id,
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE audio_normalization_promotions
                    SET status='restored', restored_at=?
                    WHERE asset_id=? AND status='active'
                    """,
                    (utc_now(), asset_id),
                )
                promotion = self.connection.execute(
                    """
                    INSERT INTO audio_normalization_promotions(
                        asset_id, source_relative_path, backup_root_id,
                        backup_relative_path, original_sha256, corrected_sha256,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        asset_id, source_relative.as_posix(), backup_root_id,
                        backup_relative.as_posix(), original_hash, corrected_hash, utc_now(),
                    ),
                )
                self.connection.execute(
                    """
                    UPDATE audio_normalization_profiles SET
                        source_signature=?, correction_status='promoted', error=NULL,
                        corrected_at=? WHERE asset_id=?
                    """,
                    (new_signature, utc_now(), asset_id),
                )
                self.connection.execute(
                    """
                    UPDATE asset_break_analyses SET source_signature=? WHERE asset_id=?
                    """,
                    (new_signature, asset_id),
                )
                promotion_id = int(promotion.lastrowid)
        except Exception:
            if installed:
                restore = source.with_name(f".{source.name}.retrotv-audio-restore.partial")
                shutil.copy2(backup, restore)
                os.replace(restore, source)
            raise
        return AudioNormalizationResult(asset_id, source, backup, promotion_id)

    def _audio_codec_args(self, suffix: str) -> list[str]:
        if suffix == ".webm":
            return ["-c:a:0", "libopus", "-b:a:0", f"{self.opus_bitrate}k"]
        if suffix == ".avi":
            return ["-c:a:0", "libmp3lame", "-b:a:0", f"{self.audio_bitrate}k"]
        if suffix in {".mpg", ".mpeg"}:
            return ["-c:a:0", "mp2", "-b:a:0", "192k"]
        args = [
            "-c:a:0", "aac", "-b:a:0", f"{self.audio_bitrate}k",
            "-ar:a:0", "48000", "-ac:a:0", "2",
        ]
        if suffix in {".mp4", ".m4v", ".mov"}:
            args.extend(["-movflags", "+faststart"])
        return args

    def _fallback_audio_codec_args(self, suffix: str) -> list[str]:
        if suffix in {".mp4", ".m4v", ".mov", ".avi", ".mkv"}:
            args = [
                "-c:a:0", "libmp3lame", "-b:a:0", f"{self.audio_bitrate}k",
                "-ar:a:0", "48000", "-ac:a:0", "2",
            ]
            if suffix in {".mp4", ".m4v", ".mov"}:
                args.extend(["-movflags", "+faststart"])
            return args
        return self._audio_codec_args(suffix)

    def _normalization_failed(
        self, asset_id: int, partial: Path, error: str
    ) -> AudioNormalizationResult:
        partial.unlink(missing_ok=True)
        self.connection.execute(
            """
            UPDATE audio_normalization_profiles
            SET correction_status='error', error=? WHERE asset_id=?
            """,
            (error, asset_id),
        )
        self.connection.commit()
        raise RuntimeError(error)
