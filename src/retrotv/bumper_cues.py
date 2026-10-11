"""Classify spoken bumper filenames for safe commercial-break placement."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class BumperCue:
    position: str = "general"
    series_hint: str | None = None


def _words(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = value.replace("’", "'").replace("&", " and ")
    value = re.sub(r"(?<=\w)'s\b", "", value)
    value = re.sub(r"\bn\b", "and", value)
    value = re.sub(r"\bwe ll\b", "we will", value)
    value = re.sub(r"\bwere back\b", "we are back", value)
    value = re.sub(r"\bdont\b", "do not", value)
    value = re.sub(r"\b(?:we're|we are)\b", "we are", value)
    value = re.sub(r"\b(?:we'll|we will)\b", "we will", value)
    value = re.sub(r"\b(?:don't|do not)\b", "do not", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _series_key(value: str) -> str:
    words = _words(value).split()
    if words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def series_matches(hint: str, series: str | None) -> bool:
    """Accept spelling/transcription drift without matching unrelated shows."""
    if not series:
        return False
    left, right = _series_key(hint), _series_key(series)
    if not left or not right:
        return False
    if left == right:
        return True
    # A one-word show name must match exactly; fuzzy one-word matches are too broad.
    if len(left.split()) < 2 or len(right.split()) < 2:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.86


def _fuzzy_phrase(
    text: str, phrases: tuple[str, ...], *, anchors: set[str] | None = None
) -> bool:
    tokens = text.split()
    anchors = anchors or {"back", "break", "messages", "return", "away"}
    for phrase in phrases:
        expected = phrase.split()
        for length in (len(expected) - 1, len(expected), len(expected) + 1):
            if length < 3:
                continue
            for index in range(len(tokens) - length + 1):
                window = tokens[index:index + length]
                if not anchors.intersection(window):
                    continue
                if SequenceMatcher(None, " ".join(window), phrase).ratio() >= 0.88:
                    return True
    return False


DEFAULT_BUMPER_CLUES = {
    "break_start": (
        "we will be right back", "we will be back", "be right back", "BRB",
        "be back right after", "be back after", "do not go away",
        "coming up after the break", "coming right up after the break",
        "coming up after this break", "after these messages",
        "after these important messages", "after this message", "after this messages",
        "after the messages", "after the break", "after this break", "will return",
    ),
    "break_end": ("welcome back", "we are back", "back to",
                  "now back to our program", "back to our show", "welcome back to the show"),
}


def configured_bumper_clues(config=None):
    custom = config.data.get("library", {}).get("bumper_filename_clues", {}) if config else {}
    return tuple(tuple(custom.get(key, DEFAULT_BUMPER_CLUES[key]))
                 for key in ("break_start", "break_end"))


def classify_bumper(filename: str, clues=None) -> BumperCue:
    return _classify_bumper(filename, tuple(tuple(group) for group in
                            (clues if clues is not None else configured_bumper_clues())))


@lru_cache(maxsize=4096)
def _classify_bumper(filename: str, clues) -> BumperCue:
    stem = Path(filename).stem
    text = _words(stem)
    if not text:
        return BumperCue()

    def matches(phrases, anchors):
        normalized = tuple(_words(phrase) for phrase in phrases)
        for phrase in normalized:
            if not phrase:
                continue
            pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"\s*") + r"\b"
            if phrase == "back to":
                pattern = r"(?:^|\b(?:and now|now) )back to\b"
            if phrase == "brb":
                pattern = r"\bb\s*r\s*b\b"
            if re.search(pattern, text):
                return True
        return _fuzzy_phrase(text, normalized, anchors=anchors)

    start = matches(clues[0], {"back", "break", "messages", "return", "away"})
    end = matches(clues[1], {"back", "program", "show"})
    return_to_program = re.search(
        r"(?:^|\b(?:and now|now) )back to (?:(?:our|your|the) )?(.+)", text,
    ) if end else None
    if start == end:
        return BumperCue()

    if start:
        # A named show's "will return" is meaningful only around that show.
        named = re.match(r"(.+?) will return\b", text)
        if named and named[1] not in {"we", "this show", "the show", "our program"}:
            return BumperCue("break_start", named[1])
        return BumperCue("break_start")

    if return_to_program:
        rest = return_to_program[1].split()
        if rest and rest[0] in {"program", "show", "regular", "series", "cartoon"}:
            return BumperCue("break_end")
        # Transcripts often append another sentence. A period ends the title.
        raw = re.search(
            r"(?:^|\b(?:and now|now)[,!\s]+)back\s+to\s+"
            r"(?:(?:our|your|the)\s+)?([^.!?]+)",
            stem, re.IGNORECASE,
        )
        hint = raw[1].strip() if raw else return_to_program[1]
        return BumperCue("break_end", hint)
    return BumperCue("break_end")


def bumper_cue_for_path(path: str, clues=None) -> BumperCue:
    return classify_bumper(Path(path).name, clues)


def effective_bumper_cue(
    path: str, override_position: str | None = None,
    override_series: str | None = None, clues=None,
) -> BumperCue:
    if override_position:
        return BumperCue(override_position, override_series)
    return bumper_cue_for_path(path, clues)


def known_series(connection: sqlite3.Connection) -> list[str]:
    titles = {
        str(row["series_title"]).strip()
        for row in connection.execute(
            """SELECT DISTINCT COALESCE(NULLIF(series_title,''),display_title) AS series_title
               FROM assets
               WHERE media_type IN ('episode','short','movie','complete_block')
                 AND integrity_status IN ('ready','not_scanned')"""
        )
        if row["series_title"]
    }
    titles.update(
        str(row["series_title"]).strip()
        for row in connection.execute(
            """SELECT DISTINCT series_title FROM shadow_files
               WHERE status='ready' AND series_title IS NOT NULL AND series_title != ''"""
        )
    )
    return sorted(titles, key=str.casefold)


def bumper_review_catalog(connection: sqlite3.Connection, config=None) -> dict[str, object]:
    series = known_series(connection)
    rows = connection.execute(
        """SELECT a.id,a.original_filename,a.source_relative_path,
                  b.position AS override_position,b.series_title AS override_series
           FROM assets a LEFT JOIN bumper_cue_overrides b ON b.asset_id=a.id
           WHERE a.media_type='bumper' AND a.enabled_for_scheduling=1
             AND a.review_status='ready'
             AND a.integrity_status IN ('ready','not_scanned')
           ORDER BY a.source_relative_path"""
    ).fetchall()
    items: list[dict[str, object]] = []
    pending_count = 0
    for row in rows:
        automatic = bumper_cue_for_path(str(row["source_relative_path"]), configured_bumper_clues(config))
        override_position = row["override_position"]
        effective = effective_bumper_cue(
            str(row["source_relative_path"]), override_position,
            row["override_series"], configured_bumper_clues(config),
        )
        if automatic.position == "general" and override_position is None:
            continue
        pending = bool(
            effective.series_hint
            and not any(series_matches(effective.series_hint, title) for title in series)
        )
        pending_count += int(pending)
        items.append({
            "id": int(row["id"]),
            "original_filename": row["original_filename"],
            "source_relative_path": row["source_relative_path"],
            "automatic_position": automatic.position,
            "automatic_series_hint": automatic.series_hint,
            "override_position": override_position,
            "override_series": row["override_series"],
            "review_required": pending,
        })
    return {"items": items, "pending_count": pending_count, "series": series}


def save_bumper_override(
    connection: sqlite3.Connection, asset_id: int,
    position: str, series_title: str | None,
) -> None:
    asset = connection.execute(
        "SELECT media_type FROM assets WHERE id=?", (asset_id,)
    ).fetchone()
    if asset is None or asset["media_type"] != "bumper":
        raise ValueError("Bumper asset was not found")
    if position not in {"auto", "general", "break_start", "break_end", "exclude"}:
        raise ValueError("Choose a valid bumper placement")
    series_title = (series_title or "").strip() or None
    if series_title and position not in {"break_start", "break_end"}:
        raise ValueError("A show can be linked only to a break bumper")
    if series_title:
        series = known_series(connection)
        matches = [title for title in series if title.casefold() == series_title.casefold()]
        if not matches:
            raise ValueError("Choose a show from the media library list")
        series_title = matches[0]
    if position == "auto":
        connection.execute(
            "DELETE FROM bumper_cue_overrides WHERE asset_id=?", (asset_id,)
        )
    else:
        connection.execute(
            """INSERT INTO bumper_cue_overrides(asset_id,position,series_title,updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(asset_id) DO UPDATE SET position=excluded.position,
                   series_title=excluded.series_title,updated_at=excluded.updated_at""",
            (asset_id, position, series_title, datetime.now(timezone.utc).isoformat()),
        )
    connection.commit()
