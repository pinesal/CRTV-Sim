from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from retroweather.config import AppPaths
from retroweather.models import NormalizedWeatherSnapshot, SnapshotValidationError
from retroweather.rendering import RenderResult, load_active_channel, render_and_publish
from retroweather.weather import fetch_and_store_weather


JOB_SCHEMA_VERSION = 1
JobAction = str
RenderFunction = Callable[..., RenderResult]
FetchFunction = Callable[..., Any]


class JobBusyError(RuntimeError):
    """Raised when a second channel job is requested while one is active."""


@dataclass(frozen=True)
class ChannelJobStatus:
    state: str
    action: JobAction | None
    stage: str
    progress: int
    started_at: str | None
    finished_at: str | None
    generation_id: str | None
    validation_result: str | None
    error: str | None
    last_successful_publication: str | None
    last_successful_generation: str | None
    weather_changed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "state": self.state,
            "action": self.action,
            "stage": self.stage,
            "progress": self.progress,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "generation_id": self.generation_id,
            "validation_result": self.validation_result,
            "error": self.error,
            "last_successful_publication": self.last_successful_publication,
            "last_successful_generation": self.last_successful_generation,
            "weather_changed": self.weather_changed,
        }


class ChannelJobManager:
    def __init__(
        self,
        paths: AppPaths,
        weather_client: Any,
        *,
        render_function: RenderFunction = render_and_publish,
        fetch_function: FetchFunction = fetch_and_store_weather,
    ) -> None:
        self.paths = paths
        self.weather_client = weather_client
        self.render_function = render_function
        self.fetch_function = fetch_function
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = self._initial_status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status.to_dict()

    def start(self, action: JobAction) -> dict[str, Any]:
        if action not in {"fetch", "render", "fetch-render"}:
            raise ValueError(f"Unsupported channel job: {action}")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise JobBusyError("Another channel job is already in progress.")
            now = _now()
            self._status = ChannelJobStatus(
                state="running",
                action=action,
                stage="Queued",
                progress=0,
                started_at=now,
                finished_at=None,
                generation_id=None,
                validation_result=None,
                error=None,
                last_successful_publication=self._status.last_successful_publication,
                last_successful_generation=self._status.last_successful_generation,
                weather_changed=None,
            )
            self._persist_locked()
            self._thread = threading.Thread(
                target=self._run,
                args=(action,),
                name=f"retroweather-{action}",
                daemon=True,
            )
            self._thread.start()
            return self._status.to_dict()

    def is_busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: float = 10.0) -> dict[str, Any]:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def _run(self, action: JobAction) -> None:
        try:
            weather_changed = None
            if action in {"fetch", "fetch-render"}:
                before = _display_fingerprint(self.paths)
                self._update("Fetching current weather", 4)
                self.fetch_function(self.paths, self.weather_client)
                weather_changed = before != _display_fingerprint(self.paths)
                self._update(
                    "Weather updated; displayed facts changed"
                    if weather_changed
                    else "Weather updated; displayed facts unchanged",
                    100 if action == "fetch" else 18,
                )

            if action == "fetch":
                with self._lock:
                    self._status = replace(
                        self._status,
                        state="success",
                        stage=(
                            "Weather updated; displayed facts changed"
                            if weather_changed
                            else "Weather updated; displayed facts unchanged"
                        ),
                        progress=100,
                        finished_at=_now(),
                        error=None,
                        weather_changed=weather_changed,
                    )
                    self._persist_locked()
                return

            def on_progress(stage: str, percent: int) -> None:
                adjusted = percent if action == "render" else 18 + round(percent * 0.82)
                self._update(stage, min(adjusted, 99))

            result = self.render_function(self.paths, progress=on_progress)
            finished = _now()
            with self._lock:
                self._status = replace(
                    self._status,
                    state="success",
                    stage="Validated and published",
                    progress=100,
                    finished_at=finished,
                    generation_id=result.generation_id,
                    validation_result=(
                        "Passed manifest, paths, media, duration, timeline, and seeking checks."
                    ),
                    error=None,
                    last_successful_publication=finished,
                    last_successful_generation=result.generation_id,
                    weather_changed=weather_changed,
                )
                self._persist_locked()
        except Exception as exc:  # The job boundary must report failures to the dashboard.
            with self._lock:
                self._status = replace(
                    self._status,
                    state="failed",
                    stage="Failed",
                    finished_at=_now(),
                    error=str(exc),
                )
                self._persist_locked()

    def _update(self, stage: str, progress: int) -> None:
        with self._lock:
            self._status = replace(
                self._status,
                stage=stage,
                progress=max(self._status.progress, max(0, min(99, int(progress)))),
            )
            self._persist_locked()

    def _initial_status(self) -> ChannelJobStatus:
        last_time = None
        last_generation = None
        try:
            manifest, _ = load_active_channel(self.paths)
            last_time = manifest.get("generated_at")
            last_generation = manifest.get("generation_id")
        except Exception:
            pass
        try:
            raw = json.loads(self.paths.render_status.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("schema_version") == JOB_SCHEMA_VERSION:
                persisted_time = raw.get("last_successful_publication")
                if _is_later(persisted_time, last_time):
                    last_time = persisted_time
                    last_generation = raw.get("last_successful_generation")
                state = raw.get("state")
                if state in {"idle", "success", "failed", "running"}:
                    interrupted = state == "running"
                    return ChannelJobStatus(
                        state="failed" if interrupted else state,
                        action=(
                            raw.get("action")
                            if raw.get("action") in {"fetch", "render", "fetch-render"}
                            else None
                        ),
                        stage="Interrupted by application restart" if interrupted else str(raw.get("stage") or "Ready"),
                        progress=max(0, min(100, int(raw.get("progress", 0)))),
                        started_at=raw.get("started_at"),
                        finished_at=_now() if interrupted else raw.get("finished_at"),
                        generation_id=raw.get("generation_id"),
                        validation_result=raw.get("validation_result"),
                        error=(
                            "The previous channel job ended when RetroWeather stopped."
                            if interrupted
                            else raw.get("error")
                        ),
                        last_successful_publication=last_time,
                        last_successful_generation=last_generation,
                        weather_changed=raw.get("weather_changed") if isinstance(raw.get("weather_changed"), bool) else None,
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return ChannelJobStatus(
            state="idle",
            action=None,
            stage="Ready",
            progress=0,
            started_at=None,
            finished_at=None,
            generation_id=None,
            validation_result=None,
            error=None,
            last_successful_publication=last_time,
            last_successful_generation=last_generation,
            weather_changed=None,
        )

    def _persist_locked(self) -> None:
        _persist_status(self.paths, self._status)


def record_render_success(paths: AppPaths, result: RenderResult) -> dict[str, Any]:
    """Record a successful direct CLI render for dashboard continuity."""

    finished = _now()
    status = ChannelJobStatus(
        state="success",
        action="render",
        stage="Validated and published",
        progress=100,
        started_at=finished,
        finished_at=finished,
        generation_id=result.generation_id,
        validation_result=(
            "Passed manifest, paths, media, duration, timeline, and seeking checks."
        ),
        error=None,
        last_successful_publication=finished,
        last_successful_generation=result.generation_id,
        weather_changed=None,
    )
    _persist_status(paths, status)
    return status.to_dict()


def _persist_status(paths: AppPaths, status: ChannelJobStatus) -> None:
    paths.render_status.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.render_status.with_name(
        f".{paths.render_status.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(status.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, paths.render_status)
    finally:
        temporary.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_later(candidate: Any, current: Any) -> bool:
    if not isinstance(candidate, str):
        return False
    if not isinstance(current, str):
        return True
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")) > datetime.fromisoformat(
            current.replace("Z", "+00:00")
        )
    except ValueError:
        return False


def _display_fingerprint(paths: AppPaths) -> str | None:
    if not paths.latest_snapshot.is_file():
        return None
    try:
        payload = NormalizedWeatherSnapshot.load(paths.latest_snapshot).to_dict()
    except (OSError, SnapshotValidationError):
        return None
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("fetched_at", None)
        metadata.pop("expires_at", None)
    current = payload.get("current")
    if isinstance(current, dict):
        current.pop("observed_at", None)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
