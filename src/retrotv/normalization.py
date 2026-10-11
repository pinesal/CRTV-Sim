from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
from threading import Event
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from retrotv.config import AppConfig
from retrotv.db import ensure_storage_root, utc_now
from retrotv.probe import decode_sample, probe_media
from retrotv.process_control import run_cancellable


MODES = {
    "auto",
    "native_4_3",
    "pillarboxed_4_3",
    "pillarboxed_stretched_4_3",
    "stretched_4_3",
    "preserve_frame",
    "custom",
}
_CROP_RE = re.compile(r"\bcrop=(\d+):(\d+):(\d+):(\d+)")


@dataclass(frozen=True)
class Crop:
    width: int
    height: int
    x: int
    y: int

    @property
    def ratio(self) -> float:
        return self.width / self.height if self.height else 0.0

    def as_dict(self) -> dict[str, int]:
        return {
            "width": self.width,
            "height": self.height,
            "x": self.x,
            "y": self.y,
        }


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    detected_mode: str
    proposed_mode: str
    confidence: float
    crop: Crop | None
    samples: list[dict[str, Any]]
    detail: str


@dataclass(frozen=True)
class ProxyResult:
    ok: bool
    path: Path | None
    error: str | None = None
    warning: str | None = None


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


def _ratio(value: str | None) -> float | None:
    if not value or value in {"0:1", "N/A"}:
        return None
    separator = ":" if ":" in value else "/"
    try:
        left, right = value.split(separator, 1)
        denominator = float(right)
        return float(left) / denominator if denominator else None
    except (ValueError, TypeError):
        return None


def display_ratio(media: dict[str, Any]) -> float | None:
    declared = _ratio(media.get("display_aspect_ratio"))
    if declared:
        return declared
    width = media.get("width")
    height = media.get("height")
    if not width or not height:
        return None
    sar = _ratio(media.get("sample_aspect_ratio")) or 1.0
    return float(width) * sar / float(height)


def sample_offsets(duration: float, count: int) -> list[float]:
    if duration <= 0:
        return [0.0]
    count = max(1, count)
    # Avoid titles, fades, and end credits while spreading samples across a block.
    return [duration * (index + 1) / (count + 1) for index in range(count)]


def parse_cropdetect(stderr: str) -> Crop | None:
    matches = _CROP_RE.findall(stderr)
    if not matches:
        return None
    width, height, x, y = (int(value) for value in matches[-1])
    return Crop(width, height, x, y)


class NormalizationAnalyzer:
    def __init__(self, config: AppConfig, *, cancel_event: Event | None = None):
        self.config = config
        self.cancel_event = cancel_event
        settings = config.data.get("normalization", {})
        self.sample_count = int(settings.get("sample_count", 5))
        self.sample_seconds = float(settings.get("analysis_sample_seconds", 1.0))
        self.timeout = float(settings.get("analysis_timeout_seconds", 45.0))
        self.crop_limit = int(settings.get("cropdetect_limit", 24))
        self.ratio_tolerance = float(settings.get("ratio_tolerance", 0.06))

    def detect_crop(self, path: Path, offset: float) -> Crop | None:
        args = [
            self.config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-ss",
            f"{max(0.0, offset):.3f}",
            "-i",
            os.fspath(path),
            "-t",
            f"{self.sample_seconds:.3f}",
            "-an",
            "-vf",
            f"cropdetect=limit={self.crop_limit}:round=2:reset=0",
            "-f",
            "null",
            "-",
        ]
        completed = (
            run_cancellable(args, self.timeout, self.cancel_event)
            if self.cancel_event is not None
            else _run(args, self.timeout)
        )
        if completed.returncode != 0:
            return None
        return parse_cropdetect(completed.stderr)

    def analyze(
        self,
        path: Path,
        media: dict[str, Any],
        *,
        crop_provider: Callable[[Path, float], Crop | None] | None = None,
    ) -> AnalysisResult:
        width = int(media.get("width") or 0)
        height = int(media.get("height") or 0)
        duration = float(media.get("duration_seconds") or 0.0)
        if width <= 0 or height <= 0:
            return AnalysisResult(
                "error", "ambiguous", "preserve_frame", 0.0, None, [],
                "Video dimensions are unavailable.",
            )

        provider = crop_provider or self.detect_crop
        samples: list[dict[str, Any]] = []
        crops: list[Crop] = []
        for offset in sample_offsets(duration, self.sample_count):
            crop = provider(path, offset)
            if crop:
                crops.append(crop)
            samples.append(
                {"offset_seconds": round(offset, 3), "crop": crop.as_dict() if crop else None}
            )

        dar = display_ratio(media) or width / height
        significant: list[Crop] = []
        for crop in crops:
            left = crop.x
            right = width - crop.width - crop.x
            horizontal_border = left + right
            if horizontal_border >= width * 0.04:
                significant.append(crop)

        pillar_candidates = [
            crop
            for crop in significant
            if abs(crop.ratio - 4 / 3) <= self.ratio_tolerance
            and abs(crop.x - (width - crop.width - crop.x)) <= max(8, width * 0.03)
            and crop.y <= max(8, height * 0.03)
        ]
        support = len(pillar_candidates) / max(1, len(samples))
        if support >= 0.6:
            key_counts = Counter(
                (crop.width, crop.height, crop.x, crop.y) for crop in pillar_candidates
            )
            chosen = Crop(*key_counts.most_common(1)[0][0])
            return AnalysisResult(
                "analyzed",
                "pillarboxed_4_3",
                "pillarboxed_4_3",
                min(0.99, 0.55 + support * 0.4),
                chosen,
                samples,
                f"Symmetrical pillarbox borders produced a 4:3 active image in "
                f"{len(pillar_candidates)}/{len(samples)} samples.",
            )

        if abs(dar - 4 / 3) <= self.ratio_tolerance and not significant:
            return AnalysisResult(
                "analyzed",
                "native_4_3",
                "native_4_3",
                0.95,
                None,
                samples,
                "Display aspect ratio is 4:3 and persistent side borders were not detected.",
            )

        border_support = len(significant) / max(1, len(samples))
        half_width_ratio = (width / 2) / height
        if (
            dar >= 2.2
            and abs(half_width_ratio - 4 / 3) <= self.ratio_tolerance
            and border_support <= 0.2
        ):
            return AnalysisResult(
                "analyzed",
                "stretched_4_3",
                "stretched_4_3",
                0.90,
                None,
                samples,
                "The full frame is an exact doubled-width 4:3 pattern "
                f"({width}x{height} restores to {width // 2}x{height}).",
            )

        if abs(dar - 16 / 9) <= 0.10 and border_support <= 0.2:
            return AnalysisResult(
                "review",
                "ambiguous",
                "stretched_4_3",
                0.35,
                None,
                samples,
                "Full-frame 16:9 could be genuine widescreen or stretched 4:3. "
                "Choose stretched 4:3 only after checking the preview.",
            )

        return AnalysisResult(
            "review",
            "ambiguous",
            "preserve_frame",
            0.25,
            None,
            samples,
            "Geometry was not consistent enough for an automatic crop.",
        )


class NormalizationService:
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
        self.analyzer = NormalizationAnalyzer(config, cancel_event=cancel_event)
        settings = config.data.get("normalization", {})
        self.canvas_width = int(settings.get("canvas_width", 640))
        self.canvas_height = int(settings.get("canvas_height", 480))
        self.allow_upscale = bool(settings.get("allow_upscale", False))
        self.crf = int(settings.get("video_crf", 20))
        self.preset = str(settings.get("video_preset", "medium"))
        self.maximum_output_size_ratio = max(
            1.0,
            float(settings.get("maximum_output_size_ratio", 1.10)),
        )
        self.size_budget_safety = min(
            0.98,
            max(0.50, float(settings.get("size_budget_safety", 0.92))),
        )
        self.size_retry_attempts = max(
            0,
            int(settings.get("size_retry_attempts", 2)),
        )
        self.accept_oversize_after_retries = bool(
            settings.get("accept_oversize_after_retries", True)
        )
        self.audio_bitrate_kbps = max(
            48,
            int(settings.get("audio_bitrate_kbps", 128)),
        )
        self.timeout = float(settings.get("encode_timeout_seconds", 43200))
        self.duration_tolerance = float(settings.get("duration_tolerance_seconds", 1.0))

    def _asset(self, asset_id: int):
        row = self.connection.execute(
            """
            SELECT a.*, m.duration_seconds, m.bit_rate, m.width, m.height,
                   m.sample_aspect_ratio, m.display_aspect_ratio
            FROM assets a
            JOIN asset_media_info m ON m.asset_id = a.id
            WHERE a.id = ?
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Asset {asset_id} does not exist or has no media metadata")
        return row

    def source_path(self, row) -> Path:
        return self.config.storage_root("media") / Path(row["source_relative_path"])

    def analyze_asset(
        self,
        asset_id: int,
        *,
        crop_provider: Callable[[Path, float], Crop | None] | None = None,
    ) -> AnalysisResult:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "normalization")
        row = self._asset(asset_id)
        if self.config.data.get("normalization", {}).get("aspect_policy", "retro_4_3") == "preserve":
            result = AnalysisResult("analyzed", "preserve_frame", "preserve_frame", 1.0,
                                    None, [], "Source aspect is preserved; only the resolution ceiling applies.")
        else:
            result = self.analyzer.analyze(
                self.source_path(row), dict(row), crop_provider=crop_provider
            )
        profile = self._profile(asset_id)
        if profile and profile["review_requested"] and result.status != "error":
            result = replace(result, status="review",
                             detail="Re-review requested. " + result.detail)
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(
                asset_id, analysis_status, detected_mode, proposed_mode,
                confidence, crop_x, crop_y, crop_width, crop_height,
                sample_results_json, analysis_detail, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                analysis_status = excluded.analysis_status,
                detected_mode = excluded.detected_mode,
                proposed_mode = excluded.proposed_mode,
                confidence = excluded.confidence,
                crop_x = excluded.crop_x,
                crop_y = excluded.crop_y,
                crop_width = excluded.crop_width,
                crop_height = excluded.crop_height,
                sample_results_json = excluded.sample_results_json,
                analysis_detail = excluded.analysis_detail,
                analyzed_at = excluded.analyzed_at,
                normalized_preview_relative_path = CASE
                    WHEN COALESCE(detected_mode, '') <>
                         COALESCE(excluded.detected_mode, '')
                      OR COALESCE(proposed_mode, '') <>
                         COALESCE(excluded.proposed_mode, '')
                      OR COALESCE(crop_x, -1) <> COALESCE(excluded.crop_x, -1)
                      OR COALESCE(crop_y, -1) <> COALESCE(excluded.crop_y, -1)
                      OR COALESCE(crop_width, -1) <>
                         COALESCE(excluded.crop_width, -1)
                      OR COALESCE(crop_height, -1) <>
                         COALESCE(excluded.crop_height, -1)
                    THEN NULL
                    ELSE normalized_preview_relative_path
                END,
                proxy_status = CASE
                    WHEN proxy_status IN ('ready', 'promoted') AND (
                         COALESCE(detected_mode, '') <>
                         COALESCE(excluded.detected_mode, '')
                      OR COALESCE(proposed_mode, '') <>
                         COALESCE(excluded.proposed_mode, '')
                      OR COALESCE(crop_x, -1) <> COALESCE(excluded.crop_x, -1)
                      OR COALESCE(crop_y, -1) <> COALESCE(excluded.crop_y, -1)
                      OR COALESCE(crop_width, -1) <>
                         COALESCE(excluded.crop_width, -1)
                      OR COALESCE(crop_height, -1) <>
                         COALESCE(excluded.crop_height, -1)
                    ) THEN 'stale'
                    ELSE proxy_status
                END
            """,
            (
                asset_id,
                result.status,
                result.detected_mode,
                result.proposed_mode,
                result.confidence,
                result.crop.x if result.crop else None,
                result.crop.y if result.crop else None,
                result.crop.width if result.crop else None,
                result.crop.height if result.crop else None,
                json.dumps(result.samples),
                result.detail,
                utc_now(),
            ),
        )
        self.connection.commit()
        from retrotv.duplicates import DuplicateService
        DuplicateService(self.connection, self.config).store_normalization_recipe(asset_id)
        return result

    def reanalyze_asset_from_stored_samples(
        self, asset_id: int
    ) -> AnalysisResult:
        profile = self._profile(asset_id)
        if profile is None or not profile["sample_results_json"]:
            raise ValueError(f"Asset {asset_id} has no stored normalization samples")
        try:
            stored = json.loads(profile["sample_results_json"])
            crops = []
            for item in stored:
                raw_crop = item.get("crop")
                crops.append(
                    Crop(
                        int(raw_crop["width"]),
                        int(raw_crop["height"]),
                        int(raw_crop["x"]),
                        int(raw_crop["y"]),
                    )
                    if raw_crop
                    else None
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Asset {asset_id} has invalid stored normalization samples"
            ) from exc
        if len(crops) != self.analyzer.sample_count:
            raise ValueError(
                f"Asset {asset_id} has {len(crops)} stored samples; "
                f"{self.analyzer.sample_count} are configured"
            )
        iterator = iter(crops)
        return self.analyze_asset(
            asset_id,
            crop_provider=lambda path, offset: next(iterator),
        )

    def set_override(
        self,
        asset_id: int,
        mode: str,
        *,
        crop: Crop | None = None,
    ) -> None:
        self._validate_override(asset_id, mode, crop)
        self._store_override(asset_id, mode, crop)
        self.connection.commit()

    def _validate_override(
        self,
        asset_id: int,
        mode: str,
        crop: Crop | None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown normalization mode: {mode}")
        if crop is not None:
            row = self._asset(asset_id)
            if (
                crop.width <= 0
                or crop.height <= 0
                or crop.x < 0
                or crop.y < 0
                or crop.x + crop.width > int(row["width"])
                or crop.y + crop.height > int(row["height"])
            ):
                raise ValueError(
                    f"Crop {crop.width}:{crop.height}:{crop.x}:{crop.y} "
                    f"is outside the {row['width']}x{row['height']} source frame"
                )
        if mode in {"custom", "pillarboxed_4_3", "pillarboxed_stretched_4_3"} and crop is None:
            profile = self._profile(asset_id)
            values = (
                profile["crop_width"] if profile else None,
                profile["crop_height"] if profile else None,
                profile["crop_x"] if profile else None,
                profile["crop_y"] if profile else None,
            )
            if any(value is None for value in values):
                raise ValueError(f"{mode} requires a crop rectangle")

    def _store_override(
        self,
        asset_id: int,
        mode: str,
        crop: Crop | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO normalization_profiles(
                asset_id, override_mode, crop_x, crop_y, crop_width, crop_height
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                override_mode = excluded.override_mode,
                -- A crop rectangle only belongs to a crop-based correction.
                -- Retaining it after a user chooses native/preserve/stretch can
                -- make a later conversion try to crop outside the source frame.
                crop_x = CASE
                    WHEN excluded.override_mode IN ('pillarboxed_4_3', 'pillarboxed_stretched_4_3', 'custom')
                    THEN COALESCE(excluded.crop_x, crop_x)
                    ELSE NULL
                END,
                crop_y = CASE
                    WHEN excluded.override_mode IN ('pillarboxed_4_3', 'pillarboxed_stretched_4_3', 'custom')
                    THEN COALESCE(excluded.crop_y, crop_y)
                    ELSE NULL
                END,
                crop_width = CASE
                    WHEN excluded.override_mode IN ('pillarboxed_4_3', 'pillarboxed_stretched_4_3', 'custom')
                    THEN COALESCE(excluded.crop_width, crop_width)
                    ELSE NULL
                END,
                crop_height = CASE
                    WHEN excluded.override_mode IN ('pillarboxed_4_3', 'pillarboxed_stretched_4_3', 'custom')
                    THEN COALESCE(excluded.crop_height, crop_height)
                    ELSE NULL
                END,
                proxy_status = CASE
                    WHEN override_mode <> excluded.override_mode
                      OR COALESCE(crop_x, -1) <> COALESCE(excluded.crop_x, crop_x, -1)
                      OR COALESCE(crop_y, -1) <> COALESCE(excluded.crop_y, crop_y, -1)
                      OR COALESCE(crop_width, -1) <> COALESCE(excluded.crop_width, crop_width, -1)
                      OR COALESCE(crop_height, -1) <> COALESCE(excluded.crop_height, crop_height, -1)
                      OR (
                          excluded.override_mode NOT IN ('pillarboxed_4_3', 'pillarboxed_stretched_4_3', 'custom')
                          AND (
                              crop_x IS NOT NULL OR crop_y IS NOT NULL
                              OR crop_width IS NOT NULL OR crop_height IS NOT NULL
                          )
                      )
                    THEN 'stale'
                    ELSE proxy_status
                END
            """,
            (
                asset_id,
                mode,
                crop.x if crop else None,
                crop.y if crop else None,
                crop.width if crop else None,
                crop.height if crop else None,
            ),
        )

    def approve_folder_review(
        self,
        asset_id: int,
        mode: str,
        *,
        crop: Crop | None = None,
    ) -> dict[str, Any]:
        """Apply one correction to every aspect-review asset in an exact folder."""
        if mode == "auto":
            raise ValueError("Choose a specific correction before applying it to a folder")
        source = self._asset(asset_id)
        source_relative = str(source["source_relative_path"]).replace("\\", "/")
        folder = PurePosixPath(source_relative).parent
        rows = self.connection.execute(
            """
            SELECT a.id, a.source_relative_path
            FROM assets a
            JOIN asset_media_info m ON m.asset_id = a.id
            JOIN normalization_profiles n ON n.asset_id = a.id
            WHERE a.source_root_id = ?
              AND n.analysis_status = 'review'
              AND m.width IS NOT NULL AND m.height IS NOT NULL
            ORDER BY a.source_relative_path
            """,
            (source["source_root_id"],),
        ).fetchall()
        targets = [
            row for row in rows
            if PurePosixPath(
                str(row["source_relative_path"]).replace("\\", "/")
            ).parent == folder
        ]
        if not targets:
            raise ValueError(f"No aspect-review videos remain in {folder.as_posix()}")

        # Validate every target before changing any of them. This is especially
        # important for a crop copied across files with differing resolutions.
        for row in targets:
            try:
                self._validate_override(int(row["id"]), mode, crop)
            except ValueError as exc:
                raise ValueError(
                    f"Folder correction is incompatible with "
                    f"{row['source_relative_path']}: {exc}"
                ) from exc

        self.connection.execute("SAVEPOINT normalization_folder_review")
        try:
            for row in targets:
                target_id = int(row["id"])
                self._store_override(target_id, mode, crop)
                self.video_filter(self._profile(target_id))
                self.connection.execute(
                    """
                    UPDATE normalization_profiles
                    SET analysis_status = 'analyzed', review_requested = 0
                    WHERE asset_id = ?
                    """,
                    (target_id,),
                )
            self.connection.execute("RELEASE normalization_folder_review")
            self.connection.commit()
        except Exception:
            self.connection.execute("ROLLBACK TO normalization_folder_review")
            self.connection.execute("RELEASE normalization_folder_review")
            raise

        from retrotv.duplicates import DuplicateService
        duplicates = DuplicateService(self.connection, self.config)
        for row in targets:
            duplicates.store_normalization_recipe(int(row["id"]))
        self.discard_previews(int(row["id"]) for row in targets)
        return {
            "folder": folder.as_posix(),
            "updated_count": len(targets),
            "asset_ids": [int(row["id"]) for row in targets],
            "override_mode": mode,
        }

    def approve_review(self, asset_id: int) -> str:
        profile = self._profile(asset_id)
        if profile is None:
            raise ValueError(f"Asset {asset_id} has not been analyzed")

        mode = profile["override_mode"]
        if mode == "auto":
            mode = profile["proposed_mode"]
            if mode not in MODES or mode == "auto":
                raise ValueError(
                    "Choose a normalization mode before approving this aspect review"
                )
            # Validate the proposed filter, including any required crop rectangle,
            # before freezing it as the user's approved override.
            self.video_filter(profile, allow_proposed=True)
            self.set_override(asset_id, mode)
            profile = self._profile(asset_id)
        else:
            self.video_filter(profile)

        self.connection.execute(
            """
            UPDATE normalization_profiles
            SET analysis_status = 'analyzed', review_requested = 0
            WHERE asset_id = ?
            """,
            (asset_id,),
        )
        self.connection.commit()
        from retrotv.duplicates import DuplicateService
        DuplicateService(self.connection, self.config).store_normalization_recipe(asset_id)
        self.discard_previews((asset_id,))
        return str(mode)

    def _preview_recipe_path(self, asset_id: int) -> Path:
        return self.config.storage_root("previews") / f"{asset_id}-normalized.preview.json"

    @staticmethod
    def _preview_crop(profile) -> Crop | None:
        values = (
            profile.get("crop_width"), profile.get("crop_height"),
            profile.get("crop_x"), profile.get("crop_y"),
        )
        if any(value is None for value in values):
            return None
        return Crop(int(values[0]), int(values[1]), int(values[2]), int(values[3]))

    def _draft_profile(self, asset_id: int, mode: str, crop: Crop | None) -> dict[str, Any]:
        self._validate_override(asset_id, mode, crop)
        stored = self._profile(asset_id)
        if stored is None:
            raise ValueError(f"Asset {asset_id} has not been analyzed")
        draft = dict(stored)
        draft["override_mode"] = mode
        if mode in {"pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"}:
            selected_crop = crop or self._preview_crop(draft)
            if selected_crop is None:
                raise ValueError(f"{mode} requires a crop rectangle")
            draft.update(
                crop_width=selected_crop.width,
                crop_height=selected_crop.height,
                crop_x=selected_crop.x,
                crop_y=selected_crop.y,
            )
        else:
            draft.update(crop_width=None, crop_height=None, crop_x=None, crop_y=None)
        return draft

    def _preview_token(
        self, row, mode: str, crop: Crop | None, video_filter: str
    ) -> str:
        payload = {
            "asset_id": int(row["id"]),
            "file_size": int(row["file_size"] or 0),
            "modified_ns": int(row["modified_ns"] or 0),
            "mode": mode,
            "crop": crop.as_dict() if crop else None,
            "video_filter": video_filter,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _render_preview_frame(
        self,
        row,
        target: Path,
        *,
        video_filter: str | None = None,
        position: float = 0.35,
    ) -> None:
        duration = max(0.0, float(row["duration_seconds"] or 0.0))
        offset = min(duration * min(0.95, max(0.05, position)), max(0.0, duration - 0.1))
        partial = target.with_name(f"{target.stem}.partial{target.suffix}")
        args = [
            self.config.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-nostdin", "-ss", f"{offset:.3f}", "-i", os.fspath(self.source_path(row)),
            "-frames:v", "1",
        ]
        if video_filter:
            args.extend(("-vf", video_filter))
        args.append(os.fspath(partial))
        completed = (
            run_cancellable(args, 60.0, self.cancel_event)
            if self.cancel_event is not None
            else _run(args, 60.0)
        )
        if completed.returncode:
            partial.unlink(missing_ok=True)
            raise RuntimeError(completed.stderr.strip()[-1000:])
        partial.replace(target)

    def _source_preview_filter(self, row) -> str:
        ratio = display_ratio(dict(row))
        if not ratio or ratio <= 0:
            ratio = float(row["width"]) / float(row["height"])
        maximum_width = max(2, self.canvas_width)
        maximum_height = max(2, self.canvas_height)
        if ratio >= maximum_width / maximum_height:
            width = maximum_width
            height = max(2, round(width / ratio))
        else:
            height = maximum_height
            width = max(2, round(height * ratio))
        width -= width % 2
        height -= height % 2
        return f"scale={width}:{height},setsar=1"

    def create_source_preview(self, asset_id: int) -> Path:
        """Create the display-correct original frame used by Aspect Review."""
        row = self._asset(asset_id)
        root = self.config.storage_root("previews")
        root.mkdir(parents=True, exist_ok=True)
        relative = Path(f"{asset_id}-source.jpg")
        target = root / relative
        self._render_preview_frame(row, target, video_filter=self._source_preview_filter(row))
        self.connection.execute(
            """
            UPDATE normalization_profiles
            SET source_preview_relative_path = ?
            WHERE asset_id = ?
            """,
            (relative.as_posix(), asset_id),
        )
        self.connection.commit()
        return target

    def create_corrected_preview(
        self,
        asset_id: int,
        mode: str,
        *,
        crop: Crop | None = None,
        frame_count: int = 1,
    ) -> dict[str, Any]:
        """Render a correction draft without changing the stored override or approval."""
        if mode == "auto":
            raise ValueError("Choose a specific correction before previewing it")
        row = self._asset(asset_id)
        draft = self._draft_profile(asset_id, mode, crop)
        selected_crop = self._preview_crop(draft)
        target_dimensions = self.output_canvas(row, draft)
        video_filter = self.video_filter(draft, target_dimensions=target_dimensions)
        root = self.config.storage_root("previews")
        root.mkdir(parents=True, exist_ok=True)
        source = root / f"{asset_id}-source.jpg"
        if not source.is_file():
            source = self.create_source_preview(asset_id)
        relative = Path(f"{asset_id}-normalized.jpg")
        normalized = root / relative
        self._render_preview_frame(row, normalized, video_filter=video_filter)
        frame_count = 3 if int(frame_count) == 3 else 1
        comparison_frames: list[dict[str, str]] = []
        if frame_count == 3:
            source_filter = self._source_preview_filter(row)
            for index, position in enumerate((0.25, 0.50, 0.75), start=1):
                source_frame = root / f"{asset_id}-source-{index}.jpg"
                corrected_frame = root / f"{asset_id}-normalized-{index}.jpg"
                self._render_preview_frame(
                    row, source_frame, video_filter=source_filter, position=position
                )
                self._render_preview_frame(
                    row, corrected_frame, video_filter=video_filter, position=position
                )
                comparison_frames.append(
                    {"source": source_frame.name, "normalized": corrected_frame.name}
                )
        token = self._preview_token(row, mode, selected_crop, video_filter)
        recipe = {
            "asset_id": asset_id,
            "token": token,
            "mode": mode,
            "crop": selected_crop.as_dict() if selected_crop else None,
            "file_size": int(row["file_size"] or 0),
            "modified_ns": int(row["modified_ns"] or 0),
            "video_filter": video_filter,
            "frame_count": frame_count,
            "frames": comparison_frames,
        }
        recipe_path = self._preview_recipe_path(asset_id)
        partial_recipe = recipe_path.with_suffix(".json.partial")
        partial_recipe.write_text(json.dumps(recipe, indent=2), encoding="utf-8")
        partial_recipe.replace(recipe_path)
        self.connection.execute(
            """
            UPDATE normalization_profiles
            SET normalized_preview_relative_path = ?
            WHERE asset_id = ?
            """,
            (relative.as_posix(), asset_id),
        )
        self.connection.commit()
        return {**recipe, "source": source, "normalized": normalized}

    def preview_recipe(self, asset_id: int) -> dict[str, Any] | None:
        """Return a still-valid correction preview recipe for the dashboard."""
        recipe_path = self._preview_recipe_path(asset_id)
        try:
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        try:
            row = self._asset(asset_id)
            crop_data = recipe.get("crop")
            crop = Crop(**crop_data) if isinstance(crop_data, dict) else None
            draft = self._draft_profile(asset_id, str(recipe["mode"]), crop)
            video_filter = self.video_filter(
                draft, target_dimensions=self.output_canvas(row, draft)
            )
            expected = self._preview_token(row, str(recipe["mode"]), crop, video_filter)
            normalized = self.config.storage_root("previews") / f"{asset_id}-normalized.jpg"
        except (KeyError, TypeError, ValueError):
            return None
        if recipe.get("token") != expected or not normalized.is_file():
            return None
        if recipe.get("frame_count") == 3:
            root = self.config.storage_root("previews")
            if any(
                not (root / f"{asset_id}-{kind}-{index}.jpg").is_file()
                for kind in ("source", "normalized")
                for index in range(1, 4)
            ):
                return None
        return {
            "token": expected,
            "mode": str(recipe["mode"]),
            "crop": crop.as_dict() if crop else None,
            "frame_count": 3 if recipe.get("frame_count") == 3 else 1,
            "frames": recipe.get("frames") if isinstance(recipe.get("frames"), list) else [],
        }

    def approve_preview(self, asset_id: int, token: str) -> str:
        """Approve exactly the correction represented by a current preview."""
        recipe = self.preview_recipe(asset_id)
        if recipe is None or not token or token != recipe["token"]:
            raise ValueError("The corrected preview is missing or stale; preview the correction again")
        crop_data = recipe["crop"]
        crop = Crop(**crop_data) if isinstance(crop_data, dict) else None
        self.set_override(asset_id, str(recipe["mode"]), crop=crop)
        return self.approve_review(asset_id)

    def approve_folder_preview(self, asset_id: int, token: str) -> dict[str, Any]:
        """Apply the exact displayed correction to the asset's review folder."""
        recipe = self.preview_recipe(asset_id)
        if recipe is None or not token or token != recipe["token"]:
            raise ValueError("The corrected preview is missing or stale; preview the correction again")
        crop_data = recipe["crop"]
        crop = Crop(**crop_data) if isinstance(crop_data, dict) else None
        return self.approve_folder_review(asset_id, str(recipe["mode"]), crop=crop)

    def discard_previews(self, asset_ids) -> list[str]:
        """Best-effort removal of previews that are no longer needed."""
        errors: list[str] = []
        preview_root = self.config.storage_root("previews").resolve()
        for asset_id in asset_ids:
            try:
                self._preview_recipe_path(int(asset_id)).unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{self._preview_recipe_path(int(asset_id))}: {exc}")
            for kind in ("source", "normalized"):
                for index in range(1, 4):
                    extra = preview_root / f"{int(asset_id)}-{kind}-{index}.jpg"
                    try:
                        extra.unlink(missing_ok=True)
                    except OSError as exc:
                        errors.append(f"{extra}: {exc}")
            row = self.connection.execute(
                """
                SELECT source_preview_relative_path, normalized_preview_relative_path
                FROM normalization_profiles WHERE asset_id = ?
                """,
                (int(asset_id),),
            ).fetchone()
            if row is None:
                continue
            for column in (
                "source_preview_relative_path",
                "normalized_preview_relative_path",
            ):
                value = row[column]
                if not value:
                    continue
                candidate = (preview_root / Path(str(value))).resolve()
                try:
                    candidate.relative_to(preview_root)
                    candidate.unlink(missing_ok=True)
                except (OSError, ValueError) as exc:
                    errors.append(f"{candidate}: {exc}")
                else:
                    # column comes exclusively from the fixed tuple above.
                    self.connection.execute(
                        f"UPDATE normalization_profiles SET {column} = NULL WHERE asset_id = ?",
                        (int(asset_id),),
                    )
        self.connection.commit()
        return errors

    @staticmethod
    def suggest_crop(profile) -> Crop | None:
        getter = profile.get if hasattr(profile, "get") else profile.__getitem__
        existing = (
            getter("crop_width"),
            getter("crop_height"),
            getter("crop_x"),
            getter("crop_y"),
        )
        if all(value is not None for value in existing):
            return Crop(
                int(existing[0]),
                int(existing[1]),
                int(existing[2]),
                int(existing[3]),
            )

        try:
            source_width = int(getter("width") or 0)
            source_height = int(getter("height") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if source_width <= 0 or source_height <= 0:
            return None

        candidates: list[Crop] = []
        try:
            samples = json.loads(getter("sample_results_json") or "[]")
        except (KeyError, TypeError, json.JSONDecodeError):
            samples = []
        for sample in samples if isinstance(samples, list) else []:
            raw = sample.get("crop") if isinstance(sample, dict) else None
            if not isinstance(raw, dict):
                continue
            try:
                crop = Crop(
                    int(raw["width"]),
                    int(raw["height"]),
                    int(raw["x"]),
                    int(raw["y"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            has_border = (
                crop.x >= 2
                or crop.y >= 2
                or source_width - crop.x - crop.width >= 2
                or source_height - crop.y - crop.height >= 2
            )
            if (
                has_border
                and crop.width > 0
                and crop.height > 0
                and crop.x >= 0
                and crop.y >= 0
                and crop.x + crop.width <= source_width
                and crop.y + crop.height <= source_height
            ):
                candidates.append(crop)

        if candidates:
            width = int(statistics.median_high([item.width for item in candidates]))
            height = int(statistics.median_high([item.height for item in candidates]))
            x = int(statistics.median_high([item.x for item in candidates]))
            y = int(statistics.median_high([item.y for item in candidates]))
        else:
            width, height, x, y = source_width, source_height, 0, 0

        # Fit a centered 4:3 rectangle inside the likely active region.
        # Rounding outward is conservative for a preview suggestion.
        target_height = int(2 * math.ceil((width * 3 / 4) / 2))
        if target_height <= height:
            y += (height - target_height) // 2
            height = target_height
        else:
            target_width = int(2 * math.ceil((height * 4 / 3) / 2))
            if target_width <= width:
                x += (width - target_width) // 2
                width = target_width

        width -= width % 2
        height -= height % 2
        x = max(0, min(x, source_width - width))
        y = max(0, min(y, source_height - height))
        return Crop(width, height, x, y)

    def _profile(self, asset_id: int):
        return self.connection.execute(
            "SELECT * FROM normalization_profiles WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()

    def effective_mode(self, profile) -> str:
        override = profile["override_mode"]
        if override != "auto":
            return override
        if self.config.data.get("normalization", {}).get("aspect_policy", "retro_4_3") == "preserve":
            return "preserve_frame"
        if profile["analysis_status"] != "analyzed":
            raise ValueError(
                "Automatic normalization is not approved; choose a manual override"
            )
        return profile["detected_mode"]

    def requires_conversion(self, row, profile=None) -> bool:
        from retrotv.processing import enabled
        if not enabled(self.config, "normalization"):
            return False
        profile = profile or row
        mode = self.effective_mode(profile)
        if mode in {"pillarboxed_4_3", "pillarboxed_stretched_4_3", "stretched_4_3", "custom"}:
            return True
        target_width, target_height = self.output_canvas(row, profile)
        if self.allow_upscale and (int(row["width"]) < target_width or int(row["height"]) < target_height):
            return True
        return (
            int(row["width"]) > target_width
            or int(row["height"]) > target_height
        )

    def output_canvas(
        self,
        row,
        profile,
        *,
        allow_proposed: bool = False,
    ) -> tuple[int, int]:
        if (self.config.data.get("normalization", {}).get("aspect_policy", "retro_4_3") == "preserve"
                and profile["override_mode"] in {"auto", "preserve_frame"}):
            # A resolution ceiling is not an instruction to change the frame's
            # shape. Respect display aspect (including anamorphic sources).
            ratio = display_ratio(dict(row)) or int(row["width"]) / int(row["height"])
            height = min(self.canvas_height, self.canvas_width / ratio)
            if not self.allow_upscale:
                height = min(height, int(row["height"]), int(row["width"]) / ratio)
            return max(2, int(height * ratio) // 2 * 2), max(2, int(height) // 2 * 2)
        if self.allow_upscale:
            return self.canvas_width, self.canvas_height

        try:
            mode = self.effective_mode(profile)
        except ValueError:
            if (
                not allow_proposed
                or profile["override_mode"] != "auto"
                or profile["proposed_mode"] not in MODES
            ):
                raise
            mode = profile["proposed_mode"]
        source_width = int(row["width"])
        source_height = int(row["height"])
        max_units = min(self.canvas_width // 4, self.canvas_height // 3)
        max_units -= max_units % 2

        if mode in {"stretched_4_3", "pillarboxed_stretched_4_3"}:
            # Horizontal stretching does not add vertical detail. Use the source
            # height to choose a corrected 4:3 canvas without enlarging it.
            units = max(2, source_height // 3)
            units -= units % 2
        else:
            if mode in {"pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"}:
                active_width = int(profile["crop_width"])
                active_height = int(profile["crop_height"])
            else:
                active_width = source_width
                active_height = source_height
            units = max(
                math.ceil(active_width / 4),
                math.ceil(active_height / 3),
            )
            if units % 2:
                units += 1

        units = max(2, min(units, max_units))
        return 4 * units, 3 * units

    def video_filter(
        self,
        profile,
        *,
        allow_proposed: bool = False,
        target_dimensions: tuple[int, int] | None = None,
    ) -> str:
        try:
            mode = self.effective_mode(profile)
        except ValueError:
            if (
                not allow_proposed
                or profile["override_mode"] != "auto"
                or not profile["proposed_mode"]
            ):
                raise
            mode = profile["proposed_mode"]
        target_width, target_height = target_dimensions or (
            self.canvas_width,
            self.canvas_height,
        )
        target = f"{target_width}:{target_height}"
        if mode == "preserve_frame" and self.config.data.get("normalization", {}).get("aspect_policy") == "preserve":
            # Target dimensions already encode display aspect, including SAR.
            return f"scale={target},setsar=1"
        if mode in {"pillarboxed_4_3", "custom"}:
            values = (
                profile["crop_width"],
                profile["crop_height"],
                profile["crop_x"],
                profile["crop_y"],
            )
            if any(value is None for value in values):
                raise ValueError(f"{mode} requires a crop rectangle")
            crop = f"crop={values[0]}:{values[1]}:{values[2]}:{values[3]},"
            return (
                f"{crop}scale={target}:force_original_aspect_ratio=decrease,"
                f"pad={target}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
            )
        if mode == "pillarboxed_stretched_4_3":
            values = (
                profile["crop_width"],
                profile["crop_height"],
                profile["crop_x"],
                profile["crop_y"],
            )
            if any(value is None for value in values):
                raise ValueError(f"{mode} requires a crop rectangle")
            return (
                f"crop={values[0]}:{values[1]}:{values[2]}:{values[3]},"
                f"scale={target},setsar=1"
            )
        if mode == "stretched_4_3":
            return f"scale={target},setsar=1"
        if mode in {"native_4_3", "preserve_frame"}:
            return (
                f"scale={target}:force_original_aspect_ratio=decrease,"
                f"pad={target}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
            )
        raise ValueError(f"Mode cannot create a proxy: {mode}")

    def encoding_rate_limits(self, row) -> tuple[int, int]:
        """Return audio and peak video kb/s constrained by source file size."""
        duration = float(row["duration_seconds"] or 0.0)
        source_size = int(row["file_size"] or 0)
        if duration <= 0 or source_size <= 0:
            # Scanned assets normally always have both values. This fallback
            # remains bounded while allowing manually-created test records.
            return self.audio_bitrate_kbps, 2_000

        source_total_bps = source_size * 8 / duration
        output_total_bps = (
            source_total_bps
            * self.maximum_output_size_ratio
            * self.size_budget_safety
        )
        # Reserve a small allowance for MP4 muxing and metadata. On very
        # low-bitrate sources, reduce audio before starving the video.
        mux_allowance_bps = max(8_000, output_total_bps * 0.02)
        audio_kbps = min(
            self.audio_bitrate_kbps,
            max(48, int(output_total_bps * 0.30 / 1_000)),
        )
        video_kbps = max(
            64,
            int(
                (
                    output_total_bps
                    - mux_allowance_bps
                    - audio_kbps * 1_000
                )
                / 1_000
            ),
        )
        return audio_kbps, video_kbps

    def retry_rate_limits(
        self,
        audio_kbps: int,
        video_kbps: int,
        *,
        source_size: int,
        output_size: int,
    ) -> tuple[int, int]:
        """Reduce rates using the measured overshoot, leaving retry headroom."""
        if source_size <= 0 or output_size <= 0:
            factor = 0.75
        else:
            target_size = source_size * self.maximum_output_size_ratio
            factor = target_size / output_size * 0.90
        factor = min(0.85, max(0.25, factor))
        return (
            max(32, int(audio_kbps * factor)),
            max(48, int(video_kbps * factor)),
        )

    def encode_args(
        self,
        row,
        partial: Path,
        video_filter: str,
        audio_kbps: int,
        video_kbps: int,
        *,
        constrained_average: bool = False,
    ) -> list[str]:
        video_rate_args = (
            ["-b:v", f"{video_kbps}k"]
            if constrained_average
            else ["-crf", str(self.crf)]
        )
        return [
            self.config.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-nostdin", "-i", os.fspath(self.source_path(row)),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-map_metadata", "0", "-map_chapters", "0",
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", self.preset, *video_rate_args,
            "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", os.fspath(partial),
        ]

    def _relative_proxy_path(self, row) -> Path:
        channel = row["channel_slug"] or "shared"
        return Path(channel) / f"{row['id']}.mp4"

    def create_preview(self, asset_id: int) -> tuple[Path, Path]:
        profile = self._profile(asset_id)
        if profile is None:
            raise ValueError("Analyze the asset before creating previews")
        mode = str(profile["override_mode"] or "auto")
        if mode == "auto":
            mode = str(profile["proposed_mode"] or "")
        crop = self._preview_crop(dict(profile))
        result = self.create_corrected_preview(asset_id, mode, crop=crop)
        return Path(result["source"]), Path(result["normalized"])

    def create_proxy(self, asset_id: int) -> ProxyResult:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "normalization")
        row = self._asset(asset_id)
        profile = self._profile(asset_id)
        if profile is None:
            return ProxyResult(False, None, "Analyze the asset first")
        try:
            target_dimensions = self.output_canvas(row, profile)
            video_filter = self.video_filter(
                profile,
                target_dimensions=target_dimensions,
            )
            audio_kbps, video_kbps = self.encoding_rate_limits(row)
        except ValueError as exc:
            return ProxyResult(False, None, str(exc))

        relative = self._relative_proxy_path(row)
        root = self.config.storage_root("normalized")
        output = root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_suffix(".partial.mp4")
        self.connection.execute(
            "UPDATE normalization_profiles SET proxy_status = 'creating', proxy_error = NULL "
            "WHERE asset_id = ?",
            (asset_id,),
        )
        self.connection.commit()
        source_size = int(row["file_size"] or 0)
        size_retries = 0
        size_warning = None
        while True:
            args = self.encode_args(
                row,
                partial,
                video_filter,
                audio_kbps,
                video_kbps,
                constrained_average=size_retries > 0,
            )
            try:
                completed = (
                    run_cancellable(args, self.timeout, self.cancel_event)
                    if self.cancel_event is not None
                    else _run(args, self.timeout)
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return self._proxy_failed(asset_id, partial, str(exc))
            if completed.returncode:
                return self._proxy_failed(
                    asset_id, partial, completed.stderr.strip()[-2000:]
                )

            size_error = self._proxy_size_error(partial, source_size)
            if size_error is not None and size_retries < self.size_retry_attempts:
                audio_kbps, video_kbps = self.retry_rate_limits(
                    audio_kbps,
                    video_kbps,
                    source_size=source_size,
                    output_size=partial.stat().st_size,
                )
                size_retries += 1
                partial.unlink(missing_ok=True)
                continue

            if size_error is not None and self.accept_oversize_after_retries:
                actual_ratio = partial.stat().st_size / source_size if source_size else 0.0
                size_warning = (
                    f"Accepted corrected output after {size_retries} size-constrained "
                    f"retries; output is {actual_ratio:.2f}x the source"
                )
                print(f"[normalization] asset={asset_id}: {size_warning}")
                validation = self._validate_proxy(
                    partial,
                    float(row["duration_seconds"]),
                    target_dimensions,
                    source_size=source_size,
                    enforce_size=False,
                )
            else:
                validation = self._validate_proxy(
                    partial,
                    float(row["duration_seconds"]),
                    target_dimensions,
                    source_size=source_size,
                )
            break

        if validation is not None:
            return self._proxy_failed(asset_id, partial, validation)
        os.replace(partial, output)
        probe = probe_media(
            output, ffprobe=self.config.ffprobe, timeout_seconds=30.0
        )
        proxy_root_id = ensure_storage_root(self.connection, "normalized")
        self.connection.execute(
            """
            UPDATE normalization_profiles SET
                proxy_root_id = ?, proxy_relative_path = ?,
                proxy_status = 'ready', proxy_duration_seconds = ?,
                proxy_error = NULL, proxy_created_at = ?
            WHERE asset_id = ?
            """,
            (
                proxy_root_id,
                relative.as_posix(),
                probe.metadata.get("duration_seconds"),
                utc_now(),
                asset_id,
            ),
        )
        self.connection.commit()
        return ProxyResult(True, output, warning=size_warning)

    def _proxy_failed(self, asset_id: int, partial: Path, error: str) -> ProxyResult:
        if partial.is_file():
            partial.unlink()
        self.connection.execute(
            """
            UPDATE normalization_profiles
            SET proxy_status = 'failed', proxy_error = ?
            WHERE asset_id = ?
            """,
            (error, asset_id),
        )
        self.connection.commit()
        return ProxyResult(False, None, error)

    def _validate_proxy(
        self,
        path: Path,
        source_duration: float,
        expected_canvas: tuple[int, int],
        *,
        source_size: int = 0,
        enforce_size: bool = True,
    ) -> str | None:
        if enforce_size:
            size_error = self._proxy_size_error(path, source_size)
            if size_error is not None:
                return size_error
        probe = probe_media(path, ffprobe=self.config.ffprobe, timeout_seconds=30.0)
        if not probe.ok:
            return f"Proxy probe failed: {probe.error_detail}"
        metadata = probe.metadata
        if (metadata.get("width"), metadata.get("height")) != expected_canvas:
            return "Proxy canvas does not match the configured target"
        proxy_duration = float(metadata.get("duration_seconds") or 0.0)
        allowed = max(self.duration_tolerance, source_duration * 0.005)
        if abs(proxy_duration - source_duration) > allowed:
            return (
                f"Proxy duration drifted by {abs(proxy_duration - source_duration):.3f}s "
                f"(allowed {allowed:.3f}s)"
            )
        offsets = [0.0, max(0.0, proxy_duration * 0.5), max(0.0, proxy_duration - 3.0)]
        for offset in offsets:
            decoded = decode_sample(
                path,
                ffmpeg=self.config.ffmpeg,
                start_seconds=offset,
                sample_seconds=1.0,
                timeout_seconds=30.0,
            )
            if not decoded.ok:
                return f"Proxy decode/seek failed at {offset:.1f}s: {decoded.error_detail}"
        return None

    def _proxy_size_error(self, path: Path, source_size: int) -> str | None:
        if (
            source_size > 0
            and path.stat().st_size
            > round(source_size * self.maximum_output_size_ratio)
        ):
            return (
                f"Proxy size exceeds the configured "
                f"{self.maximum_output_size_ratio:.2f}x source-size limit"
            )
        return None
