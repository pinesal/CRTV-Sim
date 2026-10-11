from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


COLOR_PATTERN = re.compile(r"^(?:0x|#)[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class DesignPalette:
    header: str
    panel: str
    panel_alternate: str
    primary: str
    secondary: str
    muted: str
    accent: str
    condition: str


@dataclass(frozen=True)
class Appearance:
    channel_name: str
    palette: DesignPalette
    panel_opacity: float
    panel_border_opacity: float
    safe_margin: int
    text_scale: float


DEFAULT_PALETTE = DesignPalette(
    header="0x071827",
    panel="0x071827",
    panel_alternate="0x102c44",
    primary="0xf8fbff",
    secondary="0xb8d8ec",
    muted="0x82a9c2",
    accent="0x59c7e8",
    condition="0x82edb0",
)


def appearance_settings(settings: Mapping[str, object] | None) -> Appearance:
    root = settings if isinstance(settings, Mapping) else {}
    raw = root.get("appearance")
    appearance = raw if isinstance(raw, Mapping) else {}
    raw_palette = appearance.get("palette")
    palette = raw_palette if isinstance(raw_palette, Mapping) else {}
    channel = root.get("channel")
    channel_settings = channel if isinstance(channel, Mapping) else {}
    channel_name = str(channel_settings.get("display_name", "RetroWeather")).strip()
    return Appearance(
        channel_name=channel_name or "RetroWeather",
        palette=DesignPalette(
            **{
                field: _color(palette.get(field), getattr(DEFAULT_PALETTE, field))
                for field in DesignPalette.__dataclass_fields__
            }
        ),
        panel_opacity=_number(appearance.get("panel_opacity"), 0.58, 0.25, 0.90),
        panel_border_opacity=_number(
            appearance.get("panel_border_opacity"), 0.46, 0.10, 0.90
        ),
        safe_margin=int(_number(appearance.get("safe_margin"), 28, 20, 40)),
        text_scale=_number(appearance.get("text_scale"), 1.0, 0.85, 1.15),
    )


def fit_text(
    text: str,
    preferred_size: int,
    max_width: int | None,
    *,
    minimum_size: int = 12,
    bold: bool = False,
    text_scale: float = 1.0,
) -> tuple[str, int]:
    """Conservatively fit one FFmpeg drawtext line into a pixel width."""
    size = max(minimum_size, round(preferred_size * text_scale))
    if not max_width or not text:
        return text, size
    width_factor = 0.62 if bold else 0.56
    fitted = min(size, int(max_width / max(1.0, len(text) * width_factor)))
    if fitted >= minimum_size:
        return text, max(minimum_size, fitted)
    maximum_characters = max(4, int(max_width / (minimum_size * width_factor)))
    if len(text) > maximum_characters:
        text = text[: max(1, maximum_characters - 1)].rstrip() + "…"
    return text, minimum_size


def _color(raw: object, default: str) -> str:
    value = str(raw).strip() if raw is not None else ""
    if not COLOR_PATTERN.fullmatch(value):
        return default
    return "0x" + value[-6:].lower()


def _number(raw: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return max(minimum, min(maximum, float(raw)))
