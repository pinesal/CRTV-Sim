from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

from retroweather.assets import scan_asset_library
from retroweather.rendering.design import Appearance, DesignPalette, appearance_settings


HALLOWEEN_PALETTE = DesignPalette(
    header="0x21132f",
    panel="0x1b1520",
    panel_alternate="0x342039",
    primary="0xfff5df",
    secondary="0xffcf70",
    muted="0xc6a875",
    accent="0xf26a21",
    condition="0xffb13b",
)

CHRISTMAS_PALETTE = DesignPalette(
    header="0x103b32",
    panel="0x102a24",
    panel_alternate="0x4b1f2b",
    primary="0xfff7df",
    secondary="0xf4d06f",
    muted="0xc9b98f",
    accent="0xb93a3a",
    condition="0xffd166",
)


def active_season(value: date | datetime, holidays: Mapping | None = None) -> str | None:
    """Return the basic seasonal presentation active on a local calendar date."""
    if holidays is not None:
        month_day = value.strftime("%m-%d")
        active = []
        for slug, holiday in holidays.items():
            if not isinstance(holiday, Mapping) or not holiday.get("enabled", True):
                continue
            start, end = str(holiday.get("start", "")), str(holiday.get("end", ""))
            if not start or not end:
                continue
            if (start <= month_day <= end if start <= end else month_day >= start or month_day <= end):
                active.append((-int(holiday.get("priority", 0)), str(slug)))
        return min(active)[1] if active else None
    if value.month == 10:
        return "halloween"
    if value.month == 12 and value.day <= 25:
        return "christmas"
    return None


def seasonal_appearance(
    settings: Mapping[str, object] | None, value: date | datetime
) -> tuple[Appearance, str | None]:
    appearance = appearance_settings(settings)
    holidays = settings.get("crtvsim_holidays", settings.get("holidays")) if settings else None
    season = active_season(value, holidays)
    if season == "halloween":
        appearance = replace(appearance, palette=HALLOWEEN_PALETTE)
    elif season == "christmas":
        appearance = replace(appearance, palette=CHRISTMAS_PALETTE)
    return appearance, season


def discover_holiday_overlays(media_root: Path, season: str) -> tuple[Path, ...]:
    prefix = f"graphics/holidays/{season}/overlays/"
    inventory = scan_asset_library(media_root)
    return tuple(
        media_root / str(asset["relative_path"])
        for asset in inventory["assets"]
        if asset["role"] == f"holiday_{season}_artwork"
        and str(asset["relative_path"]).startswith(prefix)
        and asset["media_type"] == "still"
        and asset["valid"]
        and asset.get("has_alpha") is True
    )


def select_holiday_overlay(
    media_root: Path, season: str | None, stable_key: str
) -> Path | None:
    if season is None:
        return None
    candidates = discover_holiday_overlays(media_root, season)
    if not candidates:
        return None
    checksum = int.from_bytes(
        hashlib.sha256(f"{stable_key}:{season}".encode("utf-8")).digest()[:8],
        "big",
    )
    return candidates[checksum % len(candidates)]
