from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from retroweather.models import (
    MODEL_SCHEMA_VERSION,
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    Location,
    NormalizedWeatherSnapshot,
    ProviderMetadata,
)
from retroweather.providers.open_meteo import ProviderRequestError, ProviderResponseError


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_ATTRIBUTION = "Weather data by Open-Meteo"
CURRENT_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "is_day",
    "precipitation",
    "weather_code",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "visibility",
)
HOURLY_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "weather_code",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "is_day",
)
DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "sunrise",
    "sunset",
)


JsonTransport = Callable[[str, float], Any]
Clock = Callable[[], datetime]


WMO_CONDITIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def condition_text(code: int | None) -> str:
    return WMO_CONDITIONS.get(code, "Unknown conditions")


@dataclass(frozen=True)
class ForecastFetchResult:
    snapshot: NormalizedWeatherSnapshot
    raw_response: Any


class OpenMeteoForecastClient:
    def __init__(
        self,
        timeout: float = 20.0,
        transport: JsonTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = float(timeout)
        self.transport = transport or _request_json
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_raw_response: Any = None

    def fetch_snapshot(
        self,
        location: Location,
        units: str,
        expires_minutes: int = 15,
    ) -> NormalizedWeatherSnapshot:
        return self.fetch(location, units, expires_minutes).snapshot

    def fetch(
        self,
        location: Location,
        units: str,
        expires_minutes: int = 15,
    ) -> ForecastFetchResult:
        if units not in {"imperial", "metric"}:
            raise ValueError("units must be imperial or metric")
        if expires_minutes <= 0:
            raise ValueError("expires_minutes must be greater than zero")
        unit_parameters = (
            {
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
            }
            if units == "imperial"
            else {
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
            }
        )
        parameters: dict[str, str | int | float] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": location.timezone,
            "forecast_days": 7,
            "current": ",".join(CURRENT_FIELDS),
            "hourly": ",".join(HOURLY_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
            **unit_parameters,
        }
        raw = self.transport(f"{FORECAST_URL}?{urlencode(parameters)}", self.timeout)
        self.last_raw_response = raw
        payload = _payload(raw)
        returned_timezone = payload.get("timezone")
        if returned_timezone != location.timezone:
            raise ProviderResponseError(
                f"response.timezone must equal selected timezone {location.timezone!r}"
            )
        timezone_info = ZoneInfo(location.timezone)
        current = _parse_current(payload.get("current"), timezone_info, units)
        hourly = _parse_hourly(payload.get("hourly"), timezone_info)
        current_hour = current.observed_at.replace(minute=0, second=0, microsecond=0).timestamp()
        hourly = tuple(item for item in hourly if item.starts_at.timestamp() >= current_hour)
        daily = _parse_daily(payload.get("daily"), timezone_info)
        fetched_at = self.clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        snapshot = NormalizedWeatherSnapshot(
            schema_version=MODEL_SCHEMA_VERSION,
            units=units,
            location=location,
            current=current,
            hourly=hourly,
            daily=daily,
            alerts=(),
            metadata=ProviderMetadata(
                provider="open-meteo",
                fetched_at=fetched_at,
                expires_at=fetched_at + timedelta(minutes=expires_minutes),
                attribution=(WEATHER_ATTRIBUTION,),
            ),
        )
        return ForecastFetchResult(snapshot=snapshot, raw_response=raw)


def _payload(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ProviderResponseError("Open-Meteo forecast response must be an object")
    if raw.get("error") is True:
        reason = raw.get("reason")
        detail = reason if isinstance(reason, str) and reason else "unknown API error"
        raise ProviderRequestError(f"Open-Meteo rejected the forecast request: {detail}")
    return raw


def _object(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ProviderResponseError(f"{path} must be an object")
    return raw


def _array(raw: Mapping[str, Any], key: str, path: str) -> Sequence[Any]:
    value = raw.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProviderResponseError(f"{path}.{key} must be an array")
    return value


def _number(raw: Mapping[str, Any], key: str, path: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderResponseError(f"{path}.{key} must be a finite number or null")
    result = float(value)
    if not math.isfinite(result):
        raise ProviderResponseError(f"{path}.{key} must be a finite number or null")
    return result


def _integer(raw: Mapping[str, Any], key: str, path: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderResponseError(f"{path}.{key} must be an integer or null")
    return value


def _is_day(raw: Mapping[str, Any], key: str, path: str) -> bool:
    value = raw.get(key)
    if value not in (0, 1) or isinstance(value, bool):
        raise ProviderResponseError(f"{path}.{key} must be 0 or 1")
    return value == 1


def _local_datetime(raw: Any, timezone_info: ZoneInfo, path: str) -> datetime:
    if not isinstance(raw, str):
        raise ProviderResponseError(f"{path} must be an ISO 8601 local timestamp")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProviderResponseError(f"{path} must be an ISO 8601 local timestamp") from exc
    if value.tzinfo is not None:
        return value.astimezone(timezone_info)
    return value.replace(tzinfo=timezone_info)


def _optional_local_datetime(raw: Any, timezone_info: ZoneInfo, path: str) -> datetime | None:
    return None if raw is None else _local_datetime(raw, timezone_info, path)


def _local_datetime_array(
    values: Sequence[Any], timezone_info: ZoneInfo, path: str
) -> tuple[datetime, ...]:
    """Resolve repeated fall-back wall times into distinct absolute hours."""
    result: list[datetime] = []
    previous_epoch: float | None = None
    for index, raw in enumerate(values):
        current = _local_datetime(raw, timezone_info, f"{path}[{index}]")
        if previous_epoch is not None and current.timestamp() <= previous_epoch:
            folded = current.replace(fold=1)
            if folded.timestamp() > previous_epoch:
                current = folded
        result.append(current)
        previous_epoch = current.timestamp()
    return tuple(result)


def _parse_current(raw: Any, timezone_info: ZoneInfo, units: str) -> CurrentConditions:
    value = _object(raw, "response.current")
    code = _integer(value, "weather_code", "response.current")
    return CurrentConditions(
        observed_at=_local_datetime(value.get("time"), timezone_info, "response.current.time"),
        temperature=_number(value, "temperature_2m", "response.current"),
        apparent_temperature=_number(value, "apparent_temperature", "response.current"),
        condition_code=code,
        condition_text=condition_text(code),
        is_day=_is_day(value, "is_day", "response.current"),
        humidity=_number(value, "relative_humidity_2m", "response.current"),
        wind_speed=_number(value, "wind_speed_10m", "response.current"),
        wind_direction=_number(value, "wind_direction_10m", "response.current"),
        wind_gust=_number(value, "wind_gusts_10m", "response.current"),
        precipitation=_number(value, "precipitation", "response.current"),
        visibility=_visibility(_number(value, "visibility", "response.current"), units),
        pressure=_number(value, "pressure_msl", "response.current"),
    )


def _parse_hourly(raw: Any, timezone_info: ZoneInfo) -> tuple[HourlyForecast, ...]:
    value = _object(raw, "response.hourly")
    keys = ("time", *HOURLY_FIELDS)
    arrays = {key: _array(value, key, "response.hourly") for key in keys}
    lengths = {len(items) for items in arrays.values()}
    if len(lengths) != 1:
        raise ProviderResponseError("response.hourly arrays must have equal lengths")
    times = _local_datetime_array(arrays["time"], timezone_info, "response.hourly.time")
    result = []
    for index in range(len(arrays["time"])):
        row = {key: arrays[key][index] for key in HOURLY_FIELDS}
        result.append(
            HourlyForecast(
                starts_at=times[index],
                temperature=_number(row, "temperature_2m", f"response.hourly[{index}]"),
                apparent_temperature=_number(
                    row, "apparent_temperature", f"response.hourly[{index}]"
                ),
                condition_code=_integer(row, "weather_code", f"response.hourly[{index}]"),
                precipitation_probability=_number(
                    row, "precipitation_probability", f"response.hourly[{index}]"
                ),
                precipitation_amount=_number(
                    row, "precipitation", f"response.hourly[{index}]"
                ),
                wind_speed=_number(row, "wind_speed_10m", f"response.hourly[{index}]"),
                wind_gust=_number(row, "wind_gusts_10m", f"response.hourly[{index}]"),
                is_day=_is_day(row, "is_day", f"response.hourly[{index}]"),
            )
        )
    return tuple(result)


def _parse_daily(raw: Any, timezone_info: ZoneInfo) -> tuple[DailyForecast, ...]:
    value = _object(raw, "response.daily")
    keys = ("time", *DAILY_FIELDS)
    arrays = {key: _array(value, key, "response.daily") for key in keys}
    lengths = {len(items) for items in arrays.values()}
    if len(lengths) != 1:
        raise ProviderResponseError("response.daily arrays must have equal lengths")
    result = []
    for index in range(len(arrays["time"])):
        row = {key: arrays[key][index] for key in DAILY_FIELDS}
        raw_date = arrays["time"][index]
        if not isinstance(raw_date, str):
            raise ProviderResponseError(f"response.daily.time[{index}] must be an ISO date")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ProviderResponseError(
                f"response.daily.time[{index}] must be an ISO date"
            ) from exc
        result.append(
            DailyForecast(
                date=day,
                high_temperature=_number(
                    row, "temperature_2m_max", f"response.daily[{index}]"
                ),
                low_temperature=_number(
                    row, "temperature_2m_min", f"response.daily[{index}]"
                ),
                condition_code=_integer(row, "weather_code", f"response.daily[{index}]"),
                precipitation_probability=_number(
                    row, "precipitation_probability_max", f"response.daily[{index}]"
                ),
                sunrise=_optional_local_datetime(
                    row.get("sunrise"), timezone_info, f"response.daily.sunrise[{index}]"
                ),
                sunset=_optional_local_datetime(
                    row.get("sunset"), timezone_info, f"response.daily.sunset[{index}]"
                ),
            )
        )
    return tuple(result)


def _visibility(meters: float | None, units: str) -> float | None:
    if meters is None:
        return None
    return meters / 1609.344 if units == "imperial" else meters / 1000


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
        raise ProviderRequestError(
            f"Open-Meteo forecast request failed: {reason or f'HTTP {exc.code}'}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderRequestError(f"Unable to reach Open-Meteo forecast service: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResponseError("Open-Meteo returned invalid forecast JSON") from exc
