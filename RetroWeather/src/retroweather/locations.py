from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from retroweather.config import AppPaths, ConfigError, load_settings, save_settings
from retroweather.models import Location, SnapshotValidationError


@dataclass(frozen=True)
class LocationSelection:
    provider: str
    provider_location_id: int
    country_code: str | None
    location: Location

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ConfigError("location.provider must be non-empty text")
        if (
            isinstance(self.provider_location_id, bool)
            or not isinstance(self.provider_location_id, int)
            or self.provider_location_id <= 0
        ):
            raise ConfigError("location.provider_location_id must be a positive integer")
        if self.country_code is not None:
            if (
                not isinstance(self.country_code, str)
                or len(self.country_code) != 2
                or not self.country_code.isalpha()
            ):
                raise ConfigError("location.country_code must be a two-letter code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_location_id": self.provider_location_id,
            "country_code": self.country_code,
            "location": self.location.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "LocationSelection":
        if not isinstance(raw, Mapping):
            raise ConfigError("location must be an object")
        expected = {"provider", "provider_location_id", "country_code", "location"}
        missing = expected - raw.keys()
        unknown = raw.keys() - expected
        if missing:
            raise ConfigError(f"location is missing: {', '.join(sorted(missing))}")
        if unknown:
            raise ConfigError(f"location has unknown fields: {', '.join(sorted(unknown))}")
        try:
            location = Location.from_dict(raw["location"])
        except SnapshotValidationError as exc:
            raise ConfigError(str(exc)) from exc
        return cls(
            provider=raw["provider"],
            provider_location_id=raw["provider_location_id"],
            country_code=raw["country_code"],
            location=location,
        )


def load_selected_location(paths: AppPaths) -> LocationSelection | None:
    settings = load_settings(paths)
    raw = settings.get("location")
    return None if raw is None else LocationSelection.from_dict(raw)


def save_selected_location(paths: AppPaths, selection: LocationSelection) -> None:
    settings = load_settings(paths)
    settings["location"] = selection.to_dict()
    save_settings(paths, settings)
