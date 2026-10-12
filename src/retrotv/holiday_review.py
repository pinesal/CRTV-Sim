"""Advisory holiday filename clues and explicitly requested library moves."""

from __future__ import annotations

import re
from pathlib import Path

from retrotv.classification import CHANNEL_DAYPARTS, classify_path, slugify
from retrotv.db import utc_now


# Add words here, or override/add a holiday with library.holiday_filename_clues
# in the profile. Each phrase is matched as whole words, ignoring separators.
DEFAULT_CLUES: dict[str, tuple[str, ...]] = {
    "halloween": (
        "halloween", "hallowe en", "all hallows eve", "trick or treat", "trick or", "or treat",
        "trick r treat", "r treat", "haunted", "haunting", "horror", "pumpkin",
        "jack o lantern", "jackolantern", "spooky", "spooktacular", "ghost",
        "witch", "witches", "vampire", "monster mash", "frankenstein",
        "scary", "creepy", "costume party",
    ),
    "christmas": (
        "christmas", "xmas", "x mas", "yuletide", "yule", "santa", "saint nick",
        "st nick", "north pole", "reindeer", "rudolph", "mistletoe", "snowman",
        "silent night", "jingle bells", "christmas eve", "holiday special",
    ),
    "thanksgiving": (
        "thanksgiving", "thanks giving", "turkey day", "pilgrim", "cornucopia",
    ),
    "easter": ("easter", "easter bunny", "easter egg", "egg hunt", "good friday"),
    "new-years": (
        "new year", "new years", "new year's", "nye", "auld lang syne",
        "countdown to midnight",
    ),
    "valentines-day": (
        "valentine", "valentines", "valentine's", "cupid", "be my valentine",
    ),
    "independence-day": (
        "fourth of july", "4th of july", "july 4", "independence day",
    ),
    "st-patricks-day": (
        "st patrick", "saint patrick", "st paddy", "shamrock", "leprechaun",
    ),
}

def configured_holidays(config):
    return {slugify(str(value)) for value in
            (*config.data.get("seasonal", {}).get("holidays", {}),
             *config.data.get("library", {}).get("holidays", [])) if slugify(str(value))}


def clue_settings(config):
    groups = {key: list(value) for key, value in DEFAULT_CLUES.items()}
    groups.update(config.data.get("library", {}).get("holiday_filename_clues", {}))
    for slug in configured_holidays(config):
        groups.setdefault(slug, [slug.replace("-", " ")])
    from retrotv.bumper_cues import configured_bumper_clues, DEFAULT_BUMPER_CLUES
    return {"holidays": groups,
            "bumpers": dict(zip(("break_start", "break_end"), configured_bumper_clues(config))),
            "mandatory_holidays": sorted(configured_holidays(config)),
            "defaults": {"holidays": {**{k: list(v) for k, v in DEFAULT_CLUES.items()},
                                      **{k: [k.replace("-", " ")] for k in configured_holidays(config) if k not in DEFAULT_CLUES}},
                         "bumpers": {k: list(v) for k, v in DEFAULT_BUMPER_CLUES.items()}}}


def validate_clue_lists(raw, *, bumper=False):
    if not isinstance(raw, dict):
        raise ValueError("Filename clues must be a group-to-phrases mapping")
    result = {}
    for group, phrases in raw.items():
        slug = str(group) if bumper and group in {"break_start", "break_end"} else slugify(str(group))
        if not slug or slug != group or not isinstance(phrases, list):
            raise ValueError("Choose a valid clue group and a list of phrases")
        clean, seen = [], set()
        for phrase in phrases:
            if not isinstance(phrase, str) or not _words(phrase) or len(phrase) > 200:
                raise ValueError("Clues must be nonempty phrases of at most 200 characters")
            key = "".join(_words(phrase))
            if key not in seen:
                seen.add(key); clean.append(phrase.strip())
        result[slug] = clean
    return result


MEDIA_FOLDERS = {"episodes", "movies", "commercials", "bumpers", "promos",
                 "filler", "shorts", "blocks", "interstitials", "station-ids"}


def destination_relative_path(source_relative_path: str, folder: str) -> Path:
    """Preserve a series' subfolders when a category root is selected."""
    destination = Path(folder.strip().replace("\\", "/"))
    if destination.is_absolute() or not destination.parts or ".." in destination.parts:
        raise ValueError("Choose a folder inside the media library")
    source = Path(source_relative_path.replace("\\", "/"))
    category_positions = [index for index, part in enumerate(source.parts[:-1])
                          if part.casefold() in MEDIA_FOLDERS]
    if category_positions:
        category_index = category_positions[-1]
        descendants = source.parts[category_index + 1:-1]
        # Only append when the selected destination is the category root.
        # An explicitly chosen series or custom folder is honored literally.
        if destination.name.casefold() == source.parts[category_index].casefold():
            destination = destination.joinpath(*descendants)
    return destination / source.name


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _pattern(clue: str) -> re.Pattern[str] | None:
    words = _words(clue)
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(map(re.escape, words))
                      + r"(?![a-z0-9])", re.IGNORECASE)


class HolidayReviewService:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    def suggested_destination(self, relative: str, holiday: str) -> str:
        parts = Path(relative.replace("\\", "/")).parts
        if not parts or ".." in parts or Path(relative).is_absolute():
            return ""
        roots = {str(row["slug"]) for row in self.connection.execute("SELECT slug FROM channels")}
        if slugify(parts[0]) not in roots | {"shared"}:
            return ""
        offset = 1
        if tuple(part.casefold() for part in parts[1:3]) == ("specials", "holidays"):
            offset = 4
        dayparts = CHANNEL_DAYPARTS | {slugify(str(item["slug"])) for item in
                    self.config.data.get("scheduling", {}).get("dayparts", [])
                    if isinstance(item, dict) and item.get("slug")}
        if (len(parts) <= offset + 2 or slugify(parts[offset]) not in dayparts
                or parts[offset + 1].casefold() not in MEDIA_FOLDERS or slugify(holiday) != holiday):
            return ""
        return Path(parts[0], "specials", "holidays", holiday,
                    parts[offset], parts[offset + 1]).as_posix()

    def clues(self) -> dict[str, tuple[str, ...]]:
        result = dict(DEFAULT_CLUES)
        custom = self.config.data.get("library", {}).get("holiday_filename_clues", {})
        if not isinstance(custom, dict):
            raise ValueError("library.holiday_filename_clues must be a holiday-to-phrases mapping")
        for holiday, phrases in custom.items():
            slug = slugify(str(holiday))
            if not slug or not isinstance(phrases, list) or not all(
                isinstance(phrase, str) and _words(phrase) for phrase in phrases
            ):
                raise ValueError(f"Invalid filename clues for holiday {holiday!r}")
            result[slug] = tuple(phrases)
        for holiday in configured_holidays(self.config):
            result[holiday] = tuple(dict.fromkeys((*result.get(holiday, ()), holiday, holiday.replace("-", " "))))
        return result

    def matches(self, filename: str) -> dict[str, str]:
        # Keep the first clue in each holiday group. Show the actual filename
        # spelling/punctuation that caused the match, not a normalized guess.
        result: dict[str, str] = {}
        stem = Path(filename).stem
        for holiday, clues in self.clues().items():
            for clue in clues:
                pattern = _pattern(clue)
                match = pattern.search(stem) if pattern else None
                if match:
                    result[holiday] = match.group(0)
                    break
        return result

    def reconcile(self) -> int:
        now = utc_now()
        prior = {
            (int(row["asset_id"]), str(row["holiday_slug"])): row
            for row in self.connection.execute("""
                SELECT asset_id,holiday_slug,filename,status,matched_clue
                FROM holiday_filename_reviews
            """).fetchall()
        }
        rows = self.connection.execute("""
            SELECT a.id,a.original_filename,a.source_relative_path,a.holiday_slug
            FROM assets a JOIN storage_roots r ON r.id=a.source_root_id
            WHERE r.logical_name='media' AND a.integrity_status!='missing'
        """).fetchall()
        pending = 0
        for row in rows:
            asset_id = int(row["id"])
            filename = str(row["original_filename"])
            matches = self.matches(filename)
            for holiday in list(matches):
                if holiday == row["holiday_slug"]:
                    del matches[holiday]
            for holiday, clue in matches.items():
                previous = prior.pop((asset_id, holiday), None)
                if previous is None or previous["filename"] != filename:
                    self.connection.execute("""
                        INSERT INTO holiday_filename_reviews
                        (asset_id,holiday_slug,filename,matched_clue,status,updated_at)
                        VALUES (?,?,?,?,'pending',?)
                        ON CONFLICT(asset_id,holiday_slug) DO UPDATE SET
                        filename=excluded.filename,matched_clue=excluded.matched_clue,
                        status='pending',updated_at=excluded.updated_at
                    """, (asset_id, holiday, filename, clue, now))
                elif previous["matched_clue"] != clue:
                    self.connection.execute("""
                        UPDATE holiday_filename_reviews SET matched_clue=?,updated_at=?
                        WHERE asset_id=? AND holiday_slug=?
                    """, (clue, now, asset_id, holiday))
                if previous is None or previous["filename"] != filename or previous["status"] == "pending":
                    pending += 1
        for asset_id, holiday in prior:
            self.connection.execute("""
                DELETE FROM holiday_filename_reviews WHERE asset_id=? AND holiday_slug=?
            """, (asset_id, holiday))
        self.connection.commit()
        return pending

    def pending(self) -> list[dict]:
        rows = self.connection.execute("""
            SELECT r.asset_id,r.holiday_slug,r.matched_clue,a.original_filename AS filename,
                   a.source_relative_path,a.media_type
            FROM holiday_filename_reviews r JOIN assets a ON a.id=r.asset_id
            WHERE r.status='pending'
            ORDER BY r.holiday_slug,a.source_relative_path
        """).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["suggested_destination_folder"] = self.suggested_destination(
                str(item["source_relative_path"]), str(item["holiday_slug"]))
            source = Path(str(item["source_relative_path"]).replace("\\", "/"))
            positions = [index for index, part in enumerate(source.parts[:-1])
                         if part.casefold() in MEDIA_FOLDERS]
            item["source_category"] = (source.parts[positions[-1]] if positions else "")
            item["source_subfolders"] = (list(source.parts[positions[-1] + 1:-1])
                                         if positions else [])
        return items

    def dismiss(self, asset_id: int, holiday: str) -> None:
        cursor = self.connection.execute("""
            UPDATE holiday_filename_reviews SET status='dismissed',updated_at=?
            WHERE asset_id=? AND holiday_slug=? AND status='pending'
        """, (utc_now(), asset_id, holiday))
        if not cursor.rowcount:
            raise ValueError("Holiday suggestion is no longer pending")
        self.connection.commit()

    def move(self, asset_id: int, holiday: str, folder: str, *, active_path: str | None = None) -> dict:
        from retrotv.asset_paths import require_managed_asset
        require_managed_asset(self.connection, self.config, asset_id)
        review = self.connection.execute("""
            SELECT a.* FROM holiday_filename_reviews r JOIN assets a ON a.id=r.asset_id
            WHERE r.asset_id=? AND r.holiday_slug=? AND r.status='pending'
        """, (asset_id, holiday)).fetchone()
        if review is None:
            raise ValueError("Holiday suggestion is no longer pending")
        root = self.config.storage_root("media").resolve()
        relative_target = destination_relative_path(str(review["source_relative_path"]), folder)
        source = (root / str(review["source_relative_path"])).resolve()
        target = (root / relative_target).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(root):
            raise ValueError("Destination escapes the media library")
        if source == target:
            raise ValueError("Choose a different destination folder")
        if not source.is_file():
            raise ValueError("Source file is missing")
        if active_path and source == Path(active_path).resolve():
            raise ValueError("Stop or change playback before moving the active file")
        if target.exists():
            raise ValueError("Destination already exists")
        known_channels = {str(row["slug"]) for row in self.connection.execute("SELECT slug FROM channels")}
        holidays = {slugify(str(value)) for value in self.config.data.get("seasonal", {}).get("holidays", {})}
        holidays.update(slugify(str(value)) for value in self.config.data.get("library", {}).get("holidays", []))
        dayparts = CHANNEL_DAYPARTS | {slugify(str(item["slug"])) for item in
                   self.config.data.get("scheduling", {}).get("dayparts", [])
                   if isinstance(item, dict) and item.get("slug")}
        relative = target.relative_to(root).as_posix()
        classification = classify_path(relative, known_channels=known_channels,
                                       known_holidays=holidays, known_dayparts=dayparts)
        other_open = self.connection.execute("""
            SELECT 1 FROM review_items WHERE asset_id=? AND status='open'
            AND reason_code!='holiday_move_path' LIMIT 1
        """, (asset_id,)).fetchone() is not None
        resolved_path_only = (classification.is_ready and review["review_status"] == "open"
                              and not other_open and review["metadata_status"] == "ready"
                              and review["integrity_status"] == "ready")
        review_status = ("open" if not classification.is_ready else
                         "ready" if resolved_path_only else review["review_status"])
        enabled = (0 if not classification.is_ready else
                   1 if resolved_path_only else int(review["enabled_for_scheduling"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        try:
            self.connection.execute("""
                UPDATE assets SET source_relative_path=?,original_filename=?,media_type=?,
                    channel_slug=?,daypart_slug=?,collection_type=?,collection_slug=?,
                    holiday_slug=?,holiday_weekly_slug=?,series_title=?,display_title=?,
                    review_status=?,enabled_for_scheduling=?,
                    last_seen_at=? WHERE id=?
            """, (relative, target.name, classification.media_type, classification.channel_slug,
                  classification.daypart_slug, classification.collection_type,
                  classification.collection_slug, classification.holiday_slug,
                  classification.holiday_weekly_slug, classification.series_title,
                  classification.display_title, review_status, enabled, utc_now(), asset_id))
            self.connection.execute("DELETE FROM asset_eligibility WHERE asset_id=?", (asset_id,))
            self.connection.executemany("""
                INSERT INTO asset_eligibility(asset_id,channel_slug,daypart_slug,
                    eligibility_source,weight,enabled) VALUES (?,?,?,?,?,1)
            """, [(asset_id, item.channel_slug, item.daypart_slug, item.source, item.weight)
                  for item in classification.eligibility])
            self.connection.execute("""
                UPDATE review_items SET status='resolved',resolved_at=?
                WHERE asset_id=? AND reason_code='holiday_move_path' AND status='open'
            """, (utc_now(), asset_id))
            if classification.review_reasons:
                self.connection.execute("""
                    INSERT INTO review_items(asset_id,reason_code,detail,status,created_at)
                    VALUES (?,'holiday_move_path',?,'open',?)
                """, (asset_id, classification.review_reasons[0][1], utc_now()))
            # A deliberate move resolves every clue for this filename, even
            # when the user chose a non-holiday destination.
            self.connection.execute("""
                UPDATE holiday_filename_reviews SET status='moved',updated_at=?
                WHERE asset_id=?
            """, (utc_now(), asset_id))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            target.rename(source)
            raise
        return {"moved_to": relative, "library_review_needed": bool(classification.review_reasons)}
