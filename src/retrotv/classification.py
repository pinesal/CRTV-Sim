from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


MEDIA_TYPES = {
    "episodes": "episode",
    "movies": "movie",
    "bumpers": "bumper",
    "commercials": "commercial",
    "promos": "promo",
    "station-ids": "station_id",
    "shorts": "short",
    "blocks": "complete_block",
    "filler": "filler",
}

DAYPARTS = {"morning", "afternoon", "evening", "night", "all-day"}
CHANNEL_DAYPARTS = DAYPARTS | {"daytime"}
# ``all-day`` and ``daytime`` are convenience scopes.  Actual dayparts come
# from Settings and may grow over time, so do not use this set to decide which
# configured folders are valid.
SHARED_DAYPARTS = CHANNEL_DAYPARTS
DEFAULT_HOLIDAYS = {"halloween", "christmas"}


def slugify(value: str) -> str:
    lowered = value.strip().lower().replace("_", "-").replace(" ", "-")
    lowered = re.sub(r"-+", "-", lowered)
    return re.sub(r"[^a-z0-9-]", "", lowered).strip("-")


def resolve_channel_root(media_root: Path, channel: str) -> Path:
    """Reuse an existing channel directory regardless of display spelling.

    Folder writers operate on canonical slugs, while a drag-and-drop library
    may intentionally use a human-readable name such as ``Cartoon Network``.
    Resolving by the same slug rules as the scanner prevents writers from
    creating a second ``cartoon-network`` directory beside it.
    """
    channel_slug = slugify(channel)
    matches = []
    if media_root.is_dir():
        matches = sorted(
            (
                path
                for path in media_root.iterdir()
                if path.is_dir() and slugify(path.name) == channel_slug
            ),
            key=lambda path: path.name.casefold(),
        )
    if len(matches) > 1:
        names = " and ".join(repr(path.name) for path in matches)
        raise ValueError(
            f"Channel folders {names} both normalize to {channel_slug!r}"
        )
    return matches[0] if matches else media_root / channel_slug


def humanize(value: str) -> str:
    value = re.sub(r"[._-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class Eligibility:
    channel_slug: str | None
    daypart_slug: str | None
    source: str
    weight: float = 1.0


@dataclass
class Classification:
    media_type: str | None = None
    channel_slug: str | None = None
    daypart_slug: str | None = None
    collection_type: str | None = None
    collection_slug: str | None = None
    holiday_slug: str | None = None
    holiday_weekly_slug: str | None = None
    series_title: str | None = None
    display_title: str | None = None
    eligibility: list[Eligibility] = field(default_factory=list)
    review_reasons: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return self.media_type is not None and not self.review_reasons


def _holiday_slug(
    raw: str,
    known_holidays: set[str],
    classification: Classification,
) -> str:
    slug = slugify(raw)
    if slug in known_holidays:
        return slug
    match = difflib.get_close_matches(slug, sorted(known_holidays), n=1, cutoff=0.72)
    detail = f"Unknown holiday folder '{raw}'"
    if match:
        detail += f"; did you mean '{match[0]}'?"
    classification.review_reasons.append(("unknown_holiday", detail))
    return slug


def _assign_media_type(
    raw: str,
    classification: Classification,
) -> None:
    slug = slugify(raw)
    classification.media_type = MEDIA_TYPES.get(slug)
    if classification.media_type is None:
        classification.review_reasons.append(
            ("unknown_media_type", f"Unknown media type folder '{raw}'")
        )


def _expanded_dayparts(scope: str | None, known: set[str]) -> tuple[str | None, ...]:
    if scope is None:
        return (None,)
    if scope == "daytime":
        values = ("morning", "afternoon", "evening")
    elif scope == "all-day":
        values = tuple(sorted(known - {"all-day", "daytime"}))
    else:
        values = (scope,)
    return values or (scope,)


def _classify_holiday_scope(
    parts: tuple[str, ...], result: Classification, dayparts: set[str]
) -> int:
    # A holiday mirrors a channel: <holiday>/<daypart>/<media-type> and
    # <holiday>/specials/weekly/<collection>/<media-type>. Keep the earlier
    # dayparts/ and weekly/ shapes readable for already imported libraries.
    kind = slugify(parts[4])
    if kind in dayparts and len(parts) >= 7:
        result.daypart_slug = kind
        _assign_media_type(parts[5], result)
        return 6
    if kind == "specials" and len(parts) >= 9 and slugify(parts[5]) == "weekly":
        result.holiday_weekly_slug = slugify(parts[6])
        _assign_media_type(parts[7], result)
        return 8
    if kind == "dayparts" and len(parts) >= 8:
        scope = slugify(parts[5])
        if scope not in dayparts:
            result.review_reasons.append(("unknown_daypart", f"Unknown holiday daypart '{parts[5]}'"))
        result.daypart_slug = scope
        _assign_media_type(parts[6], result)
        return 7
    if kind == "weekly" and len(parts) >= 8:
        result.holiday_weekly_slug = slugify(parts[5])
        _assign_media_type(parts[6], result)
        return 7
    # Legacy <holiday>/<media-type> remains ordinary all-day media.
    _assign_media_type(parts[4], result)
    return 5


def classify_path(
    relative_path: str,
    *,
    known_channels: set[str],
    known_holidays: set[str] | None = None,
    known_dayparts: set[str] | None = None,
) -> Classification:
    holidays = known_holidays or DEFAULT_HOLIDAYS
    path = PurePosixPath(relative_path.replace("\\", "/"))
    parts = path.parts
    result = Classification(display_title=humanize(path.stem))

    if len(parts) < 3:
        result.review_reasons.append(
            ("unrecognized_path", "Path does not contain enough classification folders")
        )
        return result

    top = slugify(parts[0])
    if top == "shared":
        _classify_shared(parts, result, holidays, known_dayparts or CHANNEL_DAYPARTS)
    else:
        _classify_channel(parts, result, known_channels, holidays, known_dayparts or CHANNEL_DAYPARTS)

    return result


def _classify_channel(
    parts: tuple[str, ...],
    result: Classification,
    known_channels: set[str],
    holidays: set[str],
    dayparts: set[str],
) -> None:
    channel = slugify(parts[0])
    result.channel_slug = channel
    if channel not in known_channels:
        result.review_reasons.append(
            ("unknown_channel", f"Unknown channel folder '{parts[0]}'")
        )

    second = slugify(parts[1])
    if second == "specials":
        if len(parts) < 6:
            result.review_reasons.append(
                ("unrecognized_path", "Special media path is incomplete")
            )
            return
        special_kind = slugify(parts[2])
        if special_kind == "holidays":
            holiday = _holiday_slug(parts[3], holidays, result)
            result.collection_type = "holiday"
            result.collection_slug = holiday
            result.holiday_slug = holiday
            content_index = _classify_holiday_scope(parts, result, dayparts)
        elif special_kind == "weekly":
            collection = slugify(parts[3])
            result.collection_type = "weekly"
            result.collection_slug = collection
            _assign_media_type(parts[4], result)
            content_index = 5
        else:
            result.review_reasons.append(
                ("unknown_collection_type", f"Unknown special collection '{parts[2]}'")
            )
            return
        scopes = _expanded_dayparts(result.daypart_slug, dayparts) if result.collection_type == "holiday" else (None,)
        result.eligibility.extend(
            Eligibility(channel, scope, f"channel_{result.collection_type}") for scope in scopes
        )
    else:
        if second not in dayparts:
            result.review_reasons.append(
                ("unknown_daypart", f"Unknown daypart folder '{parts[1]}'")
            )
        result.daypart_slug = second
        _assign_media_type(parts[2], result)
        content_index = 3
        expanded = _expanded_dayparts(second, dayparts)
        for daypart in expanded:
            result.eligibility.append(
                Eligibility(channel, daypart, "channel_daypart_folder")
            )

    if result.media_type == "episode" and len(parts) > content_index + 1:
        result.series_title = humanize(parts[content_index])


def _classify_shared(
    parts: tuple[str, ...],
    result: Classification,
    holidays: set[str],
    dayparts: set[str],
) -> None:
    second = slugify(parts[1])
    if second == "specials":
        if len(parts) < 6:
            result.review_reasons.append(
                ("unrecognized_path", "Shared special media path is incomplete")
            )
            return
        special_kind = slugify(parts[2])
        if special_kind == "holidays":
            holiday = _holiday_slug(parts[3], holidays, result)
            result.collection_type = "holiday"
            result.collection_slug = holiday
            result.holiday_slug = holiday
            _classify_holiday_scope(parts, result, dayparts)
            if result.media_type == "complete_block":
                result.review_reasons.append(
                    (
                        "shared_program_requires_channels",
                        "Shared holiday complete blocks require explicit channels",
                    )
                )
            elif result.media_type == "episode":
                result.eligibility.extend(
                    Eligibility("*", scope, "shared_holiday_episode_collection")
                    for scope in _expanded_dayparts(result.daypart_slug, dayparts)
                )
                category_index = next(
                    (index for index in range(4, len(parts) - 1)
                     if slugify(parts[index]) == "episodes"), None
                )
                if category_index is not None and len(parts) > category_index + 2:
                    result.series_title = humanize(parts[category_index + 1])
            elif result.media_type == "movie":
                result.eligibility.extend(
                    Eligibility("*", scope, "shared_holiday_movie_collection")
                    for scope in _expanded_dayparts(result.daypart_slug, dayparts)
                )
            else:
                result.eligibility.extend(
                    Eligibility("*", scope, "shared_holiday_interstitial")
                    for scope in _expanded_dayparts(result.daypart_slug, dayparts)
                )
        elif special_kind == "weekly":
            result.collection_type = "weekly"
            result.collection_slug = slugify(parts[3])
            _assign_media_type(parts[4], result)
            result.review_reasons.append(
                (
                    "shared_weekly_requires_channels",
                    "Shared weekly programming requires explicit channel eligibility",
                )
            )
        else:
            result.review_reasons.append(
                ("unknown_collection_type", f"Unknown shared special '{parts[2]}'")
            )
        return

    if second in dayparts:
        result.daypart_slug = second
        _assign_media_type(parts[2], result)
        if result.media_type not in {"episode", "commercial", "filler", "bumper", "promo"}:
            result.review_reasons.append(
                (
                    "shared_daypart_type",
                    "Shared daypart folders support episodes and interstitial media",
                )
            )
        if result.media_type == "episode" and len(parts) > 4:
            result.series_title = humanize(parts[3])
        expanded = _expanded_dayparts(second, dayparts)
        for daypart in expanded:
            result.eligibility.append(
                Eligibility("*", daypart, "shared_daypart_folder")
            )
        return

    # Compatibility shorthand: media/shared/commercials/file.ext
    _assign_media_type(parts[1], result)
    if result.media_type in {"commercial", "filler"}:
        result.daypart_slug = "all-day"
        for daypart in _expanded_dayparts("all-day", dayparts):
            result.eligibility.append(
                Eligibility("*", daypart, "shared_legacy_all_day")
            )
    else:
        result.review_reasons.append(
            ("unrecognized_path", "Unrecognized shared media folder layout")
        )
