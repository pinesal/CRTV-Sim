from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from retroweather.locations import LocationSelection
from retroweather.models import Location, SnapshotValidationError


GEOCODING_SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODING_GET_URL = "https://geocoding-api.open-meteo.com/v1/get"
GEOCODING_ATTRIBUTION = "Location data by GeoNames via Open-Meteo"


class LocationQueryError(ValueError):
    """Raised when a user-supplied location query is invalid."""


class ProviderRequestError(RuntimeError):
    """Raised when Open-Meteo cannot be reached or reports an API error."""


class ProviderResponseError(RuntimeError):
    """Raised when Open-Meteo returns malformed or unsafe location data."""


JsonTransport = Callable[[str, float], Any]


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError(f"{path}.{key} must be non-empty text")
    return value


def _optional_text(raw: Mapping[str, Any], key: str, path: str) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError(f"{path}.{key} must be text when present")
    return value


def _number(raw: Mapping[str, Any], key: str, path: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderResponseError(f"{path}.{key} must be a number")
    return float(value)


def _optional_number(raw: Mapping[str, Any], key: str, path: str) -> float | None:
    return None if raw.get(key) is None else _number(raw, key, path)


def _optional_integer(raw: Mapping[str, Any], key: str, path: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderResponseError(f"{path}.{key} must be an integer when present")
    return value


@dataclass(frozen=True)
class LocationSearchResult:
    provider_location_id: int
    name: str
    latitude: float
    longitude: float
    timezone: str
    country: str
    country_code: str | None
    administrative_area: str | None
    elevation: float | None
    population: int | None

    @property
    def display_name(self) -> str:
        values = (self.name, self.administrative_area, self.country)
        parts: list[str] = []
        for value in values:
            if value and value.casefold() not in {part.casefold() for part in parts}:
                parts.append(value)
        return ", ".join(parts)

    def to_location(self) -> Location:
        return Location(
            display_name=self.display_name,
            latitude=self.latitude,
            longitude=self.longitude,
            timezone=self.timezone,
            country=self.country,
            administrative_area=self.administrative_area,
        )

    def to_selection(self) -> LocationSelection:
        return LocationSelection(
            provider="open-meteo",
            provider_location_id=self.provider_location_id,
            country_code=self.country_code,
            location=self.to_location(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_location_id": self.provider_location_id,
            "name": self.name,
            "display_name": self.display_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "country": self.country,
            "country_code": self.country_code,
            "administrative_area": self.administrative_area,
            "elevation": self.elevation,
            "population": self.population,
        }

    @classmethod
    def from_api(cls, raw: Any, path: str = "result") -> "LocationSearchResult":
        if not isinstance(raw, Mapping):
            raise ProviderResponseError(f"{path} must be an object")
        identifier = raw.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise ProviderResponseError(f"{path}.id must be a positive integer")
        country_code = _optional_text(raw, "country_code", path)
        if country_code is not None:
            country_code = country_code.upper()
            if len(country_code) != 2 or not country_code.isalpha():
                raise ProviderResponseError(f"{path}.country_code must be a two-letter code")
        population = _optional_integer(raw, "population", path)
        if population is not None and population < 0:
            raise ProviderResponseError(f"{path}.population must not be negative")
        try:
            result = cls(
                provider_location_id=identifier,
                name=_text(raw, "name", path),
                latitude=_number(raw, "latitude", path),
                longitude=_number(raw, "longitude", path),
                timezone=_text(raw, "timezone", path),
                country=_text(raw, "country", path),
                country_code=country_code,
                administrative_area=_optional_text(raw, "admin1", path),
                elevation=_optional_number(raw, "elevation", path),
                population=population,
            )
            result.to_location()
            return result
        except SnapshotValidationError as exc:
            raise ProviderResponseError(f"{path}: {exc}") from exc


class OpenMeteoGeocodingClient:
    def __init__(
        self,
        timeout: float = 20.0,
        transport: JsonTransport | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = float(timeout)
        self.transport = transport or _request_json

    def search(
        self,
        query: str,
        count: int = 10,
        language: str = "en",
        country_code: str | None = None,
    ) -> tuple[LocationSearchResult, ...]:
        query = query.strip()
        if len(query) < 2:
            raise LocationQueryError("Location search must contain at least two characters.")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise LocationQueryError("Result count must be between 1 and 100.")
        if not isinstance(language, str) or not language.isalpha() or not 2 <= len(language) <= 3:
            raise LocationQueryError("Language must be a two- or three-letter code.")
        parameters: dict[str, str | int] = {
            "name": query,
            "count": count,
            "language": language.lower(),
            "format": "json",
        }
        if country_code is not None:
            normalized_country = country_code.strip().upper()
            if len(normalized_country) != 2 or not normalized_country.isalpha():
                raise LocationQueryError("Country code must contain two letters.")
            parameters["countryCode"] = normalized_country
        raw = self.transport(f"{GEOCODING_SEARCH_URL}?{urlencode(parameters)}", self.timeout)
        payload = _api_payload(raw)
        results = payload.get("results", [])
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            raise ProviderResponseError("response.results must be an array")
        return tuple(
            LocationSearchResult.from_api(item, f"response.results[{index}]")
            for index, item in enumerate(results)
        )

    def get(self, provider_location_id: int) -> LocationSearchResult:
        if (
            isinstance(provider_location_id, bool)
            or not isinstance(provider_location_id, int)
            or provider_location_id <= 0
        ):
            raise LocationQueryError("Location ID must be a positive integer.")
        url = f"{GEOCODING_GET_URL}?{urlencode({'id': provider_location_id})}"
        raw = self.transport(url, self.timeout)
        return LocationSearchResult.from_api(_api_payload(raw), "response")


def _api_payload(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ProviderResponseError("Open-Meteo response must be an object")
    if raw.get("error") is True:
        reason = raw.get("reason")
        detail = reason if isinstance(reason, str) and reason else "unknown API error"
        raise ProviderRequestError(f"Open-Meteo rejected the request: {detail}")
    return raw


def _request_json(url: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "RetroWeather/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            reason = payload.get("reason") if isinstance(payload, Mapping) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            reason = None
        detail = reason or f"HTTP {exc.code}"
        raise ProviderRequestError(f"Open-Meteo request failed: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderRequestError(f"Unable to reach Open-Meteo: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResponseError("Open-Meteo returned invalid JSON") from exc
