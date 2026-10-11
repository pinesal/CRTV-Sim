from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from retroweather.config import AppPaths, ConfigError, load_settings
from retroweather.locations import load_selected_location
from retroweather.models import NormalizedWeatherSnapshot, SnapshotValidationError
from retroweather.providers.open_meteo import ProviderRequestError, ProviderResponseError
from retroweather.providers.open_meteo_weather import OpenMeteoForecastClient


FETCH_STATE_SCHEMA_VERSION = 1
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class FetchState:
    state: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    error: str | None

    @classmethod
    def never(cls) -> "FetchState":
        return cls("never", None, None, None)

    @classmethod
    def from_dict(cls, raw: Any) -> "FetchState":
        if not isinstance(raw, Mapping):
            raise ConfigError("Fetch state must be a JSON object.")
        expected = {
            "schema_version",
            "state",
            "last_attempt_at",
            "last_success_at",
            "error",
        }
        unknown = raw.keys() - expected
        missing = expected - raw.keys()
        if missing or unknown:
            detail = missing if missing else unknown
            raise ConfigError(f"Fetch state has invalid fields: {', '.join(sorted(detail))}")
        if raw["schema_version"] != FETCH_STATE_SCHEMA_VERSION:
            raise ConfigError("Unsupported fetch-state schema version.")
        state = raw["state"]
        if state not in {"never", "success", "failed"}:
            raise ConfigError("Fetch state must be never, success, or failed.")
        error = raw["error"]
        if error is not None and (not isinstance(error, str) or not error.strip()):
            raise ConfigError("Fetch-state error must be text or null.")
        return cls(
            state=state,
            last_attempt_at=_optional_timestamp(raw["last_attempt_at"], "last_attempt_at"),
            last_success_at=_optional_timestamp(raw["last_success_at"], "last_success_at"),
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FETCH_STATE_SCHEMA_VERSION,
            "state": self.state,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class WeatherRuntimeStatus:
    available: bool
    snapshot_valid: bool
    state: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    error: str | None
    snapshot_error: str | None
    fetched_at: datetime | None
    expires_at: datetime | None
    age_seconds: int | None
    refresh_due: bool
    stale: bool
    location_mismatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "snapshot_valid": self.snapshot_valid,
            "state": self.state,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "error": self.error,
            "snapshot_error": self.snapshot_error,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "age_seconds": self.age_seconds,
            "refresh_due": self.refresh_due,
            "stale": self.stale,
            "location_mismatch": self.location_mismatch,
        }


def fetch_and_store_weather(
    paths: AppPaths,
    client: OpenMeteoForecastClient | None = None,
    clock: Clock | None = None,
) -> NormalizedWeatherSnapshot:
    settings = load_settings(paths)
    selection = load_selected_location(paths)
    if selection is None:
        raise ConfigError("Select a weather location before fetching weather data.")
    units_section = settings.get("units")
    units = units_section.get("system") if isinstance(units_section, dict) else None
    if units not in {"imperial", "metric"}:
        raise ConfigError("units.system must be imperial or metric")
    refresh = settings.get("refresh")
    if not isinstance(refresh, dict):
        raise ConfigError("Settings are missing the refresh section.")
    try:
        timeout = float(refresh.get("provider_timeout_seconds", 20))
        expires_minutes = int(refresh.get("data_minutes", 15))
    except (TypeError, ValueError) as exc:
        raise ConfigError("Weather refresh settings must be numeric.") from exc

    now = _aware_now(clock)
    previous = _load_fetch_state_or_default(paths)
    active_client = client or OpenMeteoForecastClient(timeout=timeout, clock=lambda: now)
    try:
        result = active_client.fetch(selection.location, units, expires_minutes)
        _archive_raw_response(paths, result.raw_response)
        result.snapshot.save(paths.latest_snapshot)
        _save_fetch_state(
            paths,
            FetchState(
                state="success",
                last_attempt_at=now,
                last_success_at=result.snapshot.metadata.fetched_at,
                error=None,
            ),
        )
        return result.snapshot
    except (
        ProviderRequestError,
        ProviderResponseError,
        SnapshotValidationError,
        OSError,
        ValueError,
    ) as exc:
        raw = getattr(active_client, "last_raw_response", None)
        if raw is not None:
            try:
                _archive_raw_response(paths, raw)
            except OSError:
                pass
        last_success = previous.last_success_at or _existing_snapshot_time(paths)
        try:
            _save_fetch_state(
                paths,
                FetchState(
                    state="failed",
                    last_attempt_at=now,
                    last_success_at=last_success,
                    error=str(exc)[:1000],
                ),
            )
        except OSError:
            pass
        raise


def load_fetch_state(paths: AppPaths) -> FetchState:
    if not paths.fetch_status.is_file():
        return FetchState.never()
    try:
        raw = json.loads(paths.fetch_status.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to read fetch state: {exc}") from exc
    return FetchState.from_dict(raw)


def build_weather_status(
    paths: AppPaths,
    now: datetime | None = None,
) -> WeatherRuntimeStatus:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Weather status time must be timezone-aware.")
    settings = load_settings(paths)
    refresh = settings.get("refresh")
    try:
        stale_minutes = int(refresh.get("stale_after_minutes", 90)) if isinstance(refresh, dict) else 90
    except (TypeError, ValueError):
        stale_minutes = 90

    state_error: str | None = None
    try:
        fetch_state = load_fetch_state(paths)
    except ConfigError as exc:
        fetch_state = FetchState.never()
        state_error = str(exc)

    snapshot: NormalizedWeatherSnapshot | None = None
    snapshot_error: str | None = None
    if paths.latest_snapshot.is_file():
        try:
            snapshot = NormalizedWeatherSnapshot.load(paths.latest_snapshot)
        except (OSError, SnapshotValidationError) as exc:
            snapshot_error = str(exc)

    selected = load_selected_location(paths)
    if snapshot is None:
        return WeatherRuntimeStatus(
            available=False,
            snapshot_valid=False,
            state=fetch_state.state,
            last_attempt_at=fetch_state.last_attempt_at,
            last_success_at=fetch_state.last_success_at,
            error=fetch_state.error or state_error,
            snapshot_error=snapshot_error,
            fetched_at=None,
            expires_at=None,
            age_seconds=None,
            refresh_due=True,
            stale=False,
            location_mismatch=False,
        )

    age_seconds = max(0, int((moment - snapshot.metadata.fetched_at).total_seconds()))
    location_mismatch = selected is not None and selected.location != snapshot.location
    return WeatherRuntimeStatus(
        available=True,
        snapshot_valid=True,
        state=fetch_state.state,
        last_attempt_at=fetch_state.last_attempt_at,
        last_success_at=fetch_state.last_success_at or snapshot.metadata.fetched_at,
        error=fetch_state.error or state_error,
        snapshot_error=None,
        fetched_at=snapshot.metadata.fetched_at,
        expires_at=snapshot.metadata.expires_at,
        age_seconds=age_seconds,
        refresh_due=moment >= snapshot.metadata.expires_at,
        stale=age_seconds >= stale_minutes * 60,
        location_mismatch=location_mismatch,
    )


def _load_fetch_state_or_default(paths: AppPaths) -> FetchState:
    try:
        return load_fetch_state(paths)
    except ConfigError:
        return FetchState.never()


def _existing_snapshot_time(paths: AppPaths) -> datetime | None:
    if not paths.latest_snapshot.is_file():
        return None
    try:
        return NormalizedWeatherSnapshot.load(paths.latest_snapshot).metadata.fetched_at
    except (OSError, SnapshotValidationError):
        return None


def _aware_now(clock: Clock | None) -> datetime:
    value = clock() if clock else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Weather fetch clock must return a timezone-aware datetime.")
    return value


def _optional_timestamp(value: Any, path: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Fetch-state {path} must be an ISO timestamp or null.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"Fetch-state {path} must be an ISO timestamp or null.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ConfigError(f"Fetch-state {path} must include a UTC offset.")
    return result


def _archive_raw_response(paths: AppPaths, raw: Any) -> None:
    paths.raw_cache.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(paths.latest_raw_forecast, raw)


def _save_fetch_state(paths: AppPaths, state: FetchState) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(paths.fetch_status, state.to_dict())


def _atomic_write_json(path: Path, raw: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
