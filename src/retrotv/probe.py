from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    metadata: dict[str, Any]
    raw_json: str | None = None
    error_category: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class DecodeResult:
    ok: bool
    error_category: str | None = None
    error_detail: str | None = None


def _short_error(value: str, maximum: int = 1000) -> str:
    cleaned = " ".join(value.replace("\x00", "").split())
    return cleaned[:maximum]


def categorize_ffmpeg_error(stderr: str) -> str:
    text = stderr.lower()
    categories = (
        ("permission denied", "permission_denied"),
        ("no such file", "missing_file"),
        ("invalid data found", "invalid_data"),
        ("moov atom not found", "missing_moov_atom"),
        ("could not find codec parameters", "codec_parameters"),
        ("non monotonically increasing", "non_monotonic_timestamps"),
        ("packet corrupt", "corrupt_packet"),
        ("error while decoding", "decode_error"),
        ("end of file", "truncated_eof"),
        ("timed out", "timeout"),
    )
    for needle, category in categories:
        if needle in text:
            return category
    return "tool_error"


def _run(
    args: list[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def probe_media(
    path: Path,
    *,
    ffprobe: str,
    timeout_seconds: float,
) -> ProbeResult:
    args = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        os.fspath(path),
    ]
    try:
        completed = _run(args, timeout_seconds=timeout_seconds)
    except FileNotFoundError:
        return ProbeResult(
            False,
            {},
            error_category="ffprobe_missing",
            error_detail=f"FFprobe executable was not found: {ffprobe}",
        )
    except subprocess.TimeoutExpired as exc:
        return ProbeResult(
            False,
            {},
            error_category="probe_timeout",
            error_detail=str(exc),
        )

    if completed.returncode != 0:
        return ProbeResult(
            False,
            {},
            error_category=categorize_ffmpeg_error(completed.stderr),
            error_detail=_short_error(completed.stderr),
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return ProbeResult(
            False,
            {},
            error_category="invalid_probe_json",
            error_detail=str(exc),
        )

    streams = payload.get("streams") or []
    video_stream = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not (stream.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    format_info = payload.get("format") or {}

    duration_raw = format_info.get("duration")
    if duration_raw is None and video_stream:
        duration_raw = video_stream.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.25:
        return ProbeResult(
            False,
            {},
            raw_json=completed.stdout,
            error_category="invalid_duration",
            error_detail=f"Duration was not a positive schedulable value: {duration_raw!r}",
        )
    if video_stream is None:
        return ProbeResult(
            False,
            {},
            raw_json=completed.stdout,
            error_category="missing_video_stream",
            error_detail="No playable video stream was found",
        )

    def integer(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    metadata = {
        "container_format": format_info.get("format_name"),
        "duration_seconds": duration,
        "bit_rate": integer(format_info.get("bit_rate")),
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "width": integer(video_stream.get("width")),
        "height": integer(video_stream.get("height")),
        "frame_rate": video_stream.get("avg_frame_rate")
        or video_stream.get("r_frame_rate"),
        "sample_aspect_ratio": video_stream.get("sample_aspect_ratio"),
        "display_aspect_ratio": video_stream.get("display_aspect_ratio"),
        "pixel_format": video_stream.get("pix_fmt"),
        "field_order": video_stream.get("field_order"),
        "audio_sample_rate": integer(audio_stream.get("sample_rate"))
        if audio_stream
        else None,
        "audio_channels": integer(audio_stream.get("channels"))
        if audio_stream
        else None,
    }
    return ProbeResult(True, metadata, raw_json=completed.stdout)


def decode_sample(
    path: Path,
    *,
    ffmpeg: str,
    start_seconds: float,
    sample_seconds: float,
    timeout_seconds: float,
) -> DecodeResult:
    args = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-ss",
        f"{max(0.0, start_seconds):.3f}",
        "-i",
        os.fspath(path),
        "-t",
        f"{max(0.1, sample_seconds):.3f}",
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = _run(args, timeout_seconds=timeout_seconds)
    except FileNotFoundError:
        return DecodeResult(
            False,
            "ffmpeg_missing",
            f"FFmpeg executable was not found: {ffmpeg}",
        )
    except subprocess.TimeoutExpired as exc:
        return DecodeResult(False, "decode_timeout", str(exc))
    if completed.returncode == 0:
        return DecodeResult(True)
    return DecodeResult(
        False,
        categorize_ffmpeg_error(completed.stderr),
        _short_error(completed.stderr),
    )


def fingerprint_file(
    path: Path,
    *,
    mode: str = "quick",
    quick_bytes: int = 1024 * 1024,
) -> tuple[str, str]:
    digest = hashlib.sha256()
    size = path.stat().st_size
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")

    if mode == "full":
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), "sha256-full-v1"

    if mode != "quick":
        raise ValueError(f"Unknown fingerprint mode: {mode}")

    with path.open("rb") as handle:
        digest.update(handle.read(quick_bytes))
        if size > quick_bytes:
            handle.seek(max(0, size - quick_bytes))
            digest.update(handle.read(quick_bytes))
    return digest.hexdigest(), f"sha256-size-head-tail-{quick_bytes}-v1"

