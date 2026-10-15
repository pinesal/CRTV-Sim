"""Portable, per-series audio and subtitle preferences for mpv playback."""
from __future__ import annotations

import json
from pathlib import Path

from retrotv.onboarding import atomic_json


_LANGUAGES = {
    "eng": "en", "english": "en", "jpn": "ja", "japanese": "ja",
    "fra": "fr", "fre": "fr", "french": "fr", "deu": "de",
    "ger": "de", "german": "de", "spa": "es", "spanish": "es",
    "ita": "it", "italian": "it", "por": "pt", "portuguese": "pt",
    "zho": "zh", "chi": "zh", "chinese": "zh", "kor": "ko",
    "korean": "ko", "rus": "ru", "russian": "ru", "nld": "nl",
    "dut": "nl", "dutch": "nl",
}
_ROLES = ("forced", "hearing-impaired", "visual-impaired", "external")


def _language(value) -> str:
    value = str(value or "").strip().casefold().replace("_", "-")
    if value in {"und", "unknown"}:
        return ""
    base, separator, region = value.partition("-")
    return _LANGUAGES.get(base, base) + (separator + region if separator else "")


def _title(value) -> str:
    return " ".join(str(value or "").casefold().split())


def _metadata(track: dict) -> dict:
    return {
        "language": _language(track.get("lang")),
        "title": _title(track.get("title")),
        **{role: bool(track.get(role)) for role in _ROLES},
    }


def describe_choice(tracks: list[dict], kind: str, selected_id) -> dict | None:
    """Describe a choice without storing mpv's file-specific track ID."""
    eligible = [track for track in tracks if track.get("type") == kind]
    if not eligible:
        # An episode with no such streams is not a request to disable them.
        return None
    if selected_id is None:
        return {"mode": "off"}
    selected = next((track for track in eligible if track.get("id") == selected_id), None)
    if selected is None:
        return None
    metadata = _metadata(selected)
    peers = [track for track in eligible if _metadata(track) == metadata]
    return {
        "mode": "track", **metadata,
        "ordinal": peers.index(selected), "index": eligible.index(selected),
    }


def match_choice(tracks: list[dict], kind: str, preference: dict):
    """Return a matching ID, 'no', or None to keep this file's default."""
    if preference.get("mode") == "off":
        return "no"
    if preference.get("mode") != "track":
        return None
    eligible = [track for track in tracks if track.get("type") == kind]
    language = preference.get("language", "")
    title = preference.get("title", "")
    if not language and not title:
        # Some libraries have no track tags. Their relative order is the only
        # available clue; never reuse a numeric ID from another file.
        index = preference.get("index")
        if isinstance(index, int) and 0 <= index < len(eligible):
            track = eligible[index]
            if all(bool(track.get(role)) == bool(preference.get(role)) for role in _ROLES):
                return track.get("id")
        return None
    candidates = eligible
    if language:
        candidates = [track for track in candidates if _language(track.get("lang")) == language]
    if title:
        exact = [track for track in candidates if _title(track.get("title")) == title]
        # A missing title may still match the language. A different title may
        # represent commentary or another dub and should not be guessed.
        candidates = exact or (
            [track for track in candidates if not _title(track.get("title"))]
            if language else []
        )
    candidates = [track for track in candidates if all(
        bool(track.get(role)) == bool(preference.get(role)) for role in _ROLES
    )]
    ordinal = preference.get("ordinal", 0)
    if not isinstance(ordinal, int) or ordinal < 0 or ordinal >= len(candidates):
        return None
    return candidates[ordinal].get("id")


class SeriesTrackPreferences:
    """Remember exact series names across channels without touching media files."""

    def __init__(self, path: Path):
        self.path = path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"version": 1, "series": {}}
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or not isinstance(payload.get("series"), dict)):
            raise ValueError("Invalid series track preferences")
        self.series = payload["series"]

    def get(self, series: str) -> dict:
        value = self.series.get(series, {})
        return value.copy() if isinstance(value, dict) else {}

    def update(self, series: str, changes: dict) -> None:
        value = self.get(series)
        value.update(changes)
        self.series[series] = value
        atomic_json(self.path, {"version": 1, "series": self.series})
