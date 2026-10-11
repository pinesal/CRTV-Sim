from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


JsonObject = dict[str, Any]
PublicationProbe = Callable[[], JsonObject]
ProcessFactory = Callable[..., subprocess.Popen]


class CompanionPortConflictError(OSError):
    """The configured port belongs to a different RetroWeather home."""


class WeatherCompanionManager:
    """Supervise the optional RetroWeather localhost companion process."""

    def __init__(
        self,
        config,
        *,
        publication_probe: PublicationProbe | None = None,
        process_factory: ProcessFactory | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        settings = config.data.get("synthetic_channels", {}).get("weather", {})
        self.config = config
        raw_home = Path(
            os.path.expandvars(
                os.path.expanduser(str(settings.get("companion_home", "./RetroWeather")))
            )
        )
        self.home = (
            raw_home if raw_home.is_absolute() else config.project_root / raw_home
        ).resolve()
        self.enabled = bool(settings.get("enabled", False))
        self.manage = bool(settings.get("manage_companion", True))
        self.host = str(settings.get("companion_host", "127.0.0.1")).strip()
        self.port = int(settings.get("companion_port", 8775))
        connect_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        self.base_url = f"http://{connect_host}:{self.port}"
        self.poll_seconds = max(
            1.0, float(settings.get("companion_health_poll_seconds", 5))
        )
        self.startup_timeout_seconds = max(
            2.0, float(settings.get("companion_startup_timeout_seconds", 20))
        )
        self.restart_initial_seconds = max(
            1.0, float(settings.get("companion_restart_initial_seconds", 2))
        )
        self.restart_max_seconds = max(
            self.restart_initial_seconds,
            float(settings.get("companion_restart_max_seconds", 60)),
        )
        self.http_timeout = max(
            0.2, float(settings.get("companion_http_timeout_seconds", 2))
        )
        self.publication_probe = publication_probe
        self.process_factory = process_factory or subprocess.Popen
        self.monotonic = monotonic or time.monotonic
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.owned = False
        self._launched_at: float | None = None
        self._next_restart_at = 0.0
        self._restart_failures = 0
        self._restart_requested = False
        self._freshness_checked = False
        self._status: JsonObject = {
            "state": "disabled" if not self.enabled else "stopped",
            "healthy": False,
            "managed": self.manage,
            "owned": False,
            "pid": None,
            "home": str(self.home),
            "dashboard_url": f"{self.base_url}/",
            "last_health_check": None,
            "last_update_request": None,
            "next_restart_seconds": None,
            "restart_failures": 0,
            "job": None,
            "scheduler": None,
            "error": None,
            "setup_required": False,
        }

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.manage:
            from retrotv.weather_graphics import sync_weather_holidays
            try:
                sync_weather_holidays(self.config)
            except (OSError, ValueError) as exc:
                self._set_status(error=f"Unable to sync Weather holidays: {exc}")
            return
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self._set_status(state="starting", error=None)
            self.thread = threading.Thread(
                target=self._run,
                name="retrotv-weather-companion",
                daemon=True,
            )
            self.thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(min(timeout, 3.0))
        process = self.process
        if self.owned and process is not None and process.poll() is None:
            try:
                self._post_json("/api/shutdown")
            except OSError:
                pass
            try:
                process.wait(timeout=max(0.5, timeout - 3.0))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
        with self.lock:
            self.process = None
            self.owned = False
            self._set_status(
                state="stopped", healthy=False, owned=False, pid=None
            )

    def request_restart(self) -> bool:
        if not self.enabled or not self.manage:
            return False
        with self.lock:
            # Ownership is deliberate: never stop an independently launched
            # RetroWeather merely because its dashboard is reachable.
            if self._status.get("healthy") and not self.owned:
                self._status["error"] = (
                    "RetroWeather is running independently and was left running."
                )
                return False
            self._restart_requested = True
            self._next_restart_at = 0.0
            return True

    def status(self) -> JsonObject:
        with self.lock:
            remaining = max(0.0, self._next_restart_at - self.monotonic())
            return {
                **self._status,
                "owned": self.owned,
                "pid": self.process.pid if self.process is not None else None,
                "next_restart_seconds": (
                    round(remaining, 1) if remaining > 0 else None
                ),
                "restart_failures": self._restart_failures,
            }

    def poll_once(self) -> None:
        """Perform one health/supervision cycle; public for deterministic tests."""
        if not self.enabled or not self.manage or self.stop_event.is_set():
            return
        from retrotv.weather_graphics import sync_weather_holidays
        if sync_weather_holidays(self.config):
            self._freshness_checked = False
        now = self.monotonic()
        if self._restart_requested:
            self._restart_requested = False
            if self.owned:
                self._stop_owned_process()
            self._freshness_checked = False
            self._next_restart_at = 0.0

        try:
            remote = self._get_json("/api/status")
            if remote.get("schema_version") != 1 or "version" not in remote:
                raise OSError("Port does not appear to be a RetroWeather server")
            remote_home = remote.get("home")
            if remote_home and os.path.normcase(str(Path(str(remote_home)).resolve())) != os.path.normcase(
                str(self.home)
            ):
                raise CompanionPortConflictError(
                    f"Port {self.port} is used by RetroWeather home {remote_home}; "
                    f"expected {self.home}"
                )
        except CompanionPortConflictError as exc:
            self._set_status(
                state="unavailable",
                healthy=False,
                owned=False,
                pid=None,
                last_health_check=_now(),
                error=str(exc),
            )
            return
        except OSError as exc:
            self._handle_unhealthy(now, exc)
            return

        if self.process is not None and self.process.poll() is not None:
            # A healthy service now answering on the configured port is not
            # the exited process we launched; treat it as independently owned.
            self.process = None
            self.owned = False
        self._restart_failures = 0
        self._next_restart_at = 0.0
        try:
            job = self._get_json("/api/channel/job")
        except OSError:
            job = None
        scheduler = remote.get("scheduler")
        if "selected_location" in remote and remote["selected_location"] is None:
            self._freshness_checked = False
            self._set_status(
                state="setup_required", healthy=True, owned=self.owned,
                last_health_check=_now(), scheduler=scheduler, job=job,
                setup_required=True, error=None,
            )
            # Fetch/render cannot succeed until the user has selected a location.
            return
        updating = bool(
            (isinstance(job, dict) and job.get("state") == "running")
            or (isinstance(scheduler, dict) and scheduler.get("state") == "job_running")
        )
        stale = self._is_stale(remote)
        state = "updating" if updating else "stale" if stale else "running"
        self._set_status(
            state=state,
            healthy=True,
            owned=self.owned,
            pid=self.process.pid if self.process is not None else None,
            last_health_check=_now(),
            scheduler=scheduler,
            job=job,
            setup_required=False,
            error=None,
        )
        if not self._freshness_checked:
            self._freshness_checked = True
            if stale:
                try:
                    self._post_json("/api/channel/fetch-render")
                    self._set_status(
                        state="updating", last_update_request=_now(), error=None
                    )
                except HTTPError as exc:
                    if exc.code == 409:
                        self._set_status(
                            state="updating", last_update_request=_now(), error=None
                        )
                    else:
                        self._set_status(error=f"Update request failed: HTTP {exc.code}")
                except OSError as exc:
                    self._set_status(error=f"Update request failed: {exc}")

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # Supervision must never take CRTV Sim down.
                self._set_status(state="unavailable", healthy=False, error=str(exc))
            self.stop_event.wait(self.poll_seconds)

    def _handle_unhealthy(self, now: float, exc: OSError) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            if self._launched_at is not None and now - self._launched_at < self.startup_timeout_seconds:
                self._set_status(
                    state="starting",
                    healthy=False,
                    owned=True,
                    pid=process.pid,
                    last_health_check=_now(),
                    error=None,
                )
                return
            self._stop_owned_process()
            self._record_failure(now, "RetroWeather did not become healthy in time")
            return
        if process is not None:
            self.process = None
            self.owned = False
            self._record_failure(
                now, f"RetroWeather exited unexpectedly with code {process.returncode}"
            )
            return
        if now < self._next_restart_at:
            self._set_status(
                state="unavailable",
                healthy=False,
                last_health_check=_now(),
                error=str(exc),
            )
            return
        try:
            self._launch()
        except OSError as launch_error:
            self._record_failure(now, str(launch_error))

    def _launch(self) -> None:
        settings_file = self.home / "config" / "settings.json"
        if not settings_file.is_file():
            self._run_initializer()
        command = self._retroweather_command(
            "--home",
            str(self.home),
            "serve",
            "--host",
            self.host,
            "--port",
            str(self.port),
        )
        options: JsonObject = {
            "cwd": str(self.home),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = self.process_factory(command, **options)
        self.process = process
        self.owned = True
        self._launched_at = self.monotonic()
        self._set_status(
            state="starting",
            healthy=False,
            owned=True,
            pid=process.pid,
            last_health_check=_now(),
            error=None,
        )

    def _run_initializer(self) -> None:
        result = subprocess.run(
            self._retroweather_command(
                "--home",
                str(self.home),
                "init",
            ),
            cwd=str(self.home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise OSError(
                (result.stderr or "RetroWeather initialization failed").strip()
            )

    @staticmethod
    def _retroweather_command(*arguments: str) -> list[str]:
        """Build a companion command for source and frozen portable installs."""
        if getattr(sys, "frozen", False):
            return [sys.executable, "--retroweather", *arguments]
        return [sys.executable, "-m", "retroweather", *arguments]

    def _stop_owned_process(self) -> None:
        process = self.process
        if not self.owned or process is None:
            return
        if process.poll() is None:
            try:
                self._post_json("/api/shutdown")
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.process = None
        self.owned = False
        self._launched_at = None

    def _record_failure(self, now: float, error: str) -> None:
        self._restart_failures += 1
        delay = min(
            self.restart_max_seconds,
            self.restart_initial_seconds * (2 ** (self._restart_failures - 1)),
        )
        self._next_restart_at = now + delay
        self._set_status(
            state="unavailable",
            healthy=False,
            owned=False,
            pid=None,
            last_health_check=_now(),
            error=error,
        )

    def _is_stale(self, remote: JsonObject) -> bool:
        weather = remote.get("weather_status")
        weather_bad = (
            not remote.get("weather_snapshot")
            or not isinstance(weather, dict)
            or not weather.get("snapshot_valid")
            or bool(weather.get("stale"))
        )
        publication_bad = not remote.get("published_channel")
        if self.publication_probe is not None:
            try:
                publication = self.publication_probe()
                publication_bad = publication_bad or not publication.get("ready") or bool(
                    publication.get("stale")
                )
            except Exception:
                publication_bad = True
        return weather_bad or publication_bad

    def _get_json(self, path: str) -> JsonObject:
        request = Request(f"{self.base_url}{path}", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OSError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise OSError("RetroWeather returned a non-object response")
        return payload

    def _post_json(self, path: str) -> JsonObject:
        request = Request(
            f"{self.base_url}{path}",
            data=b"{}",
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OSError(str(exc)) from exc
        return payload if isinstance(payload, dict) else {}

    def _set_status(self, **changes: Any) -> None:
        with self.lock:
            self._status.update(changes)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
