from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from retroweather.assets import scan_asset_library
from retroweather.rendering.themes import THEME_PRIORITY


@dataclass(frozen=True)
class MusicAsset:
    path: Path
    source: str
    duration_seconds: float


def build_music_catalog(media_root: Path) -> dict[str, tuple[MusicAsset, ...]]:
    grouped: dict[str, list[MusicAsset]] = {}
    inventory = scan_asset_library(media_root)
    for asset in inventory["assets"]:
        if not asset["eligible"] or asset["media_type"] != "audio":
            continue
        duration = asset.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0:
            continue
        relative = Path(asset["relative_path"])
        key: str | None = None
        if asset["role"] == "default_music":
            key = "default"
        elif asset["role"] == "condition_music" and len(relative.parts) >= 4:
            key = relative.parts[2]
        elif str(asset["role"]).startswith("holiday_"):
            key = str(asset["role"]).removesuffix("_music")
        if key is not None:
            grouped.setdefault(key, []).append(
                MusicAsset(media_root / relative, key, float(duration))
            )
    return {
        key: tuple(sorted(assets, key=lambda item: item.path.name.casefold()))
        for key, assets in grouped.items()
    }


def select_music(
    media_root: Path,
    theme_families: Iterable[str],
    generation_id: str,
    catalog: Mapping[str, tuple[MusicAsset, ...]] | None = None,
) -> MusicAsset | None:
    available = catalog if catalog is not None else build_music_catalog(media_root)
    families = sorted(
        set(theme_families),
        key=lambda family: THEME_PRIORITY.get(family, 0),
        reverse=True,
    )
    candidates: tuple[MusicAsset, ...] = ()
    for family in families:
        candidates = available.get(family, ())
        if candidates:
            break
    if not candidates:
        candidates = available.get("default", ())
    if not candidates:
        return None
    checksum = int.from_bytes(
        hashlib.sha256(f"music:{generation_id}".encode("utf-8")).digest()[:8]
    )
    return candidates[checksum % len(candidates)]


def soundtrack_filter(
    track_duration: float,
    output_duration: float,
    settings: Mapping[str, object],
) -> tuple[int, bool, str, str]:
    """Return input count, stream-loop flag, FFmpeg graph, and strategy."""
    volume = _bounded_number(settings.get("volume"), 0.28, 0.0, 1.0)
    target_lufs = _bounded_number(settings.get("target_lufs"), -18.0, -30.0, -10.0)
    fade_in = min(
        _bounded_number(settings.get("fade_in_seconds"), 1.5, 0.0, 10.0),
        output_duration / 4,
    )
    fade_out = min(
        _bounded_number(settings.get("fade_out_seconds"), 2.0, 0.0, 10.0),
        output_duration / 4,
    )
    requested_crossfade = _bounded_number(
        settings.get("crossfade_seconds"), 1.0, 0.0, 5.0
    )
    crossfade = min(requested_crossfade, track_duration / 4)
    effective_length = track_duration - crossfade
    repeats = max(1, math.ceil((output_duration - crossfade) / effective_length))

    # Very short clips can make FFmpeg's chained acrossfade graph timing
    # nondeterministic after loudness analysis. Stream-loop those clips; real
    # soundtrack-length assets retain seamless crossfades.
    if repeats > 1 and crossfade > 0 and repeats <= 16 and track_duration >= 5.0:
        # Use separate inputs for each repeat. Splitting one decoded input can
        # exhaust acrossfade's second branch before it is needed, truncating
        # the soundtrack at the end of the first play.
        graph_parts: list[str] = []
        previous = "1:a"
        for index in range(1, repeats):
            output = f"loop{index}"
            graph_parts.append(
                f"[{previous}][{index + 1}:a]"
                f"acrossfade=d={crossfade:.3f}:c1=tri:c2=tri[{output}]"
            )
            previous = output
        source = f"[{previous}]"
        input_count = repeats
        stream_loop = False
        strategy = f"crossfade-{repeats}x"
    else:
        graph_parts = []
        source = "[1:a]"
        input_count = 1
        stream_loop = repeats > 1
        strategy = "stream-loop" if repeats > 1 else "single-play"

    processing = (
        f"{source}loudnorm=I={target_lufs:.1f}:TP=-2.0:LRA=11.0,"
        f"volume={volume:.4f},aresample=48000,"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={max(0.0, output_duration - fade_out):.3f}:d={fade_out:.3f},"
        f"atrim=duration={output_duration:.3f},asetpts=N/SR/TB[aout]"
    )
    graph_parts.append(processing)
    return input_count, stream_loop, ";".join(graph_parts), strategy


def music_settings(raw: object) -> dict[str, object]:
    value = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": bool(value.get("enabled", True)),
        "volume": _bounded_number(value.get("volume"), 0.28, 0.0, 1.0),
        "target_lufs": _bounded_number(value.get("target_lufs"), -18.0, -30.0, -10.0),
        "fade_in_seconds": _bounded_number(value.get("fade_in_seconds"), 1.5, 0.0, 10.0),
        "fade_out_seconds": _bounded_number(value.get("fade_out_seconds"), 2.0, 0.0, 10.0),
        "crossfade_seconds": _bounded_number(value.get("crossfade_seconds"), 1.0, 0.0, 5.0),
    }


def _bounded_number(raw: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return max(minimum, min(maximum, float(raw)))
