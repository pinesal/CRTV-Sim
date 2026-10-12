"""Create the empty drag-and-drop media layout understood by CRTV Sim."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable


DEFAULT_DAYPARTS = ("morning", "afternoon", "evening", "night", "over-night")
SCOPES = (*DEFAULT_DAYPARTS, "daytime", "all-day")
MEDIA_TYPES = (
    "episodes", "shorts", "movies", "blocks", "commercials", "bumpers",
    "promos", "filler", "station-ids",
)
SHARED_MEDIA_TYPES = ("episodes", "commercials", "bumpers", "promos", "filler")
HOLIDAY_MEDIA_TYPES = MEDIA_TYPES
SHARED_HOLIDAY_MEDIA_TYPES = tuple(kind for kind in MEDIA_TYPES if kind != "blocks")
GRAPHIC_PURPOSES = ("up-next", "interstitials")
DEFAULT_HOLIDAYS = ("halloween", "christmas")


def _slugs(values: Iterable[object], defaults: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in (*defaults, *tuple(values)):
        value = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
        if value and value not in result:
            result.append(value)
    return tuple(result)


def ensure_channel_scaffold(
    channel_root: Path,
    *,
    dayparts: Iterable[object] = DEFAULT_DAYPARTS,
    holidays: Iterable[object] = DEFAULT_HOLIDAYS,
    shared: bool = False,
) -> list[Path]:
    """Create recognized media, graphics/music, and seasonal directories.

    Existing content is never changed. The returned paths are only directories
    created by this call, which also makes the function useful for status text.
    """
    root = Path(channel_root)
    scopes = _slugs(dayparts, DEFAULT_DAYPARTS)
    scopes = _slugs(scopes, ("daytime", "all-day"))
    holiday_slugs = _slugs(holidays, DEFAULT_HOLIDAYS)
    targets: set[Path] = set()

    media_types = SHARED_MEDIA_TYPES if shared else MEDIA_TYPES
    for scope in scopes:
        targets.update(root / scope / kind for kind in media_types)

    # Channel graphics are selected by purpose, daypart, holiday, and weekly
    # collection. Audio is supported both directly in each scope and in music/.
    graphics_roots = [root / "graphics"]
    graphics_roots.extend(root / "graphics" / scope for scope in scopes)
    for graphics in graphics_roots:
        targets.add(graphics / "music")
        for purpose in GRAPHIC_PURPOSES:
            targets.add(graphics / purpose)
            targets.add(graphics / purpose / "music")
    for holiday in holiday_slugs:
        for graphics in (root / "graphics" / "holidays" / holiday,
                         *(root / "graphics" / scope / "holidays" / holiday for scope in scopes)):
            targets.add(graphics / "music")
            for purpose in GRAPHIC_PURPOSES:
                targets.add(graphics / purpose)
                targets.add(graphics / purpose / "music")
        if not shared:
            targets.add(root / "specials" / "holidays" / holiday / "specials" / "weekly")
            for scope in scopes:
                targets.update(
                    root / "specials" / "holidays" / holiday / scope / kind
                    for kind in HOLIDAY_MEDIA_TYPES
                )
        else:
            for scope in scopes:
                targets.update(
                    root / "specials" / "holidays" / holiday / scope / kind
                    for kind in SHARED_HOLIDAY_MEDIA_TYPES
                )
            targets.add(root / "specials" / "holidays" / holiday / "specials" / "weekly")
    if not shared:
        targets.add(root / "specials" / "weekly")

    created: list[Path] = []
    for target in sorted(targets, key=lambda item: (len(item.parts), item.as_posix().casefold())):
        if target.exists():
            if not target.is_dir():
                raise ValueError(f"Media folder path is not a directory: {target}")
            continue
        target.mkdir(parents=True, exist_ok=True)
        created.append(target)
    return created


def ensure_library_scaffold(
    media_root: Path,
    *,
    channels: Iterable[Path] = (),
    dayparts: Iterable[object] = DEFAULT_DAYPARTS,
    holidays: Iterable[object] = DEFAULT_HOLIDAYS,
) -> list[Path]:
    """Create shared/template trees and optionally supplied channel roots."""
    root = Path(media_root).resolve()
    if not root.is_dir():
        raise ValueError(f"Media library folder is unavailable: {root}")
    created: list[Path] = []
    created.extend(ensure_channel_scaffold(
        root / "shared", dayparts=dayparts, holidays=holidays, shared=True
    ))
    created.extend(ensure_channel_scaffold(
        root / "_channel_template", dayparts=dayparts, holidays=holidays
    ))
    for channel_root in channels:
        resolved = Path(channel_root).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ValueError(f"Channel folder is outside the media library: {channel_root}")
        created.extend(ensure_channel_scaffold(
            resolved, dayparts=dayparts, holidays=holidays
        ))
    return created
