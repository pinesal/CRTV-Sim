from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MODEL_SCHEMA_VERSION = 1
UNIT_SYSTEMS = frozenset({"imperial", "metric"})


class SnapshotValidationError(ValueError):
    """Raised when a normalized weather snapshot violates its schema."""


class UnsupportedSnapshotVersion(SnapshotValidationError):
    """Raised when a snapshot uses a schema this application cannot read."""


def _fail(path: str, message: str) -> None:
    raise SnapshotValidationError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "must be an object")
    return value


def _keys(
    value: Mapping[str, Any],
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    if missing:
        _fail(path, f"missing required field(s): {', '.join(sorted(missing))}")
    unknown = value.keys() - required - optional
    if unknown:
        _fail(path, f"unknown field(s): {', '.join(sorted(unknown))}")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be non-empty text")
    return value


def _optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _text(value, path)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        _fail(path, "must be a finite number")
    return result


def _optional_number(value: Any, path: str) -> float | None:
    if value is None:
        return None
    return _number(value, path)


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")
    return value


def _optional_integer(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return _integer(value, path)


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be true or false")
    return value


def _datetime(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        _fail(path, "must be an ISO 8601 timestamp with an offset")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(candidate)
    except ValueError:
        _fail(path, "must be a valid ISO 8601 timestamp with an offset")
    if result.tzinfo is None or result.utcoffset() is None:
        _fail(path, "must include a UTC offset")
    return result


def _optional_datetime(value: Any, path: str) -> datetime | None:
    if value is None:
        return None
    return _datetime(value, path)


def _date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        _fail(path, "must be an ISO 8601 date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(path, "must be a valid ISO 8601 date")


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(path, "must be an array")
    return value


def _range(value: float | None, path: str, low: float, high: float) -> None:
    if value is not None and not low <= value <= high:
        _fail(path, f"must be between {low:g} and {high:g}")


def _nonnegative(value: float | None, path: str) -> None:
    if value is not None and value < 0:
        _fail(path, "must be zero or greater")


@dataclass(frozen=True)
class Location:
    display_name: str
    latitude: float
    longitude: float
    timezone: str
    country: str
    administrative_area: str | None

    def __post_init__(self) -> None:
        _text(self.display_name, "location.display_name")
        _range(_number(self.latitude, "location.latitude"), "location.latitude", -90, 90)
        _range(_number(self.longitude, "location.longitude"), "location.longitude", -180, 180)
        timezone = _text(self.timezone, "location.timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            _fail("location.timezone", "must be a known IANA timezone")
        _text(self.country, "location.country")
        _optional_text(self.administrative_area, "location.administrative_area")

    @classmethod
    def from_dict(cls, raw: Any) -> "Location":
        value = _mapping(raw, "location")
        _keys(
            value,
            "location",
            {"display_name", "latitude", "longitude", "timezone", "country", "administrative_area"},
        )
        return cls(
            display_name=_text(value["display_name"], "location.display_name"),
            latitude=_number(value["latitude"], "location.latitude"),
            longitude=_number(value["longitude"], "location.longitude"),
            timezone=_text(value["timezone"], "location.timezone"),
            country=_text(value["country"], "location.country"),
            administrative_area=_optional_text(
                value["administrative_area"], "location.administrative_area"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "country": self.country,
            "administrative_area": self.administrative_area,
        }


@dataclass(frozen=True)
class CurrentConditions:
    observed_at: datetime
    temperature: float | None
    apparent_temperature: float | None
    condition_code: int | None
    condition_text: str
    is_day: bool
    humidity: float | None
    wind_speed: float | None
    wind_direction: float | None
    wind_gust: float | None
    precipitation: float | None
    visibility: float | None
    pressure: float | None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            _fail("current.observed_at", "must include a UTC offset")
        _optional_number(self.temperature, "current.temperature")
        _optional_number(self.apparent_temperature, "current.apparent_temperature")
        _optional_integer(self.condition_code, "current.condition_code")
        _text(self.condition_text, "current.condition_text")
        _boolean(self.is_day, "current.is_day")
        humidity = _optional_number(self.humidity, "current.humidity")
        wind_speed = _optional_number(self.wind_speed, "current.wind_speed")
        wind_direction = _optional_number(self.wind_direction, "current.wind_direction")
        wind_gust = _optional_number(self.wind_gust, "current.wind_gust")
        precipitation = _optional_number(self.precipitation, "current.precipitation")
        visibility = _optional_number(self.visibility, "current.visibility")
        pressure = _optional_number(self.pressure, "current.pressure")
        _range(humidity, "current.humidity", 0, 100)
        _nonnegative(wind_speed, "current.wind_speed")
        _range(wind_direction, "current.wind_direction", 0, 360)
        _nonnegative(wind_gust, "current.wind_gust")
        _nonnegative(precipitation, "current.precipitation")
        _nonnegative(visibility, "current.visibility")
        if pressure is not None and pressure <= 0:
            _fail("current.pressure", "must be greater than zero")

    @classmethod
    def from_dict(cls, raw: Any) -> "CurrentConditions":
        value = _mapping(raw, "current")
        fields = {
            "observed_at", "temperature", "apparent_temperature", "condition_code",
            "condition_text", "is_day", "humidity", "wind_speed", "wind_direction",
            "wind_gust", "precipitation", "visibility", "pressure",
        }
        _keys(value, "current", fields)
        return cls(
            observed_at=_datetime(value["observed_at"], "current.observed_at"),
            temperature=_optional_number(value["temperature"], "current.temperature"),
            apparent_temperature=_optional_number(
                value["apparent_temperature"], "current.apparent_temperature"
            ),
            condition_code=_optional_integer(value["condition_code"], "current.condition_code"),
            condition_text=_text(value["condition_text"], "current.condition_text"),
            is_day=_boolean(value["is_day"], "current.is_day"),
            humidity=_optional_number(value["humidity"], "current.humidity"),
            wind_speed=_optional_number(value["wind_speed"], "current.wind_speed"),
            wind_direction=_optional_number(value["wind_direction"], "current.wind_direction"),
            wind_gust=_optional_number(value["wind_gust"], "current.wind_gust"),
            precipitation=_optional_number(value["precipitation"], "current.precipitation"),
            visibility=_optional_number(value["visibility"], "current.visibility"),
            pressure=_optional_number(value["pressure"], "current.pressure"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "temperature": self.temperature,
            "apparent_temperature": self.apparent_temperature,
            "condition_code": self.condition_code,
            "condition_text": self.condition_text,
            "is_day": self.is_day,
            "humidity": self.humidity,
            "wind_speed": self.wind_speed,
            "wind_direction": self.wind_direction,
            "wind_gust": self.wind_gust,
            "precipitation": self.precipitation,
            "visibility": self.visibility,
            "pressure": self.pressure,
        }


@dataclass(frozen=True)
class HourlyForecast:
    starts_at: datetime
    temperature: float | None
    apparent_temperature: float | None
    condition_code: int | None
    precipitation_probability: float | None
    precipitation_amount: float | None
    wind_speed: float | None
    wind_gust: float | None
    is_day: bool

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            _fail("hourly[].starts_at", "must include a UTC offset")
        _optional_number(self.temperature, "hourly[].temperature")
        _optional_number(self.apparent_temperature, "hourly[].apparent_temperature")
        _optional_integer(self.condition_code, "hourly[].condition_code")
        probability = _optional_number(
            self.precipitation_probability, "hourly[].precipitation_probability"
        )
        amount = _optional_number(self.precipitation_amount, "hourly[].precipitation_amount")
        wind_speed = _optional_number(self.wind_speed, "hourly[].wind_speed")
        wind_gust = _optional_number(self.wind_gust, "hourly[].wind_gust")
        _range(probability, "hourly[].precipitation_probability", 0, 100)
        _nonnegative(amount, "hourly[].precipitation_amount")
        _nonnegative(wind_speed, "hourly[].wind_speed")
        _nonnegative(wind_gust, "hourly[].wind_gust")
        _boolean(self.is_day, "hourly[].is_day")

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "HourlyForecast":
        path = f"hourly[{index}]"
        value = _mapping(raw, path)
        fields = {
            "starts_at", "temperature", "apparent_temperature", "condition_code",
            "precipitation_probability", "precipitation_amount", "wind_speed",
            "wind_gust", "is_day",
        }
        _keys(value, path, fields)
        return cls(
            starts_at=_datetime(value["starts_at"], f"{path}.starts_at"),
            temperature=_optional_number(value["temperature"], f"{path}.temperature"),
            apparent_temperature=_optional_number(
                value["apparent_temperature"], f"{path}.apparent_temperature"
            ),
            condition_code=_optional_integer(value["condition_code"], f"{path}.condition_code"),
            precipitation_probability=_optional_number(
                value["precipitation_probability"], f"{path}.precipitation_probability"
            ),
            precipitation_amount=_optional_number(
                value["precipitation_amount"], f"{path}.precipitation_amount"
            ),
            wind_speed=_optional_number(value["wind_speed"], f"{path}.wind_speed"),
            wind_gust=_optional_number(value["wind_gust"], f"{path}.wind_gust"),
            is_day=_boolean(value["is_day"], f"{path}.is_day"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "starts_at": self.starts_at.isoformat(),
            "temperature": self.temperature,
            "apparent_temperature": self.apparent_temperature,
            "condition_code": self.condition_code,
            "precipitation_probability": self.precipitation_probability,
            "precipitation_amount": self.precipitation_amount,
            "wind_speed": self.wind_speed,
            "wind_gust": self.wind_gust,
            "is_day": self.is_day,
        }


@dataclass(frozen=True)
class DailyForecast:
    date: date
    high_temperature: float | None
    low_temperature: float | None
    condition_code: int | None
    precipitation_probability: float | None
    sunrise: datetime | None
    sunset: datetime | None

    def __post_init__(self) -> None:
        _optional_number(self.high_temperature, "daily[].high_temperature")
        _optional_number(self.low_temperature, "daily[].low_temperature")
        _optional_integer(self.condition_code, "daily[].condition_code")
        probability = _optional_number(
            self.precipitation_probability, "daily[].precipitation_probability"
        )
        _range(probability, "daily[].precipitation_probability", 0, 100)
        for name, value in (("sunrise", self.sunrise), ("sunset", self.sunset)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                _fail(f"daily[].{name}", "must include a UTC offset")
        if self.sunrise is not None and self.sunset is not None and self.sunset <= self.sunrise:
            _fail("daily[]", "sunset must be later than sunrise")

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "DailyForecast":
        path = f"daily[{index}]"
        value = _mapping(raw, path)
        fields = {
            "date", "high_temperature", "low_temperature", "condition_code",
            "precipitation_probability", "sunrise", "sunset",
        }
        _keys(value, path, fields)
        return cls(
            date=_date(value["date"], f"{path}.date"),
            high_temperature=_optional_number(
                value["high_temperature"], f"{path}.high_temperature"
            ),
            low_temperature=_optional_number(value["low_temperature"], f"{path}.low_temperature"),
            condition_code=_optional_integer(value["condition_code"], f"{path}.condition_code"),
            precipitation_probability=_optional_number(
                value["precipitation_probability"], f"{path}.precipitation_probability"
            ),
            sunrise=_optional_datetime(value["sunrise"], f"{path}.sunrise"),
            sunset=_optional_datetime(value["sunset"], f"{path}.sunset"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "high_temperature": self.high_temperature,
            "low_temperature": self.low_temperature,
            "condition_code": self.condition_code,
            "precipitation_probability": self.precipitation_probability,
            "sunrise": self.sunrise.isoformat() if self.sunrise else None,
            "sunset": self.sunset.isoformat() if self.sunset else None,
        }


@dataclass(frozen=True)
class WeatherAlert:
    identifier: str
    severity: str
    headline: str
    starts_at: datetime
    ends_at: datetime | None
    source: str

    def __post_init__(self) -> None:
        _text(self.identifier, "alerts[].identifier")
        _text(self.severity, "alerts[].severity")
        _text(self.headline, "alerts[].headline")
        _text(self.source, "alerts[].source")
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            _fail("alerts[].starts_at", "must include a UTC offset")
        if self.ends_at is not None:
            if self.ends_at.tzinfo is None or self.ends_at.utcoffset() is None:
                _fail("alerts[].ends_at", "must include a UTC offset")
            if self.ends_at <= self.starts_at:
                _fail("alerts[].ends_at", "must be later than starts_at")

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "WeatherAlert":
        path = f"alerts[{index}]"
        value = _mapping(raw, path)
        fields = {"identifier", "severity", "headline", "starts_at", "ends_at", "source"}
        _keys(value, path, fields)
        return cls(
            identifier=_text(value["identifier"], f"{path}.identifier"),
            severity=_text(value["severity"], f"{path}.severity"),
            headline=_text(value["headline"], f"{path}.headline"),
            starts_at=_datetime(value["starts_at"], f"{path}.starts_at"),
            ends_at=_optional_datetime(value["ends_at"], f"{path}.ends_at"),
            source=_text(value["source"], f"{path}.source"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "severity": self.severity,
            "headline": self.headline,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "source": self.source,
        }


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    fetched_at: datetime
    expires_at: datetime
    attribution: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.provider, "metadata.provider")
        for name, value in (("fetched_at", self.fetched_at), ("expires_at", self.expires_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                _fail(f"metadata.{name}", "must include a UTC offset")
        if self.expires_at <= self.fetched_at:
            _fail("metadata.expires_at", "must be later than fetched_at")
        if not self.attribution:
            _fail("metadata.attribution", "must contain at least one entry")
        for index, item in enumerate(self.attribution):
            _text(item, f"metadata.attribution[{index}]")

    @classmethod
    def from_dict(cls, raw: Any) -> "ProviderMetadata":
        value = _mapping(raw, "metadata")
        _keys(value, "metadata", {"provider", "fetched_at", "expires_at", "attribution"})
        attribution = _sequence(value["attribution"], "metadata.attribution")
        return cls(
            provider=_text(value["provider"], "metadata.provider"),
            fetched_at=_datetime(value["fetched_at"], "metadata.fetched_at"),
            expires_at=_datetime(value["expires_at"], "metadata.expires_at"),
            attribution=tuple(
                _text(item, f"metadata.attribution[{index}]")
                for index, item in enumerate(attribution)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "fetched_at": self.fetched_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "attribution": list(self.attribution),
        }


@dataclass(frozen=True)
class NormalizedWeatherSnapshot:
    schema_version: int
    units: str
    location: Location
    current: CurrentConditions
    hourly: tuple[HourlyForecast, ...]
    daily: tuple[DailyForecast, ...]
    alerts: tuple[WeatherAlert, ...]
    metadata: ProviderMetadata

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_SCHEMA_VERSION:
            raise UnsupportedSnapshotVersion(
                f"schema_version: expected {MODEL_SCHEMA_VERSION}, got {self.schema_version!r}"
            )
        if self.units not in UNIT_SYSTEMS:
            _fail("units", f"must be one of: {', '.join(sorted(UNIT_SYSTEMS))}")
        hourly_times = [item.starts_at.timestamp() for item in self.hourly]
        if hourly_times != sorted(hourly_times) or len(hourly_times) != len(set(hourly_times)):
            _fail("hourly", "must be strictly ordered by unique starts_at values")
        daily_dates = [item.date for item in self.daily]
        if daily_dates != sorted(daily_dates) or len(daily_dates) != len(set(daily_dates)):
            _fail("daily", "must be strictly ordered by unique date values")

    @classmethod
    def from_dict(cls, raw: Any) -> "NormalizedWeatherSnapshot":
        value = _mapping(raw, "snapshot")
        fields = {"schema_version", "units", "location", "current", "hourly", "daily", "alerts", "metadata"}
        _keys(value, "snapshot", fields)
        schema_version = _integer(value["schema_version"], "schema_version")
        if schema_version != MODEL_SCHEMA_VERSION:
            raise UnsupportedSnapshotVersion(
                f"schema_version: expected {MODEL_SCHEMA_VERSION}, got {schema_version}"
            )
        hourly = _sequence(value["hourly"], "hourly")
        daily = _sequence(value["daily"], "daily")
        alerts = _sequence(value["alerts"], "alerts")
        return cls(
            schema_version=schema_version,
            units=_text(value["units"], "units"),
            location=Location.from_dict(value["location"]),
            current=CurrentConditions.from_dict(value["current"]),
            hourly=tuple(HourlyForecast.from_dict(item, index) for index, item in enumerate(hourly)),
            daily=tuple(DailyForecast.from_dict(item, index) for index, item in enumerate(daily)),
            alerts=tuple(WeatherAlert.from_dict(item, index) for index, item in enumerate(alerts)),
            metadata=ProviderMetadata.from_dict(value["metadata"]),
        )

    @classmethod
    def load(cls, path: Path) -> "NormalizedWeatherSnapshot":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotValidationError(
                f"snapshot: invalid JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "units": self.units,
            "location": self.location.to_dict(),
            "current": self.current.to_dict(),
            "hourly": [item.to_dict() for item in self.hourly],
            "daily": [item.to_dict() for item in self.daily],
            "alerts": [item.to_dict() for item in self.alerts],
            "metadata": self.metadata.to_dict(),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
