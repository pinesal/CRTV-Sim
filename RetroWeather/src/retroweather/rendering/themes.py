from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

from retroweather.assets import MOTION_EXTENSIONS, scan_asset_library
from retroweather.models import (
    DailyForecast,
    HourlyForecast,
    NormalizedWeatherSnapshot,
    WeatherAlert,
)


STILL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
THEMES = frozenset(
    {"default", "sunny", "partly-cloudy", "cloudy", "rainy", "stormy", "snowy", "foggy", "windy"}
)
STORM_CODES = frozenset({95, 96, 99})
SNOW_CODES = frozenset({56, 57, 66, 67, 71, 73, 75, 77, 85, 86})
RAIN_CODES = frozenset({51, 53, 55, 61, 63, 65, 80, 81, 82})
FOG_CODES = frozenset({45, 48})
CLOUDY_CODES = frozenset({3})
PARTLY_CLOUDY_CODES = frozenset({2})
CLEAR_CODES = frozenset({0, 1})
KNOWN_WMO_CODES = (
    STORM_CODES | SNOW_CODES | RAIN_CODES | FOG_CODES | CLOUDY_CODES
    | PARTLY_CLOUDY_CODES | CLEAR_CODES
)
THEME_PRIORITY = {
    "default": 0,
    "sunny": 1,
    "partly-cloudy": 2,
    "cloudy": 3,
    "windy": 4,
    "foggy": 5,
    "rainy": 6,
    "snowy": 7,
    "stormy": 8,
    "alert": 9,
}


@dataclass(frozen=True)
class ThemeSelection:
    family: str
    variant: str

    @property
    def key(self) -> str:
        return f"{self.family}/{self.variant}"


def classify_condition(
    code: int | None,
    wind_speed: float | None = None,
    visibility: float | None = None,
    *,
    units: str = "imperial",
    severe_alert: bool = False,
) -> str:
    """Map normalized facts to one visual family in documented priority order."""
    if severe_alert:
        return "alert"
    if code in STORM_CODES:
        return "stormy"
    if code in SNOW_CODES:
        return "snowy"
    if code in RAIN_CODES:
        return "rainy"
    reduced_visibility = 1.0 if units == "imperial" else 1.6
    if code in FOG_CODES or (visibility is not None and visibility <= reduced_visibility):
        return "foggy"
    wind_threshold = 25.0 if units == "imperial" else 40.0
    if wind_speed is not None and wind_speed >= wind_threshold:
        return "windy"
    if code in CLOUDY_CODES:
        return "cloudy"
    if code in PARTLY_CLOUDY_CODES:
        return "partly-cloudy"
    if code in CLEAR_CODES:
        return "sunny"
    return "default"


def current_theme(snapshot: NormalizedWeatherSnapshot) -> ThemeSelection:
    return ThemeSelection(
        classify_condition(
            snapshot.current.condition_code,
            snapshot.current.wind_speed,
            snapshot.current.visibility,
            units=snapshot.units,
            severe_alert=has_active_severe_alert(snapshot.alerts, snapshot.current.observed_at),
        ),
        "day" if snapshot.current.is_day else "night",
    )


def hourly_theme(
    periods: Iterable[HourlyForecast],
    units: str,
    alerts: Sequence[WeatherAlert] = (),
) -> ThemeSelection:
    selected: tuple[int, str, bool] | None = None
    for period in periods:
        family = classify_condition(
            period.condition_code,
            period.wind_speed,
            units=units,
            severe_alert=has_active_severe_alert(alerts, period.starts_at),
        )
        candidate = (THEME_PRIORITY[family], family, period.is_day)
        if selected is None or candidate[0] > selected[0]:
            selected = candidate
    if selected is None:
        return ThemeSelection("default", "day")
    return ThemeSelection(selected[1], "day" if selected[2] else "night")


def daily_theme(
    forecast: DailyForecast | None,
    variant: str = "day",
    alerts: Sequence[WeatherAlert] = (),
) -> ThemeSelection:
    family = classify_condition(
        forecast.condition_code if forecast else None,
        severe_alert=bool(forecast and has_severe_alert_on_date(alerts, forecast.date)),
    )
    return ThemeSelection(family, variant)


def has_active_severe_alert(alerts: Sequence[WeatherAlert], instant: datetime) -> bool:
    return any(
        alert.severity.strip().casefold() in {"severe", "extreme"}
        and alert.starts_at <= instant
        and (alert.ends_at is None or instant < alert.ends_at)
        for alert in alerts
    )


def has_severe_alert_on_date(alerts: Sequence[WeatherAlert], represented_date: date) -> bool:
    return any(
        alert.severity.strip().casefold() in {"severe", "extreme"}
        and alert.starts_at.date() <= represented_date
        and (alert.ends_at is None or represented_date <= alert.ends_at.date())
        for alert in alerts
    )


def build_background_catalog(media_root: Path) -> dict[str, tuple[Path, ...]]:
    grouped: dict[str, list[Path]] = {}
    inventory = scan_asset_library(media_root)
    for asset in inventory["assets"]:
        if not asset["eligible"] or asset["media_type"] not in {"still", "motion"}:
            continue
        relative = Path(asset["relative_path"])
        key: str | None = None
        if asset["role"] == "condition_background" and len(relative.parts) >= 5:
            key = f"{relative.parts[2]}/{relative.parts[3]}"
        elif asset["role"] == "alert_artwork":
            key = "alert"
        if key is not None:
            grouped.setdefault(key, []).append(media_root / relative)
    return {
        key: tuple(sorted(paths, key=lambda path: path.name.casefold()))
        for key, paths in grouped.items()
    }


def discover_backgrounds(
    media_root: Path,
    theme: ThemeSelection,
    catalog: dict[str, tuple[Path, ...]] | None = None,
) -> tuple[Path, ...]:
    available = catalog if catalog is not None else build_background_catalog(media_root)
    if theme.family == "alert":
        return available.get("alert", ())
    candidates = available.get(theme.key, ())
    if candidates:
        return candidates
    if theme.family != "default":
        candidates = available.get(f"default/{theme.variant}", ())
        if candidates:
            return candidates
    return ()


def select_background(
    media_root: Path,
    theme: ThemeSelection,
    stable_key: str,
    catalog: dict[str, tuple[Path, ...]] | None = None,
) -> Path | None:
    candidates = discover_backgrounds(media_root, theme, catalog)
    if not candidates:
        return None
    # A stable, process-independent choice that changes with each generation.
    checksum = int.from_bytes(hashlib.sha256(stable_key.encode("utf-8")).digest()[:8])
    return candidates[checksum % len(candidates)]


def inventory(media_root: Path) -> dict[str, int]:
    catalog = build_background_catalog(media_root)
    return {
        f"{family}/{variant}": len(catalog.get(f"{family}/{variant}", ()))
        for family in sorted(THEMES)
        for variant in ("day", "night")
    }


def _files(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in STILL_EXTENSIONS),
            key=lambda path: path.name.casefold(),
        )
    )
