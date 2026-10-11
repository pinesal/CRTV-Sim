from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from retroweather.config import AppPaths, load_settings
from retroweather.locations import load_selected_location
from retroweather.models import NormalizedWeatherSnapshot, SnapshotValidationError
from retroweather.rendering import RenderError, load_active_channel
from retroweather.web.jobs import ChannelJobManager, JobBusyError


SCHEDULER_SCHEMA_VERSION = 1
Clock = Callable[[], datetime]


class RefreshScheduler:
    """Coordinate weather fetches and renders outside the request path."""

    def __init__(
        self,
        paths: AppPaths,
        jobs: ChannelJobManager,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.paths = paths
        self.jobs = jobs
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_action: str | None = None
        self._pending_started_at: str | None = None
        self._force_render = False
        self._failure_count = 0
        self._next_retry_at: datetime | None = None
        self._last_cleanup_at: datetime | None = None
        self._status = self._initial_status()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="retroweather-refresh-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            self._status["running"] = False
            if self._status["state"] == "sleeping":
                self._status["state"] = "stopped"
            self._persist_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        moment = _aware(now or self.clock())
        settings = load_settings(self.paths)
        refresh = settings.get("refresh", {})
        if not isinstance(refresh, Mapping):
            refresh = {}
        enabled = bool(refresh.get("automatic", True))
        data_interval = timedelta(
            minutes=_positive_float(refresh.get("data_minutes", 15), 15)
        )
        render_interval = timedelta(
            minutes=_positive_float(refresh.get("render_minutes", 30), 30)
        )
        retry_initial = _positive_float(
            refresh.get("retry_initial_seconds", 60), 60
        )
        retry_max = max(
            retry_initial,
            _positive_float(refresh.get("retry_max_seconds", 900), 900),
        )

        self._observe_pending(moment, retry_initial, retry_max)
        last_fetch = _snapshot_time(self.paths)
        last_render = _publication_time(self.paths)
        next_fetch = last_fetch + data_interval if last_fetch else moment
        next_render = last_render + render_interval if last_render else moment

        if not enabled:
            return self._set_status(
                moment,
                state="disabled",
                enabled=False,
                next_fetch_at=next_fetch,
                next_render_at=next_render,
            )
        if load_selected_location(self.paths) is None:
            return self._set_status(
                moment,
                state="waiting_for_location",
                enabled=True,
                next_fetch_at=None,
                next_render_at=None,
            )
        if self._pending_action is not None or self.jobs.is_busy():
            return self._set_status(
                moment,
                state="job_running",
                enabled=True,
                next_fetch_at=next_fetch,
                next_render_at=next_render,
            )
        if self._next_retry_at is not None and moment < self._next_retry_at:
            return self._set_status(
                moment,
                state="backing_off",
                enabled=True,
                next_fetch_at=next_fetch,
                next_render_at=next_render,
            )

        if self._last_cleanup_at is None or moment - self._last_cleanup_at >= timedelta(hours=1):
            cleanup_storage(self.paths, settings, now=moment)
            self._last_cleanup_at = moment

        fetch_due = moment >= next_fetch
        render_due = self._force_render or moment >= next_render
        action = (
            "fetch-render"
            if fetch_due and render_due
            else "fetch"
            if fetch_due
            else "render"
            if render_due
            else None
        )
        if action is not None:
            try:
                started = self.jobs.start(action)
            except JobBusyError:
                return self._set_status(
                    moment,
                    state="job_running",
                    enabled=True,
                    next_fetch_at=next_fetch,
                    next_render_at=next_render,
                )
            self._pending_action = action
            self._pending_started_at = started.get("started_at")
            if action in {"render", "fetch-render"}:
                self._force_render = False
            return self._set_status(
                moment,
                state="job_running",
                enabled=True,
                last_action=action,
                next_fetch_at=next_fetch,
                next_render_at=next_render,
            )
        return self._set_status(
            moment,
            state="sleeping",
            enabled=True,
            next_fetch_at=next_fetch,
            next_render_at=next_render,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # Scheduler failures must not stop the dashboard.
                self._set_status(
                    _aware(self.clock()),
                    state="scheduler_error",
                    enabled=True,
                    error=str(exc)[:1000],
                )
            try:
                settings = load_settings(self.paths)
                refresh = settings.get("refresh", {})
                poll = _positive_float(
                    refresh.get("scheduler_poll_seconds", 5)
                    if isinstance(refresh, Mapping)
                    else 5,
                    5,
                )
            except Exception:
                poll = 5
            self._stop.wait(min(60.0, max(0.25, poll)))

    def _observe_pending(
        self,
        moment: datetime,
        retry_initial: float,
        retry_max: float,
    ) -> None:
        if self._pending_action is None or self.jobs.is_busy():
            return
        result = self.jobs.status()
        if self._pending_started_at and result.get("started_at") != self._pending_started_at:
            self._pending_action = None
            self._pending_started_at = None
            return
        action = self._pending_action
        self._pending_action = None
        self._pending_started_at = None
        if result.get("state") == "success":
            self._failure_count = 0
            self._next_retry_at = None
            if action == "fetch" and result.get("weather_changed") is True:
                self._force_render = True
            if action in {"render", "fetch-render"}:
                try:
                    cleanup_storage(self.paths, load_settings(self.paths), now=moment)
                    self._last_cleanup_at = moment
                except OSError:
                    pass
            return
        self._failure_count += 1
        delay = min(retry_max, retry_initial * (2 ** (self._failure_count - 1)))
        self._next_retry_at = moment + timedelta(seconds=delay)

    def _set_status(
        self,
        moment: datetime,
        *,
        state: str,
        enabled: bool,
        next_fetch_at: datetime | None = None,
        next_render_at: datetime | None = None,
        last_action: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._status.update(
                {
                    "schema_version": SCHEDULER_SCHEMA_VERSION,
                    "enabled": enabled,
                    "running": self._thread is not None and self._thread.is_alive(),
                    "state": state,
                    "last_check_at": moment.isoformat(),
                    "next_fetch_at": _iso(next_fetch_at),
                    "next_render_at": _iso(next_render_at),
                    "next_retry_at": _iso(self._next_retry_at),
                    "consecutive_failures": self._failure_count,
                    "pending_action": self._pending_action,
                    "last_action": last_action or self._status.get("last_action"),
                    "material_change_pending": self._force_render,
                    "error": error,
                }
            )
            self._persist_locked()
            return dict(self._status)

    def _initial_status(self) -> dict[str, Any]:
        result = {
            "schema_version": SCHEDULER_SCHEMA_VERSION,
            "enabled": True,
            "running": False,
            "state": "stopped",
            "last_check_at": None,
            "next_fetch_at": None,
            "next_render_at": None,
            "next_retry_at": None,
            "consecutive_failures": 0,
            "pending_action": None,
            "last_action": None,
            "material_change_pending": False,
            "error": None,
        }
        try:
            raw = json.loads(self.paths.scheduler_status.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("schema_version") == SCHEDULER_SCHEMA_VERSION:
                for key in result:
                    if key in raw:
                        result[key] = raw[key]
        except (OSError, json.JSONDecodeError):
            pass
        result["running"] = False
        result["pending_action"] = None
        if result["state"] == "job_running":
            result["state"] = "stopped"
        return result

    def _persist_locked(self) -> None:
        self.paths.scheduler_status.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.paths.scheduler_status.with_name(
            f".{self.paths.scheduler_status.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self._status, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.paths.scheduler_status)
        finally:
            temporary.unlink(missing_ok=True)


def cleanup_storage(
    paths: AppPaths,
    settings: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Prune only bounded publication/cache artifacts owned by RetroWeather."""

    moment = _aware(now or datetime.now(timezone.utc))
    paths.versions.mkdir(parents=True, exist_ok=True)
    count = max(1, int(settings.get("retention", {}).get("valid_versions", 3)))
    active_generation = None
    try:
        manifest, _ = load_active_channel(paths)
        active_generation = manifest.get("generation_id")
    except RenderError:
        pass
    versions = sorted(
        (
            item
            for item in paths.versions.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ),
        key=lambda item: item.name,
        reverse=True,
    )
    retained = {item.name for item in versions[:count]}
    if isinstance(active_generation, str):
        retained.add(active_generation)
    removed_versions = 0
    for version in versions:
        if version.name not in retained:
            _remove_owned_tree(paths.versions, version)
            removed_versions += 1

    removed_temporary = 0
    cutoff = moment.timestamp() - 6 * 60 * 60
    for root in (paths.versions, paths.cache, paths.state):
        if not root.is_dir():
            continue
        for item in root.iterdir():
            if not item.name.startswith(".") or item.stat().st_mtime >= cutoff:
                continue
            if item.is_dir():
                _remove_owned_tree(root, item)
            elif item.is_file():
                item.unlink()
            removed_temporary += 1
    return {"versions": removed_versions, "temporary": removed_temporary}


def _remove_owned_tree(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise OSError(f"Refusing to remove path outside RetroWeather storage: {target}")
    shutil.rmtree(resolved_target)


def _snapshot_time(paths: AppPaths) -> datetime | None:
    try:
        return NormalizedWeatherSnapshot.load(paths.latest_snapshot).metadata.fetched_at.astimezone(
            timezone.utc
        )
    except (OSError, SnapshotValidationError):
        return None


def _publication_time(paths: AppPaths) -> datetime | None:
    try:
        manifest, _ = load_active_channel(paths)
        return _timestamp(manifest.get("generated_at"))
    except (RenderError, ValueError, TypeError):
        return None


def _timestamp(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("timestamp must be text")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return _aware(value)


def _positive_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scheduler time must include a UTC offset.")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
