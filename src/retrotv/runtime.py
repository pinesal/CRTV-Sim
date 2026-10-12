from __future__ import annotations

import threading
import os
import sqlite3
import shutil
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from retrotv.processing import enabled
from retrotv.db import connect_database
from retrotv.duplicates import DuplicateService
from retrotv.announcements import UpNextRenderer
from retrotv.audio_normalization import AudioNormalizationService
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.cleanup import GeneratedAssetCleanup
from retrotv.commercial_tails import CommercialTailService
from retrotv.database_backup import DatabaseBackupService
from retrotv.database_maintenance import (
    DatabaseCompactionService,
    DatabaseHousekeepingService,
)
from retrotv.guide import GuideRenderer, GuideService
from retrotv.graphics import GraphicsService
from retrotv.internet_media import InternetMediaService, PreparationService
from retrotv.maintenance_window import (
    MaintenanceWindowSchedule,
    MaintenanceWindowStateStore,
)
from retrotv.normalization import NormalizationService
from retrotv.playback import (
    PlaybackController,
    PlaybackError,
    ScheduleResolver,
    create_player,
)
from retrotv.promotion import PromotionService
from retrotv.process_control import ProcessCancelled
from retrotv.scanner import LibraryScanner
from retrotv.scheduling import ScheduleBuilder
from retrotv.shutdown_control import ShutdownControl
from retrotv.synthetic import (
    weather_channel_available,
    weather_channel_definition,
    weather_channel_reader,
)
from retrotv.weather_companion import WeatherCompanionManager


class ApplicationRuntime:
    """Owns television playback and recurring Windows application maintenance."""

    def __init__(self, config):
        self.config = config
        self.settings = config.data.get("application_layer", {})
        self.lock = threading.RLock()
        self.shutdown_lock = threading.Lock()
        self.shutdown_complete_event = threading.Event()
        self.stop_event = threading.Event()
        self.started_at: datetime | None = None
        self.controller: PlaybackController | None = None
        self.playback_thread: threading.Thread | None = None
        self.automation_thread: threading.Thread | None = None
        self.internet_media_thread: threading.Thread | None = None
        self.shutdown_request_thread: threading.Thread | None = None
        self.maintenance_thread: threading.Thread | None = None
        self.readiness_thread: threading.Thread | None = None
        self.normalization_batch_thread: threading.Thread | None = None
        self.audio_analysis_thread: threading.Thread | None = None
        self.audio_normalization_thread: threading.Thread | None = None
        self.generated_cleanup_thread: threading.Thread | None = None
        self.generated_cleanup_lock = threading.Lock()
        self.database_backup_thread: threading.Thread | None = None
        self.database_housekeeping_thread: threading.Thread | None = None
        self.database_compaction_thread: threading.Thread | None = None
        self.cache_cleanup_thread: threading.Thread | None = None
        self.normalization_review_thread: threading.Thread | None = None
        self.library_recovery_thread: threading.Thread | None = None
        self.normalization_review = {"state": "idle", "error": None}
        self.playback_recovery = {"state": "idle", "missing": None, "error": None}
        self._missing_paths_seen: set[tuple[int, str]] = set()
        self._missing_scan_pending = False
        self._missing_scan_not_before = 0.0
        self._review_resume = False
        self.database_backup_lock = threading.Lock()
        self.guide_thread: threading.Thread | None = None
        self.guide_lock = threading.Lock()
        self.guide_reload_token = 0
        self.exclusive_operation_lock = threading.Lock()
        self.maintenance_cancel_event = threading.Event()
        self.internet_pause_event = threading.Event()
        self.internet_writer_lock = threading.Lock()
        self.announcement_thread: threading.Thread | None = None
        self.tuned_since: datetime | None = None
        self.last_qualifying_user_activity: datetime | None = None
        self.announcement_state: dict[str, Any] = {
            "next_opportunity": None,
            "eligibility_reason": "Television is not playing",
            "generation_state": "idle",
            "job_id": None,
            "error": None,
            "last_poll_at": None,
        }
        self.tv_state = "stopped"
        self.tv_error: str | None = None
        self.requested_channel: str | None = str(
            self.settings.get("startup_channel", "fox")
        )
        self.previous_channel: str | None = None
        self.synthetic_readers = {
            "weather": weather_channel_reader(config)
        }
        self.weather_companion = WeatherCompanionManager(
            config,
            publication_probe=lambda: self.synthetic_readers["weather"]
            .refresh()
            .as_dict(),
        )
        self.digit_buffer = ""
        self._instance_lock = None
        web = config.data.get("web", {})
        host = str(web.get("host", "127.0.0.1"))
        url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        self.control_dashboard_url = f"http://{url_host}:{int(web.get('port', 8765))}"
        self.shutdown_control = ShutdownControl(config.storage_root("state"))
        self.maintenance_window_schedule = MaintenanceWindowSchedule(config)
        self.maintenance_window_store = MaintenanceWindowStateStore(
            config.storage_root("state") / "maintenance-window.json"
        )
        self.maintenance_window_state = self.maintenance_window_store.read()
        self.shutdown_state: dict[str, Any] = {
            "state": "running",
            "stage": None,
            "active_jobs": [],
            "started_at": None,
            "completed_at": None,
            "safe_to_remove": False,
            "database_backup": None,
            "error": None,
        }
        self.maintenance: dict[str, Any] = {
            "state": "idle",
            "stage": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "scan": None,
            "duplicates": None,
            "normalization": None,
            "commercial_tails": None,
            "breakpoints": None,
            "audio": None,
            "schedules_created": 0,
            "guide_rendered": False,
            "generated_cleanup": None,
            "database_backup": None,
        }
        self.readiness: dict[str, Any] = {
            "state": "idle",
            "stage": None,
            "failed_stage": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "idle_required": True,
        }
        self.internet_media: dict[str, Any] = {
            "state": "idle",
            "current": None,
            "completed_at": None,
            "error": None,
        }
        self.generated_cleanup: dict[str, Any] = {
            "state": "idle",
            "started_at": None,
            "completed_at": None,
            "files_removed": 0,
            "bytes_removed": 0,
            "files_examined": 0,
            "skipped": False,
            "reason": None,
            "errors": [],
        }
        backup_inventory = DatabaseBackupService(config).inventory()
        self.database_backup: dict[str, Any] = {
            "state": "idle",
            "started_at": None,
            "completed_at": None,
            "created": False,
            "skipped": False,
            "reason": None,
            "error": None,
            "removed_count": 0,
            **backup_inventory,
        }
        self.database_housekeeping: dict[str, Any] = {
            "state": "idle", "started_at": None, "completed_at": None,
            "trigger": None, "rows_removed": {}, "reclaimable_bytes": 0,
            "database_size_before": 0, "database_size_after": 0,
            "backup": None, "error": None,
            "reason": None,
        }
        self.database_compaction: dict[str, Any] = {
            "state": "idle", "started_at": None, "completed_at": None,
            "database_size_before": 0, "database_size_after": 0,
            "bytes_reclaimed": 0, "backup": None, "error": None,
        }
        self.cache_cleanup: dict[str, Any] = {
            "state": "idle", "started_at": None, "completed_at": None,
            "files_removed": 0, "bytes_removed": 0, "errors": [],
        }
        self.normalization_batch: dict[str, Any] = {
            "state": "idle",
            "policy": str(
                self.config.data.get("normalization", {}).get(
                    "output_policy", "proxy"
                )
            ),
            "total": 0,
            "processed": 0,
            "converted": 0,
            "reused": 0,
            "skipped": 0,
            "not_needed": 0,
            "failed": 0,
            "current_asset_id": None,
            "current_path": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "failures": [],
        }
        self.audio_analysis_batch: dict[str, Any] = self._new_audio_batch_state()
        self.audio_normalization_batch: dict[str, Any] = self._new_audio_batch_state()
        self.pending_actions: dict[str, Any] = {
            "library_review": 0,
            "aspect_review": 0,
            "conversion_ready": 0,
            "total": 0,
            "show_indicator": False,
        }

    @staticmethod
    def _new_audio_batch_state() -> dict[str, Any]:
        return {
            "state": "idle",
            "total": 0,
            "processed": 0,
            "analyzed": 0,
            "review": 0,
            "not_applicable": 0,
            "correction_needed": 0,
            "normalized": 0,
            "failed": 0,
            "current_asset_id": None,
            "current_path": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "failures": [],
        }

    @property
    def timezone(self):
        with connect_database(self.config.database_path) as connection:
            return ScheduleBuilder(connection, self.config).timezone

    def start(self) -> None:
        with self.lock:
            if self.started_at is not None:
                return
            self._acquire_instance_lock()
            try:
                from retrotv.normalization_review import NormalizationReviewService
                with connect_database(self.config.database_path) as connection:
                    NormalizationReviewService(connection, self.config).recover_pending()
            except Exception:
                self._release_instance_lock()
                raise
            self.started_at = datetime.now().astimezone()
            self.stop_event.clear()
            self.shutdown_complete_event.clear()
            self.shutdown_state.update(
                {
                    "state": "running",
                    "stage": None,
                    "active_jobs": [],
                    "started_at": None,
                    "completed_at": None,
                    "safe_to_remove": False,
                    "database_backup": None,
                    "error": None,
                }
            )
            self.shutdown_control.begin_session(
                dashboard_url=self.control_dashboard_url
            )
            self.shutdown_request_thread = threading.Thread(
                target=self._shutdown_request_worker,
                name="retrotv-shutdown-control",
                # This worker must survive the web server's shutdown long enough
                # to publish the final safe/error acknowledgement.  If it were a
                # daemon, Python could exit in the tiny gap between releasing the
                # instance lock and writing that acknowledgement.
                daemon=False,
            )
            self.shutdown_request_thread.start()
            with connect_database(self.config.database_path) as connection:
                connection.execute(
                    """
                    UPDATE announcement_jobs
                    SET status='stale', detail='Application restarted before job completion',
                        completed_at=?
                    WHERE status IN ('queued','running')
                    """,
                    (self.started_at.isoformat(timespec="seconds"),),
                )
                connection.commit()
            self.automation_thread = threading.Thread(
                target=self._automation_worker,
                name="retrotv-automation",
                daemon=True,
            )
            self.automation_thread.start()
            self.internet_media_thread = threading.Thread(
                target=self._internet_media_worker,
                name="retrotv-internet-media",
                daemon=True,
            )
            self.internet_media_thread.start()
        self.weather_companion.start()
        self.request_guide_refresh()
        self._refresh_pending_actions()
        window_active = self._check_maintenance_window(initial=True)
        if window_active:
            return
        if bool(self.settings.get("start_tv_on_launch", False)):
            self.start_tv(qualifying_activity=False)
        else:
            self.request_readiness_pipeline()

    def set_control_endpoint(self, host: str, port: int) -> None:
        url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        self.control_dashboard_url = f"http://{url_host}:{port}"

    def _shutdown_request_worker(self) -> None:
        while not self.stop_event.wait(0.5):
            request = self.shutdown_control.read_request()
            if request is None:
                continue
            request_id = str(request["request_id"])
            self.shutdown_control.clear_request(request_id)
            self.shutdown_control.write_status(
                request_id,
                state="stopping",
                message=(
                    "CRTV Sim accepted the safe-stop request and is waiting for "
                    "active work to finish."
                ),
            )
            result = self.shutdown()
            safe = result.get("safe_to_remove") is True
            self.shutdown_control.write_status(
                request_id,
                state="safe" if safe else "error",
                message=(
                    "CRTV Sim shut down safely."
                    if safe
                    else str(result.get("error") or "CRTV Sim safe shutdown failed")
                ),
                shutdown=result,
            )
            return

    def shutdown(self) -> dict[str, Any]:
        """Stop every writer and checkpoint SQLite before declaring storage safe."""
        with self.shutdown_lock:
            if self.shutdown_state["safe_to_remove"]:
                return dict(self.shutdown_state)
            started = datetime.now().astimezone().isoformat(timespec="seconds")
            shutdown_started = time.monotonic()
            stage_started = shutdown_started
            timed_stage = None

            def enter_stage(stage: str | None) -> None:
                nonlocal stage_started, timed_stage
                now = time.monotonic()
                with self.lock:
                    if timed_stage is not None:
                        seconds = round(now - stage_started, 3)
                        self.shutdown_state["stage_timings"][timed_stage] = seconds
                        print(f"[shutdown] {timed_stage}: {seconds:.3f}s", flush=True)
                    if stage is not None:
                        self.shutdown_state["stage"] = stage
                    self.shutdown_state["elapsed_seconds"] = round(now - shutdown_started, 3)
                timed_stage, stage_started = stage, now

            with self.lock:
                self._shutdown_clock_started = shutdown_started
                self.shutdown_state.update(
                    {
                        "state": "stopping",
                        "stage": "stopping television playback",
                        "started_at": self.shutdown_state["started_at"] or started,
                        "completed_at": None,
                        "safe_to_remove": False,
                        "error": None,
                        "stage_timings": {},
                        "elapsed_seconds": 0,
                    }
                )
            # This gate prevents automation and Dashboard actions from starting
            # more work while the current FFmpeg/database operation drains.
            self.stop_event.set()
            try:
                enter_stage("stopping television playback")
                self.stop_tv()
                enter_stage("stopping weather companion")
                self.weather_companion.stop()
                enter_stage("waiting for active work to finish")
                self._wait_for_background_writers()
                enter_stage("checkpointing database")
                with closing(connect_database(self.config.database_path)) as connection:
                    checkpoint = connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint is None or int(checkpoint[0]) != 0:
                        raise RuntimeError(
                            "SQLite could not finish its shutdown checkpoint"
                        )
                    # Full integrity scans belong to verified backup creation.
                    # A successful checkpoint protects pending writes without
                    # scanning several gigabytes on every idle shutdown.
                    enter_stage("checking recent backup; creating one if needed")
                    backup = DatabaseBackupService(self.config).create_for_shutdown(
                        connection
                    ).as_dict()
                    print(f"[shutdown] backup: {backup['reason'] or 'created and verified'}", flush=True)
                completed = datetime.now().astimezone().isoformat(timespec="seconds")
                with self.lock:
                    self.started_at = None
                    self.shutdown_state.update(
                        {
                            "state": "safe",
                            "stage": "complete",
                            "active_jobs": [],
                            "completed_at": completed,
                            "safe_to_remove": True,
                            "database_backup": backup,
                            "error": None,
                        }
                    )
            except Exception as exc:
                with self.lock:
                    self.shutdown_state.update(
                        {
                            "state": "error",
                            "stage": "shutdown check failed",
                            "safe_to_remove": False,
                            "error": str(exc),
                        }
                    )
            finally:
                enter_stage(None)
                self._release_instance_lock()
                self.shutdown_complete_event.set()
            return dict(self.shutdown_state)

    def shutdown_status(self) -> dict[str, Any]:
        """Read shutdown progress without opening the database or scanning media."""
        with self.lock:
            result = {
                **self.shutdown_state,
                "active_jobs": list(self.shutdown_state.get("active_jobs", [])),
                "stage_timings": dict(self.shutdown_state.get("stage_timings", {})),
            }
            if result["state"] == "stopping" and hasattr(self, "_shutdown_clock_started"):
                result["elapsed_seconds"] = round(time.monotonic() - self._shutdown_clock_started, 3)
        return result

    def _wait_for_background_writers(self) -> None:
        current = threading.current_thread()
        threads = (
            ("shutdown control", self.shutdown_request_thread),
            ("automation", self.automation_thread),
            ("internet media", self.internet_media_thread),
            ("playback", self.playback_thread),
            ("readiness", self.readiness_thread),
            ("maintenance", self.maintenance_thread),
            ("video conversion", self.normalization_batch_thread),
            ("audio analysis", self.audio_analysis_thread),
            ("audio normalization", self.audio_normalization_thread),
            ("generated cleanup", self.generated_cleanup_thread),
            ("database backup", self.database_backup_thread),
            ("database housekeeping", self.database_housekeeping_thread),
            ("database compaction", self.database_compaction_thread),
            ("internet cache cleanup", self.cache_cleanup_thread),
            ("normalization review", self.normalization_review_thread),
            ("missing media scan", self.library_recovery_thread),
            ("Guide rendering", self.guide_thread),
            ("announcement rendering", self.announcement_thread),
        )
        while True:
            active = [
                (label, thread)
                for label, thread in threads
                if thread is not None and thread is not current and thread.is_alive()
            ]
            with self.lock:
                self.shutdown_state.update(
                    {
                        "stage": (
                            "waiting for active work to finish"
                            if active else "closing background work"
                        ),
                        "active_jobs": [label for label, _thread in active],
                    }
                )
            if not active:
                return
            for _label, thread in active:
                thread.join(timeout=0.25)

    def channels(self) -> list[dict[str, Any]]:
        configured = self.config.data.get("guide", {}).get("channel_numbers", {})
        with connect_database(self.config.database_path) as connection:
            rows = connection.execute(
                """
                SELECT c.slug, c.display_name, c.channel_number,
                       COALESCE(p.playback_mode,'scheduled') playback_mode
                FROM channels c LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id
                WHERE c.enabled = 1
                """
            ).fetchall()
        items = [
            {
                "slug": row["slug"],
                "display_name": row["display_name"],
                "channel_number": configured.get(
                    row["slug"], row["channel_number"]
                ),
                "is_guide": False,
                "playback_mode": row["playback_mode"],
            }
            for row in rows
            # Synthetic channels are owned by their publication readers, not
            # conventional channel rows that may have been left behind by an
            # older maintenance run.
            if str(row["slug"]) != "weather"
        ]
        weather = weather_channel_definition(self.config)
        if weather_channel_available(self.config, self.synthetic_readers["weather"]):
            items.append(
                {
                    "slug": weather.slug,
                    "display_name": weather.display_name,
                    "channel_number": weather.channel_number,
                    "is_guide": False,
                    "is_synthetic": True,
                }
            )
        guide_number = int(
            self.config.data.get("guide", {}).get("channel_number", 2)
        )
        items.append(
            {
                "slug": "__guide__",
                "display_name": "CRTV Sim Guide",
                "channel_number": guide_number,
                "is_guide": True,
            }
        )
        items.sort(
            key=lambda item: (
                item["channel_number"] is None,
                item["channel_number"] or 9999,
                item["display_name"].casefold(),
            )
        )
        return items

    def status(self) -> dict[str, Any]:
        with self.lock:
            controller = self.controller
            current = controller.current if controller else None
            if self.tv_state == "starting" and current is not None:
                self.tv_state = "playing"
            external = bool(current and getattr(current, 'external', False))
            synthetic_channels = self._synthetic_status()
            return {
                "runtime": "running" if self.started_at else "stopped",
                "started_at": (
                    self.started_at.isoformat(timespec="seconds")
                    if self.started_at
                    else None
                ),
                "tv_state": self.tv_state,
                "tv_error": self.tv_error,
                "requested_channel": self.requested_channel,
                "previous_channel": self.previous_channel,
                "digit_buffer": self.digit_buffer,
                "now_playing": (
                    {
                        "title": current.title,
                        "asset_id": current.asset_id,
                        "external": external,
                        "missing_media": current.missing_media,
                        "entry_type": current.entry_type,
                        "representation": current.representation,
                        "starts_at": current.starts_at.isoformat(),
                        "ends_at": current.ends_at.isoformat(),
                    }
                    if current
                    else None
                ),
                "channels": self.channels(),
                "maintenance": dict(self.maintenance),
                "normalization_review": dict(self.normalization_review),
                "playback_recovery": dict(self.playback_recovery),
                "readiness": dict(self.readiness),
                "internet_media": dict(self.internet_media),
                "normalization_batch": {
                    **self.normalization_batch,
                    "failures": list(self.normalization_batch["failures"]),
                },
                "audio_analysis_batch": {
                    **self.audio_analysis_batch,
                    "failures": list(self.audio_analysis_batch["failures"]),
                },
                "audio_normalization_batch": {
                    **self.audio_normalization_batch,
                    "failures": list(self.audio_normalization_batch["failures"]),
                },
                "generated_cleanup": {
                    **self.generated_cleanup,
                    "errors": list(self.generated_cleanup["errors"]),
                },
                "database_backup": dict(self.database_backup),
                "database_housekeeping": dict(self.database_housekeeping),
                "database_compaction": dict(self.database_compaction),
                "cache_cleanup": {
                    **self.cache_cleanup,
                    "errors": list(self.cache_cleanup.get("errors", [])),
                },
                "maintenance_window": {
                    **self.maintenance_window_schedule.status(
                        self.maintenance_window_state
                    ),
                    "stage": self.readiness.get("stage"),
                },
                "shutdown": {
                    **self.shutdown_state,
                    "active_jobs": list(self.shutdown_state["active_jobs"]),
                },
                "pending_actions": dict(self.pending_actions),
                "announcements": {
                    **self.announcement_state,
                    "automation_thread_alive": bool(
                        self.automation_thread and self.automation_thread.is_alive()
                    ),
                    "tuned_since": (
                        self.tuned_since.isoformat(timespec="seconds")
                        if self.tuned_since else None
                    ),
                    "last_qualifying_user_activity": (
                        self.last_qualifying_user_activity.isoformat(timespec="seconds")
                        if self.last_qualifying_user_activity else None
                    ),
                },
                "diagnostics": self._diagnostics(),
                "synthetic_channels": synthetic_channels,
            }

    def _synthetic_status(self) -> dict[str, Any]:
        definition = weather_channel_definition(self.config)
        status = self.synthetic_readers["weather"].refresh()
        payload = status.as_dict()
        age = status.age_seconds
        threshold_warning = bool(
            age is not None and age > definition.stale_warning_seconds
        )
        payload.update(
            {
                "slug": definition.slug,
                "display_name": definition.display_name,
                "enabled": definition.enabled,
                "channel_number": definition.channel_number,
                "channel_root": str(definition.channel_root),
                "dashboard_url": definition.dashboard_url,
                "companion": self.weather_companion.status(),
                "setup_required": definition.enabled and not status.ready,
                "setup_message": (
                    "Choose your location in RetroWeather, then use Fetch weather + create channel. "
                    "Weather becomes available to tune after its first playable video is ready."
                    if definition.enabled and not status.ready else None
                ),
                "stale_warning_seconds": definition.stale_warning_seconds,
                "warning": bool(
                    definition.enabled
                    and (
                        not status.ready
                        or status.stale
                        or threshold_warning
                        or status.error
                    )
                ),
            }
        )
        if status.ready and status.video_path is not None:
            try:
                position = self.synthetic_readers["weather"].playback_position()
            except ValueError:
                position = None
            if position is not None:
                payload["current_segment"] = {
                    "id": position.segment_id,
                    "title": position.segment_title,
                    "offset_seconds": round(position.offset_seconds, 3),
                }
        return {definition.slug: payload}

    def restart_weather_companion(self) -> bool:
        return self.weather_companion.request_restart()

    def _diagnostics(self) -> dict[str, Any]:
        player = str(self.config.data.get("player", {}).get("mpv", "mpv"))
        return {
            "profile": self.config.profile_name,
            "media_root": str(self.config.storage_root("media")),
            "media_root_available": self.config.storage_root("media").is_dir(),
            "database": str(self.config.database_path),
            "ffmpeg_available": self._command_available(self.config.ffmpeg),
            "ffprobe_available": self._command_available(self.config.ffprobe),
            "mpv_available": self._command_available(player),
        }

    def _refresh_pending_actions(self) -> None:
        try:
            with connect_database(self.config.database_path) as connection:
                summary = GuideService(
                    connection, self.config
                )._pending_action_summary()
            with self.lock:
                self.pending_actions = dict(summary)
        except Exception as exc:
            with self.lock:
                self.pending_actions = {
                    **self.pending_actions,
                    "error": str(exc),
                }

    @staticmethod
    def _command_available(value: str) -> bool:
        candidate = os.path.expandvars(os.path.expanduser(value))
        return bool(shutil.which(candidate) or os.path.isfile(candidate))

    def _acquire_instance_lock(self) -> None:
        target = self.config.storage_root("state") / "retrotv.instance.lock"
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.open("a+b")
        handle.seek(0)
        if handle.tell() == 0 and target.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another CRTV Sim application instance is running") from exc
        self._instance_lock = handle

    def _release_instance_lock(self) -> None:
        handle = self._instance_lock
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._instance_lock = None

    def start_tv(
        self, channel: str | None = None, *, qualifying_activity: bool = True
    ) -> None:
        with self.lock:
            if (self.normalization_review_thread and self.normalization_review_thread.is_alive()
                    and self.normalization_review_thread is not threading.current_thread()):
                raise ValueError("Wait for normalization re-review to release the working file")
        if qualifying_activity and self._window_is_active_and_unfinished():
            self.skip_current_maintenance_window(start_tv=False)
        requested = channel or str(
            self.settings.get("startup_channel", "fox")
        )
        requested = self._resolve_channel(requested)
        if qualifying_activity:
            self._record_human_activity()
        with self.lock:
            if self.playback_thread and self.playback_thread.is_alive():
                self.tune(requested)
                return
            self.tv_state = "starting"
            self.tv_error = None
            self.requested_channel = requested
            self.tuned_since = datetime.now(self.timezone)
            self.playback_thread = threading.Thread(
                target=self._playback_worker,
                args=(requested,),
                name="retrotv-playback",
                daemon=True,
            )
            self.playback_thread.start()

    def stop_tv(self, *, for_maintenance: bool = False, _for_review: bool = False) -> None:
        if not _for_review:
            self._review_resume = False
        if not for_maintenance and self._window_is_active_and_unfinished():
            with self.lock:
                self.maintenance_window_state["auto_resume"] = False
                self.maintenance_window_state["reason"] = "TV was stopped manually"
                self._persist_maintenance_window()
        with self.lock:
            controller = self.controller
            thread = self.playback_thread
            if controller:
                controller.stop()
            self.tv_state = "stopping" if thread and thread.is_alive() else "stopped"
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self.lock:
            if not thread or not thread.is_alive():
                self.tv_state = "stopped"
        if not _for_review:
            self.request_readiness_pipeline(
                trigger="maintenance_window" if for_maintenance else "idle"
            )

    def tune(self, channel: str) -> None:
        if self.normalization_review_thread and self.normalization_review_thread.is_alive():
            raise ValueError("Wait for normalization re-review before changing channels")
        resolved = self._resolve_channel(channel)
        self._record_human_activity()
        with self.lock:
            if self.requested_channel != resolved:
                self.previous_channel = self.requested_channel
                self.tuned_since = datetime.now(self.timezone)
            self.requested_channel = resolved
            controller = self.controller
            if controller is None or not self.playback_thread:
                self.start_tv(resolved)
                return
            controller.request_tune(resolved)
            self._show_channel_osd(resolved)

    def tune_number(self, channel_number: int) -> None:
        matches = [
            item
            for item in self.channels()
            if item["channel_number"] == channel_number
        ]
        if not matches:
            raise ValueError(f"Channel {channel_number} is not configured")
        if len(matches) > 1:
            raise ValueError(f"Channel number {channel_number} is duplicated")
        self.tune(str(matches[0]["slug"]))

    def _report_missing_media(self, missing: dict) -> None:
        key = (missing["asset_id"], missing["path"])
        with self.lock:
            self.playback_recovery["missing"] = missing
            if key not in self._missing_paths_seen:
                self._missing_paths_seen.add(key)
                self._missing_scan_pending = True
                self._missing_scan_not_before = 0.0
                if self.playback_recovery["state"] != "scanning":
                    self.playback_recovery.update(state="queued", error=None)

    def _request_missing_media_scan(self) -> bool:
        with self.lock:
            if (self.playback_recovery["state"] not in {"queued", "deferred"} or self.stop_event.is_set()
                    or time.monotonic() < self._missing_scan_not_before
                    or self._exclusive_media_job_running()):
                return False
            self.library_recovery_thread = threading.Thread(
                target=self._missing_media_scan_worker, name="retrotv-missing-media-scan", daemon=True)
            self.library_recovery_thread.start()
            return True

    def _pause_internet_writer(self) -> None:
        self.internet_pause_event.set()
        if not self.internet_writer_lock.acquire(timeout=30):
            raise ValueError("Internet preparation is still finishing; try again when it is idle")
        self.internet_writer_lock.release()
        if self.stop_event.is_set():
            raise ValueError("CRTV Sim is shutting down")

    def _missing_media_scan_worker(self) -> None:
        scan_started = False
        with self.lock:
            missing = dict(self.playback_recovery["missing"])
            self._missing_scan_pending = False
            self.playback_recovery.update(state="scanning", error=None)
        try:
            with self.exclusive_operation_lock:
                self._pause_internet_writer()
                scan_started = True
                with connect_database(self.config.database_path) as connection:
                    from retrotv.asset_paths import is_external, resolve_asset_path
                    if is_external(connection, missing['asset_id']):
                        from retrotv.external_media import ExternalMediaService
                        root_id = connection.execute('SELECT source_root_id FROM assets WHERE id=?', (missing['asset_id'],)).fetchone()[0]
                        ExternalMediaService(connection, self.config).scan(root_id, cancel_event=self.stop_event)
                    else:
                        LibraryScanner(connection, self.config).scan(
                            skip_samples=True, cancel_requested=self.stop_event.is_set)
                    row = connection.execute("SELECT source_relative_path FROM assets WHERE id=?",
                                             (missing["asset_id"],)).fetchone()
                    repaired = bool(row and resolve_asset_path(connection, self.config, missing["asset_id"]).is_file())
                with self.lock:
                    self.playback_recovery.update(state="queued" if self._missing_scan_pending else
                                                  "recovered" if repaired else "missing")
                    if repaired and self.controller:
                        self.controller.request_retune()
        except Exception as exc:
            with self.lock:
                deferred = not scan_started and not self.stop_event.is_set()
                self.playback_recovery.update(state="deferred" if deferred else "failed", error=str(exc))
                if deferred:
                    self._missing_scan_not_before = time.monotonic() + 60
        finally:
            self.internet_pause_event.clear()

    def request_normalization_review(self, asset_id: int) -> bool:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "normalization")
        with self.lock:
            if self.stop_event.is_set():
                raise ValueError("CRTV Sim is shutting down")
            current = self.controller.current if self.controller else None
            if not current or current.asset_id != asset_id:
                raise ValueError("The playing media changed; refresh the Dashboard and try again")
            if self._exclusive_media_job_running():
                raise ValueError("Wait for the current maintenance job to finish before re-review")
            if any(thread and thread.is_alive() for thread in (self.guide_thread, self.announcement_thread)):
                raise ValueError("Wait for Guide or announcement generation to finish before re-review")
            self._review_resume = True
            self.normalization_review = {"state": "running", "asset_id": asset_id, "error": None}
            self.normalization_review_thread = threading.Thread(
                target=self._normalization_review_worker, args=(asset_id, self.requested_channel),
                name="retrotv-normalization-review", daemon=True)
            self.normalization_review_thread.start()
            return True

    def _normalization_review_worker(self, asset_id: int, channel: str) -> None:
        from retrotv.normalization_review import NormalizationReviewService
        try:
            self.stop_tv(_for_review=True)
            if self.playback_thread and self.playback_thread.is_alive():
                raise ValueError("The player has not released the file; no restore was attempted")
            with self.exclusive_operation_lock:
                self._pause_internet_writer()
                with connect_database(self.config.database_path) as connection:
                    result = NormalizationReviewService(connection, self.config).reopen(asset_id)
                with self.lock:
                    self.normalization_review.update(result, state="failed" if result.get("error") else "completed")
        except Exception as exc:
            with self.lock:
                self.normalization_review.update(state="failed", error=str(exc))
        finally:
            self.internet_pause_event.clear()
            with self.lock:
                if self._review_resume and not self.stop_event.is_set():
                    self.start_tv(channel, qualifying_activity=False)
                self._review_resume = False

    def request_maintenance(self) -> bool:
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.maintenance_thread = threading.Thread(
                target=self._maintenance_worker,
                name="retrotv-maintenance",
                daemon=True,
            )
            self.maintenance_thread.start()
            return True

    def request_normalization_batch(self) -> bool:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "normalization")
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.normalization_batch_thread = threading.Thread(
                target=self._normalization_batch_worker,
                name="retrotv-normalization-batch",
                daemon=True,
            )
            self.normalization_batch_thread.start()
            return True

    def request_audio_analysis(self) -> bool:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "audio_normalization")
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.audio_analysis_thread = threading.Thread(
                target=self._audio_analysis_worker,
                name="retrotv-audio-analysis",
                daemon=True,
            )
            self.audio_analysis_thread.start()
            return True

    def request_audio_normalization(self) -> bool:
        from retrotv.processing import require_enabled
        require_enabled(self.config, "audio_normalization")
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.audio_normalization_thread = threading.Thread(
                target=self._audio_normalization_worker,
                name="retrotv-audio-normalization",
                daemon=True,
            )
            self.audio_normalization_thread.start()
            return True

    def _exclusive_media_job_running(self) -> bool:
        current = threading.current_thread()
        return any(
            thread is not current and thread is not None and thread.is_alive()
            for thread in (
                self.readiness_thread,
                self.maintenance_thread,
                self.normalization_batch_thread,
                self.audio_analysis_thread,
                self.audio_normalization_thread,
                self.generated_cleanup_thread,
                self.database_backup_thread,
                self.database_housekeeping_thread,
                self.database_compaction_thread,
                self.cache_cleanup_thread,
                self.normalization_review_thread,
                self.library_recovery_thread,
            )
        )

    def _tv_is_idle(self) -> bool:
        with self.lock:
            playback_alive = bool(
                self.playback_thread and self.playback_thread.is_alive()
            )
            return (
                not playback_alive
                and self.controller is None
                and self.tv_state in {"stopped", "error"}
            )

    def request_readiness_pipeline(self, *, trigger: str = "idle") -> bool:
        if (
            trigger != "maintenance_window"
            and not bool(self.settings.get("auto_readiness_when_tv_idle", True))
        ):
            return False
        if self.stop_event.is_set():
            return False
        if (
            trigger != "maintenance_window"
            and self.maintenance_window_state.get("state") == "ending"
        ):
            return False
        with self.lock:
            if not self._tv_is_idle() or self._exclusive_media_job_running():
                return False
            self.maintenance_cancel_event.clear()
            self.readiness["trigger"] = trigger
            self.readiness_thread = threading.Thread(
                target=self._readiness_worker,
                name="retrotv-readiness",
                daemon=True,
            )
            self.readiness_thread.start()
            return True

    def request_generated_cleanup(self) -> bool:
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.generated_cleanup_thread = threading.Thread(
                target=self._generated_cleanup_worker,
                name="retrotv-generated-cleanup",
                daemon=True,
            )
            self.generated_cleanup_thread.start()
            return True

    def request_database_backup(self) -> bool:
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.database_backup_thread = threading.Thread(
                target=self._database_backup_worker,
                name="retrotv-database-backup",
                daemon=True,
            )
            self.database_backup_thread.start()
            return True

    def request_database_housekeeping(
        self, *, force: bool = True, trigger: str = "manual"
    ) -> bool:
        if self.stop_event.is_set() or not self._tv_is_idle():
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.database_housekeeping_thread = threading.Thread(
                target=self._database_housekeeping_worker,
                kwargs={"force": force, "trigger": trigger},
                name="retrotv-database-housekeeping",
                daemon=True,
            )
            self.database_housekeeping_thread.start()
            return True

    def database_compaction_preview(self) -> dict[str, int]:
        return DatabaseCompactionService(self.config).preview()

    def request_database_compaction(self) -> bool:
        if (
            self.stop_event.is_set() or not self._tv_is_idle()
            or self.internet_media.get("state") == "working"
        ):
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            self.internet_pause_event.set()
            self.database_compaction_thread = threading.Thread(
                target=self._database_compaction_worker,
                name="retrotv-database-compaction",
                daemon=True,
            )
            self.database_compaction_thread.start()
            return True

    def internet_cache_preview(self) -> dict[str, object]:
        with connect_database(self.config.database_path) as connection:
            return PreparationService(connection, self.config).preview_empty_cache(
                active_path=self._active_media_path()
            )

    def request_internet_cache_cleanup(self) -> bool:
        if self.stop_event.is_set() or self.internet_media.get("state") == "working":
            return False
        with self.lock:
            if self._exclusive_media_job_running():
                return False
            if self.cache_cleanup_thread and self.cache_cleanup_thread.is_alive():
                return False
            self.internet_pause_event.set()
            self.cache_cleanup_thread = threading.Thread(
                target=self._internet_cache_cleanup_worker,
                name="retrotv-internet-cache-cleanup",
                daemon=True,
            )
            self.cache_cleanup_thread.start()
            return True

    def _active_media_path(self) -> str | None:
        with self.lock:
            current = self.controller.current if self.controller else None
            return str(current.path.resolve()) if current is not None else None

    def request_guide_refresh(self, *, force: bool = False) -> bool:
        if self.stop_event.is_set():
            return False
        with self.lock:
            if self.normalization_review_thread and self.normalization_review_thread.is_alive():
                return False
            if self.guide_thread and self.guide_thread.is_alive():
                return False
            self.guide_thread = threading.Thread(
                target=self._refresh_guide_if_needed,
                args=(force,),
                name="retrotv-guide-refresh",
                daemon=True,
            )
            self.guide_thread.start()
            return True

    def _resolve_channel(self, value: str) -> str:
        cleaned = value.strip().casefold()
        if cleaned.isdigit():
            matches = [
                item for item in self.channels()
                if item["channel_number"] == int(cleaned)
            ]
            if len(matches) == 1:
                return str(matches[0]["slug"])
        if cleaned in {"guide", "__guide__"}:
            return "__guide__"
        if cleaned == "weather" and weather_channel_definition(self.config).enabled:
            if not weather_channel_available(self.config, self.synthetic_readers["weather"]):
                raise ValueError(
                    "RetroWeather is not ready. Open RetroWeather, choose your location, "
                    "and use Fetch weather + create channel before tuning it."
                )
            return "weather"
        if cleaned.isdigit() and weather_channel_definition(self.config).enabled:
            if int(cleaned) == weather_channel_definition(self.config).channel_number:
                return self._resolve_channel("weather")
        slugs = {str(item["slug"]) for item in self.channels()}
        if cleaned not in slugs:
            raise ValueError(f"Unknown or disabled channel: {value}")
        return cleaned

    def _playback_worker(self, channel: str) -> None:
        connection = connect_database(self.config.database_path)
        try:
            resolver = ScheduleResolver(
                connection,
                self.config,
                synthetic_readers={
                    slug: reader
                    for slug, reader in self.synthetic_readers.items()
                    if slug != "weather"
                    or weather_channel_definition(self.config).enabled
                },
                missing_media_handler=self._report_missing_media,
            )
            backend = str(
                self.config.data.get("player", {}).get("backend", "auto")
            )
            player = create_player(self.config, backend)
            controller = PlaybackController(
                resolver,
                player,
                skip_short_remaining_seconds=float(
                    self.config.data.get("player", {}).get(
                        "skip_short_remaining_seconds", 1.0
                    )
                ),
                action_handler=self._handle_player_action,
            )
            with self.lock:
                self.controller = controller
            controller.run(channel)
        except (PlaybackError, OSError, ValueError) as exc:
            with self.lock:
                self.tv_error = str(exc)
                self.tv_state = "error"
        finally:
            connection.close()
            with self.lock:
                self.controller = None
                if self.tv_state != "error":
                    self.tv_state = "stopped"

    def _handle_player_action(self, action: str) -> None:
        # A remote action must not terminate playback when its destination is
        # unavailable (for example Last channel after Weather loses its video).
        try:
            self._dispatch_player_action(action)
        except ValueError as exc:
            self._show_text(str(exc))

    def _dispatch_player_action(self, action: str) -> None:
        self._record_human_activity()
        if action.startswith("digit:"):
            digit = action.partition(":")[2]
            if digit.isdigit() and len(self.digit_buffer) < 3:
                self.digit_buffer += digit
                self._show_text(f"CH {self.digit_buffer}_")
            return
        if action == "backspace":
            self.digit_buffer = self.digit_buffer[:-1]
            self._show_text(
                f"CH {self.digit_buffer}_" if self.digit_buffer else "CHANNEL"
            )
            return
        if action == "cancel":
            self.digit_buffer = ""
            self._show_text("CHANNEL ENTRY CANCELED")
            return
        if action == "enter":
            if self.digit_buffer:
                number = int(self.digit_buffer)
                self.digit_buffer = ""
                try:
                    self.tune_number(number)
                except ValueError as exc:
                    self._show_text(str(exc))
            return
        if action == "guide":
            self.tune("__guide__")
        elif action == "retune":
            if self.controller:
                self.controller.request_retune()
        elif action == "volume-up" and self.controller:
            self.controller.player.adjust_volume(5)
        elif action == "volume-down" and self.controller:
            self.controller.player.adjust_volume(-5)
        elif action == "mute" and self.controller:
            self.controller.player.toggle_mute()
        elif action == "last-channel" and self.previous_channel:
            self.tune(self.previous_channel)
        elif action in {"channel-up", "channel-down"}:
            self._channel_step(1 if action == "channel-up" else -1)
        elif action == "quit":
            if self.controller:
                self.controller.stop()

    def _record_human_activity(self) -> None:
        with self.lock:
            self.last_qualifying_user_activity = datetime.now(self.timezone)

    def _channel_step(self, direction: int) -> None:
        channels = self.channels()
        slugs = [str(item["slug"]) for item in channels]
        current = self.requested_channel
        index = slugs.index(current) if current in slugs else 0
        self.tune(slugs[(index + direction) % len(slugs)])

    def _show_channel_osd(self, slug: str) -> None:
        item = next(
            (item for item in self.channels() if item["slug"] == slug), None
        )
        if item:
            display_name = str(item["display_name"])
            if not item.get("is_guide") and not item.get("is_synthetic"):
                with connect_database(self.config.database_path) as connection:
                    graphics = GraphicsService(connection, self.config)
                    display_name = graphics.identity_name(
                        slug,
                        display_name,
                        graphics.daypart_slug_at(datetime.now(self.timezone)),
                    )
            self._show_text(
                f"CH {item['channel_number']}  {display_name.upper()}"
            )

    def _show_text(self, value: str) -> None:
        controller = self.controller
        if controller is None:
            return
        try:
            controller.player.show_text(value)
        except (PlaybackError, OSError):
            pass

    def _window_is_active_and_unfinished(self) -> bool:
        occurrence = self.maintenance_window_schedule.active()
        if occurrence is None:
            return False
        state = self.maintenance_window_state
        return not (
            state.get("occurrence_date") == occurrence.occurrence_date
            and state.get("state") in {"completed_early", "complete", "skipped", "failed"}
        )

    def _persist_maintenance_window(self) -> None:
        self.maintenance_window_store.write(dict(self.maintenance_window_state))

    def skip_current_maintenance_window(self, *, start_tv: bool = True) -> bool:
        occurrence = self.maintenance_window_schedule.active()
        if occurrence is None:
            return False
        self.maintenance_cancel_event.set()
        with self.lock:
            self.maintenance_window_state.update(
                {
                    "occurrence_date": occurrence.occurrence_date,
                    "state": "skipped",
                    "auto_resume": False,
                    "completed_at": datetime.now(self.timezone).isoformat(timespec="seconds"),
                    "reason": "Skipped for manual playback",
                }
            )
            self._persist_maintenance_window()
            channel = str(
                self.maintenance_window_state.get("resume_channel")
                or self.requested_channel
                or self.settings.get("startup_channel", "fox")
            )
        if start_tv and self._tv_is_idle():
            self.start_tv(channel, qualifying_activity=False)
        return True

    def _check_maintenance_window(self, *, initial: bool = False) -> bool:
        schedule = self.maintenance_window_schedule
        if not schedule.enabled:
            return False
        occurrence = schedule.active()
        if occurrence is None:
            state = self.maintenance_window_state
            if state.get("state") in {"active", "ending"}:
                self.maintenance_cancel_event.set()
                with self.lock:
                    state["state"] = "ending"
                    state["reason"] = "Configured maintenance window ended"
                    self._persist_maintenance_window()
                thread = self.readiness_thread
                if thread is None or not thread.is_alive():
                    self._finish_maintenance_window("complete", "Maintenance window ended")
                return True
            return False

        state = self.maintenance_window_state
        same_occurrence = state.get("occurrence_date") == occurrence.occurrence_date
        if same_occurrence and state.get("state") in {
            "completed_early", "complete", "skipped", "failed"
        }:
            return True

        if initial and same_occurrence and state.get("state") in {"active", "ending"}:
            state["state"] = "active"
            state["pipeline_started"] = False
            self._persist_maintenance_window()

        if not same_occurrence or state.get("state") not in {"active", "ending"}:
            tv_running = not self._tv_is_idle()
            auto_resume = tv_running or (
                initial and bool(self.settings.get("start_tv_on_launch", False))
            )
            channel = str(
                self.requested_channel or self.settings.get("startup_channel", "fox")
            )
            self.maintenance_cancel_event.clear()
            with self.lock:
                self.maintenance_window_state = {
                    "occurrence_date": occurrence.occurrence_date,
                    "state": "active",
                    "auto_resume": auto_resume,
                    "resume_channel": channel,
                    "started_at": datetime.now(self.timezone).isoformat(timespec="seconds"),
                    "completed_at": None,
                    "pipeline_started": False,
                    "reason": None,
                }
                self._persist_maintenance_window()
            if tv_running:
                self.stop_tv(for_maintenance=True)
                with self.lock:
                    if self.readiness_thread and self.readiness_thread.is_alive():
                        self.maintenance_window_state["pipeline_started"] = True
                        self._persist_maintenance_window()

        if not self._tv_is_idle():
            return True
        state = self.maintenance_window_state
        if not state.get("pipeline_started"):
            started = self.request_readiness_pipeline(trigger="maintenance_window")
            if started:
                with self.lock:
                    self.maintenance_window_state["pipeline_started"] = True
                    self._persist_maintenance_window()
            return True

        thread = self.readiness_thread
        if thread is not None and not thread.is_alive():
            readiness_state = str(self.readiness.get("state"))
            if readiness_state == "ready":
                self._finish_maintenance_window(
                    "completed_early", "All maintenance work completed early"
                )
            elif readiness_state == "error":
                self._finish_maintenance_window(
                    "failed", str(self.readiness.get("error") or "Maintenance failed")
                )
        return True

    def _finish_maintenance_window(self, state_name: str, reason: str) -> None:
        with self.lock:
            should_resume = bool(self.maintenance_window_state.get("auto_resume"))
            channel = str(
                self.maintenance_window_state.get("resume_channel")
                or self.settings.get("startup_channel", "fox")
            )
            self.maintenance_window_state.update(
                {
                    "state": state_name,
                    "completed_at": datetime.now(self.timezone).isoformat(timespec="seconds"),
                    "reason": reason,
                    "auto_resume": False,
                }
            )
            self._persist_maintenance_window()
        if should_resume and not self.stop_event.is_set() and self._tv_is_idle():
            self.start_tv(channel, qualifying_activity=False)

    def _automation_worker(self) -> None:
        maintenance_interval = max(
            60, int(self.settings.get("maintenance_interval_seconds", 900))
        )
        guide_interval = max(
            15, int(self.settings.get("guide_check_interval_seconds", 60))
        )
        announcement_interval = max(
            1,
            int(
                self.config.data.get("announcements", {}).get(
                    "poll_interval_seconds", 5
                )
            ),
        )
        next_maintenance = time.monotonic() + maintenance_interval
        next_guide = time.monotonic()
        while not self.stop_event.wait(announcement_interval):
            self._request_missing_media_scan()
            window_controlled = False
            try:
                window_controlled = self._check_maintenance_window()
            except Exception as exc:
                with self.lock:
                    self.maintenance_window_state["reason"] = str(exc)
            try:
                if time.monotonic() >= next_guide:
                    self.request_guide_refresh()
                    next_guide = time.monotonic() + guide_interval
            except Exception as exc:
                with self.lock:
                    self.maintenance["error"] = f"Guide polling failed: {exc}"
            try:
                self._check_announcement_demand()
            except Exception as exc:
                with self.lock:
                    self.announcement_state.update(
                        {
                            "eligibility_reason": "Announcement demand check failed; retrying",
                            "generation_state": "failed",
                            "error": str(exc),
                        }
                    )
            try:
                if time.monotonic() >= next_maintenance and not window_controlled:
                    self.request_readiness_pipeline()
                    next_maintenance = time.monotonic() + maintenance_interval
            except Exception as exc:
                with self.lock:
                    self.maintenance["error"] = f"Maintenance polling failed: {exc}"

    def _internet_media_worker(self) -> None:
        """Prepare one persistent internet-media job at a time."""
        with connect_database(self.config.database_path) as connection:
            service = PreparationService(
                connection, self.config, stop_event=self.stop_event
            )
            with self.internet_writer_lock:
                service.recover_interrupted()
                service.cleanup_completed_cache()
                try:
                    InternetMediaService(connection, self.config).sync_source_files()
                except Exception as exc:
                    with self.lock:
                        self.internet_media["error"] = str(exc)
            while not self.stop_event.is_set():
                if self.internet_pause_event.is_set():
                    with self.lock:
                        self.internet_media.update(
                            {"state": "paused", "current": None, "error": None}
                        )
                    if self.stop_event.wait(0.5):
                        break
                    continue
                try:
                    with self.internet_writer_lock:
                        if self.internet_pause_event.is_set():
                            continue
                        service.reconcile_retained()
                        tuned = self.controller.channel_slug if self.controller is not None else None
                        service.queue_all(tuned)
                        result = service.run_next()
                    with self.lock:
                        self.internet_media.update(
                            {
                                "state": "working" if result else "idle",
                                "current": result,
                                "completed_at": (
                                    datetime.now().astimezone().isoformat(timespec="seconds")
                                    if result
                                    else self.internet_media["completed_at"]
                                ),
                                "error": result.get("error") if result else None,
                            }
                        )
                except Exception as exc:
                    with self.lock:
                        self.internet_media.update(
                            {"state": "error", "error": str(exc)}
                        )
                    if self.stop_event.wait(10):
                        break
                    continue
                if result is None and self.stop_event.wait(5):
                    break

    def _readiness_worker(self) -> None:
        started = datetime.now().astimezone()
        with self.lock:
            self.readiness.update(
                {
                    "state": "running",
                    "stage": "scan and update",
                    "failed_stage": None,
                    "started_at": started.isoformat(timespec="seconds"),
                    "completed_at": None,
                    "error": None,
                }
            )
        try:
            stages = [
                ("scan and update", self._maintenance_worker),
            ]
            if bool(self.settings.get("auto_convert_approved_media", True)):
                stages.append(
                    (
                        "convert approved media",
                        lambda: self._normalization_batch_worker(idle_only=True),
                    )
                )
            if bool(self.settings.get("auto_analyze_pending_audio", True)):
                stages.append(
                    (
                        "analyze pending audio",
                        lambda: self._audio_analysis_worker(idle_only=True),
                    )
                )
            if bool(self.settings.get("auto_normalize_approved_audio", True)):
                stages.append(
                    (
                        "normalize approved audio",
                        lambda: self._audio_normalization_worker(idle_only=True),
                    )
                )

            for stage, worker in stages:
                if self.stop_event.is_set() or self.maintenance_cancel_event.is_set():
                    with self.lock:
                        self.readiness.update(
                            {"state": "deferred", "stage": stage, "error": None}
                        )
                    return
                if not self._tv_is_idle():
                    with self.lock:
                        self.readiness.update(
                            {
                                "state": "deferred",
                                "stage": stage,
                                "error": None,
                            }
                        )
                    return
                with self.lock:
                    self.readiness["stage"] = stage
                worker()
                worker_state = {
                    "scan and update": self.maintenance,
                    "convert approved media": self.normalization_batch,
                    "analyze pending audio": self.audio_analysis_batch,
                    "normalize approved audio": self.audio_normalization_batch,
                }[stage]
                if worker_state.get("state") == "error":
                    raise RuntimeError(
                        str(worker_state.get("error") or f"{stage} failed")
                    )
            with self.lock:
                self.readiness.update(
                    {"state": "ready", "stage": None, "error": None}
                )
        except Exception as exc:
            with self.lock:
                self.readiness.update(
                    {"state": "error", "stage": None, "error": str(exc),
                     "failed_stage": (
                         self.maintenance.get("failed_stage")
                         if self.readiness.get("stage") == "scan and update"
                         else self.readiness.get("stage")) or "scan and update"}
                )
            print(f"[readiness] Failed during {self.readiness['failed_stage']}: {exc}", file=sys.stderr)
        finally:
            with self.lock:
                self.readiness["completed_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )

    def _check_announcement_demand(self) -> None:
        if self.normalization_review_thread and self.normalization_review_thread.is_alive():
            return
        settings = self.config.data.get("announcements", {})
        now = datetime.now(self.timezone)
        with self.lock:
            self.announcement_state["last_poll_at"] = now.isoformat(timespec="seconds")
            channel = self.requested_channel
            tuned_since = self.tuned_since
            activity = self.last_qualifying_user_activity
            busy = bool(self.announcement_thread and self.announcement_thread.is_alive())
            playing = self.controller is not None and self.tv_state in {"starting", "playing"}
        if not playing or not channel or channel == "__guide__":
            self._set_announcement_status(None, "Tune to a playing channel", "idle")
            return
        with connect_database(self.config.database_path) as connection:
            row = connection.execute(
                """
                SELECT ao.*, sg.status AS generation_status
                FROM announcement_opportunities ao
                JOIN schedule_generations sg ON sg.id=ao.generation_id
                JOIN channels c ON c.id=ao.channel_id
                WHERE c.slug=? AND sg.status='active' AND ao.starts_at>?
                ORDER BY ao.starts_at LIMIT 1
                """,
                (channel, now.isoformat(timespec="milliseconds")),
            ).fetchone()
            if row is not None:
                from retrotv.announcements import UpNextService

                UpNextService(connection, self.config).refresh_opportunity_facts(
                    int(row["id"])
                )
                row = connection.execute(
                    """
                    SELECT ao.*, sg.status AS generation_status
                    FROM announcement_opportunities ao
                    JOIN schedule_generations sg ON sg.id=ao.generation_id
                    WHERE ao.id=?
                    """,
                    (int(row["id"]),),
                ).fetchone()
                if row is not None and not row["validated_output_path"]:
                    cached_job = connection.execute(
                        """
                        SELECT output_path FROM announcement_jobs
                        WHERE opportunity_id=? AND cache_key=? AND status='ready'
                          AND output_path IS NOT NULL
                        ORDER BY completed_at DESC LIMIT 1
                        """,
                        (int(row["id"]), row["cache_key"]),
                    ).fetchone()
                    if cached_job is not None and Path(cached_job["output_path"]).is_file():
                        connection.execute(
                            "UPDATE announcement_opportunities SET validated_output_path=?, render_status='ready', detail=NULL, updated_at=? WHERE id=?",
                            (
                                cached_job["output_path"],
                                now.isoformat(timespec="seconds"),
                                int(row["id"]),
                            ),
                        )
                        connection.commit()
                        row = connection.execute(
                            "SELECT ao.*, sg.status AS generation_status FROM announcement_opportunities ao JOIN schedule_generations sg ON sg.id=ao.generation_id WHERE ao.id=?",
                            (int(row["id"]),),
                        ).fetchone()
        if row is None:
            self._set_announcement_status(None, "No upcoming opportunity", "idle")
            return
        starts = datetime.fromisoformat(row["starts_at"])
        lead = (starts - now).total_seconds()
        summary = {
            "id": int(row["id"]),
            "starts_at": row["starts_at"],
            "seconds_away": round(lead, 1),
            "channel": channel,
        }
        dwell_required = float(settings.get("dwell_seconds", 60))
        activity_window = float(settings.get("activity_window_minutes", 120)) * 60
        minimum_lead = float(settings.get("minimum_lead_minutes", 4)) * 60
        maximum_lead = float(settings.get("maximum_lead_minutes", 10)) * 60
        tts_policy = self._automatic_tts_policy(
            channel, connection, opportunity_id=int(row["id"])
        )
        fallback_ready = bool(
            row["fallback_path"] and Path(row["fallback_path"]).is_file()
        )
        if lead <= maximum_lead and not fallback_ready and not busy:
            thread = threading.Thread(
                target=self._silent_announcement_worker,
                args=(int(row["id"]),),
                name="retrotv-silent-announcement",
                daemon=True,
            )
            with self.lock:
                self.announcement_thread = thread
                self.announcement_state.update(
                    {
                        "next_opportunity": summary,
                        "eligibility_reason": "Preparing mandatory silent fallback",
                        "generation_state": "rendering-fallback",
                    }
                )
            thread.start()
            return
        if tts_policy == "disabled":
            reason = "Automatic TTS is disabled or unavailable"
        else:
            reason = self.announcement_eligibility_reason(
                now, tuned_since, activity, lead,
                dwell_required=dwell_required,
                activity_window_seconds=activity_window,
                minimum_lead_seconds=minimum_lead,
                maximum_lead_seconds=maximum_lead,
                local_tts=False,
            )
        if reason == "eligible" and row["validated_output_path"] and Path(row["validated_output_path"]).is_file():
            reason = "Matching narrated result is already cached"
        elif reason == "eligible" and busy:
            reason = "Another automatic generation job is running"
        self._set_announcement_status(
            summary, reason, "running" if busy else "idle"
        )
        if reason != "eligible":
            return
        thread = threading.Thread(
            target=self._announcement_worker,
            args=(int(row["id"]), int(row["generation_id"]), channel),
            name="retrotv-announcement-generation",
            daemon=True,
        )
        with self.lock:
            if self.announcement_thread and self.announcement_thread.is_alive():
                return
            self.announcement_thread = thread
            self.announcement_state["generation_state"] = "queued"
        thread.start()

    @staticmethod
    def announcement_eligibility_reason(
        now: datetime,
        tuned_since: datetime | None,
        activity: datetime | None,
        lead_seconds: float,
        *,
        dwell_required: float = 60,
        activity_window_seconds: float = 120 * 60,
        minimum_lead_seconds: float = 4 * 60,
        maximum_lead_seconds: float = 10 * 60,
        local_tts: bool = False,
        local_minimum_lead_seconds: float = 15,
        local_maximum_lead_seconds: float = 120,
    ) -> str:
        if activity is None or (now - activity).total_seconds() > activity_window_seconds:
            return "No recent playback-related human activity"
        if local_tts:
            if lead_seconds <= local_minimum_lead_seconds:
                return "Opportunity is too close for safe local rendering"
            if lead_seconds > local_maximum_lead_seconds:
                return "Local opportunity is more than two minutes away"
            return "eligible"
        if tuned_since is None or (now - tuned_since).total_seconds() < dwell_required:
            return f"Channel dwell is under {dwell_required:g} seconds"
        if lead_seconds <= minimum_lead_seconds:
            return "Opportunity is four minutes away or less"
        if lead_seconds > maximum_lead_seconds:
            return "Opportunity is more than ten minutes away"
        return "eligible"

    def _automatic_tts_policy(
        self, channel: str, connection, *, opportunity_id: int | None = None
    ) -> str:
        configured = str(
            self.config.data.get("announcements", {}).get("tts_provider", "openai")
        ).casefold()
        if configured in {"none", "off", "disabled"}:
            return "disabled"
        from retrotv.ai_announcements import configured_openai_api_key
        if configured not in {"auto", "openai", "cloud"} or not configured_openai_api_key():
            return "disabled"
        from retrotv.ai_announcements import AnnouncementProfileService
        try:
            daypart_slug = None
            weekly_collection_slug = None
            if opportunity_id is not None:
                from retrotv.announcements import UpNextService
                plan = UpNextService(
                    connection, self.config
                ).build_opportunity_plan(opportunity_id)
                daypart_slug = plan.daypart_slug
                weekly_collection_slug = plan.weekly_collection_slug
            profiles = AnnouncementProfileService(connection, self.config)
            if opportunity_id is not None and plan.weekly_special_promo:
                weekly = profiles.scoped_profile(
                    channel, weekly_collection_slug=weekly_collection_slug
                )
                if not weekly.promo_narration_enabled:
                    return "disabled"
            profile = profiles.profile(channel, daypart_slug, weekly_collection_slug)
            from retrotv.tts import profile_tts_ready
            if not profile.enabled or not profile_tts_ready(profile):
                return "disabled"
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            return "disabled"
        return "cloud"

    def _silent_announcement_worker(self, opportunity_id: int) -> None:
        connection = connect_database(self.config.database_path)
        try:
            UpNextRenderer(connection, self.config).render_opportunity(
                opportunity_id, use_tts=False
            )
            with self.lock:
                self.announcement_state["generation_state"] = "fallback-ready"
                self.announcement_state["error"] = None
        except Exception as exc:
            with self.lock:
                self.announcement_state["generation_state"] = "failed"
                self.announcement_state["error"] = str(exc)
        finally:
            connection.close()
            self._release_announcement_worker_slot()

    def _set_announcement_status(
        self, opportunity: dict[str, Any] | None, reason: str, state: str
    ) -> None:
        with self.lock:
            self.announcement_state["next_opportunity"] = opportunity
            self.announcement_state["eligibility_reason"] = reason
            if self.announcement_state["generation_state"] not in {"queued", "running"}:
                self.announcement_state["generation_state"] = state

    def _announcement_worker(
        self, opportunity_id: int, generation_id: int, channel: str
    ) -> None:
        connection = connect_database(self.config.database_path)
        job_id = None
        try:
            row = connection.execute(
                "SELECT cache_key, starts_at FROM announcement_opportunities WHERE id=?",
                (opportunity_id,),
            ).fetchone()
            if row is None:
                return
            now = datetime.now(self.timezone)
            with self.lock:
                still_tuned = self.requested_channel == channel
            status = "queued" if still_tuned else "cancelled"
            cursor = connection.execute(
                """
                INSERT INTO announcement_jobs(
                    opportunity_id, cache_key, provider, status, requested_at
                ) VALUES (?, ?, 'automatic', ?, ?)
                """,
                (opportunity_id, row["cache_key"], status, now.isoformat(timespec="seconds")),
            )
            job_id = int(cursor.lastrowid)
            connection.commit()
            with self.lock:
                self.announcement_state["job_id"] = job_id
            if not still_tuned:
                return
            connection.execute(
                "UPDATE announcement_jobs SET status='running', started_at=? WHERE id=?",
                (now.isoformat(timespec="seconds"), job_id),
            )
            connection.commit()
            with self.lock:
                self.announcement_state["generation_state"] = "running"
            result = UpNextRenderer(connection, self.config).render_opportunity(
                opportunity_id, use_tts=True, activate=False
            )
            current = connection.execute(
                "SELECT status FROM schedule_generations WHERE id=?", (generation_id,)
            ).fetchone()
            deadline_missed = datetime.now(self.timezone) >= datetime.fromisoformat(row["starts_at"])
            with self.lock:
                still_tuned = self.requested_channel == channel
            if current is None or current["status"] != "active" or deadline_missed:
                final = "stale"
                result.output.unlink(missing_ok=True)
                connection.execute(
                    "UPDATE announcement_opportunities SET validated_output_path=NULL, render_status='expired', detail='Schedule changed or playback deadline passed' WHERE id=?",
                    (opportunity_id,),
                )
            elif not still_tuned:
                # Keep the valid cache. Playback may only activate it if the
                # viewer returns while this same schedule is still active.
                final = "ready"
            else:
                final = "ready"
            if final == "ready" and still_tuned:
                connection.execute(
                    """
                    UPDATE announcement_opportunities
                    SET validated_output_path=?, render_status='ready',
                        detail=NULL, updated_at=? WHERE id=?
                    """,
                    (
                        str(result.output),
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        opportunity_id,
                    ),
                )
            connection.execute(
                "UPDATE announcement_jobs SET status=?, output_path=?, completed_at=? WHERE id=?",
                (final, str(result.output), datetime.now().astimezone().isoformat(timespec="seconds"), job_id),
            )
            connection.commit()
            with self.lock:
                self.announcement_state["generation_state"] = final
                self.announcement_state["error"] = None
        except Exception as exc:
            if job_id is not None:
                connection.execute(
                    "UPDATE announcement_jobs SET status='failed', detail=?, completed_at=? WHERE id=?",
                    (str(exc), datetime.now().astimezone().isoformat(timespec="seconds"), job_id),
                )
                connection.commit()
            with self.lock:
                self.announcement_state["generation_state"] = "failed"
                self.announcement_state["error"] = str(exc)
        finally:
            connection.close()
            self._release_announcement_worker_slot()

    def _release_announcement_worker_slot(self) -> None:
        current = threading.current_thread()
        with self.lock:
            if self.announcement_thread is current:
                self.announcement_thread = None

    def _refresh_guide_if_needed(self, force: bool = False) -> bool:
        if not self.guide_lock.acquire(blocking=False):
            return False
        try:
            for attempt in range(3):
                connection = None
                try:
                    connection = connect_database(self.config.database_path)
                    renderer = GuideRenderer(connection, self.config)
                    if not force and not renderer.needs_refresh():
                        return False
                    result = renderer.render(versioned=True)
                    # Music can make the rendered file several visual cycles long.
                    # Retune at the next short visual seam so fresh half-hour listings
                    # are not delayed until the selected song ends.
                    self._schedule_guide_reload(
                        float(
                            result.get(
                                "cycle_duration_seconds", result["duration_seconds"]
                            )
                        )
                    )
                    with self.lock:
                        self.maintenance["guide_rendered"] = True
                        if self.maintenance["state"] != "error":
                            self.maintenance["error"] = None
                    return True
                except sqlite3.OperationalError as exc:
                    if ("locked" not in str(exc).lower() and "busy" not in str(exc).lower()) or attempt == 2:
                        raise
                    time.sleep(0.25 * (attempt + 1))
                finally:
                    if connection is not None:
                        connection.close()
        except Exception as exc:
            with self.lock:
                self.maintenance["error"] = f"Guide refresh failed: {exc}"
            return False
        finally:
            self.guide_lock.release()

    def _schedule_guide_reload(self, loop_duration: float) -> None:
        with self.lock:
            controller = self.controller
            current = controller.current if controller else None
            if (
                controller is None
                or current is None
                or current.entry_type != "guide"
                or self.requested_channel != "__guide__"
            ):
                return
            now = datetime.now(controller.resolver.scheduler.timezone)
            elapsed = max(0.0, (now - current.starts_at).total_seconds())
            position = (current.media_offset_seconds + elapsed) % loop_duration
            delay = loop_duration - position
            if delay < 0.05:
                delay = loop_duration
            self.guide_reload_token += 1
            token = self.guide_reload_token

        def reload_at_boundary() -> None:
            with self.lock:
                if (
                    token != self.guide_reload_token
                    or self.controller is not controller
                    or self.requested_channel != "__guide__"
                ):
                    return
                controller.request_retune()

        timer = threading.Timer(delay, reload_at_boundary)
        timer.daemon = True
        timer.start()

    def _maintenance_worker(self) -> None:
        started = datetime.now().astimezone()
        with self.lock:
            self.maintenance.update(
                {
                    "state": "running",
                    "stage": "database backup",
                    "failed_stage": None,
                    "started_at": started.isoformat(timespec="seconds"),
                    "completed_at": None,
                    "error": None,
                    "scan": None,
                    "duplicates": None,
                    "normalization": None,
                    "commercial_tails": None,
                    "breakpoints": None,
                    "audio": None,
                    "schedules_created": 0,
                    "guide_rendered": False,
                    "generated_cleanup": None,
                    "database_backup": None,
                    "database_housekeeping": None,
                }
            )
        connection = connect_database(self.config.database_path)
        try:
            backup_result = self._perform_database_backup(connection, force=False)
            with self.lock:
                self.maintenance["database_backup"] = backup_result
                self.maintenance["stage"] = "database housekeeping"
            housekeeping = DatabaseHousekeepingService(connection, self.config).run(
                force=False,
                trigger=str(self.readiness.get("trigger") or "maintenance"),
            ).as_dict()
            with self.lock:
                self.database_housekeeping.update(housekeeping)
                self.maintenance["database_housekeeping"] = housekeeping
                self.maintenance["stage"] = "scan"
            if self._maintenance_should_stop():
                return
            scan = LibraryScanner(connection, self.config).scan(
                cancel_requested=self._maintenance_should_stop
            )
            from retrotv.external_media import ExternalMediaService
            class ScanCancellation:
                def is_set(inner):
                    return self._maintenance_should_stop()
            external_scans = ExternalMediaService(connection, self.config).scan_all(cancel_event=ScanCancellation())
            with self.lock:
                self.maintenance['external_scans'] = external_scans
                self.maintenance["scan"] = {
                    "discovered": scan.discovered,
                    "new_or_changed_ready": scan.ready,
                    "unchanged": scan.unchanged,
                    "review": scan.review,
                    "errors": scan.errors,
                    "missing": scan.missing,
                    "channels_created": scan.channels_created,
                    "channels_enabled": scan.channels_enabled,
                    "channels_disabled": scan.channels_disabled,
                    "shadows_ready": scan.shadows_ready,
                    "shadows_review": scan.shadows_review,
                    "shadows_removed": scan.shadows_removed,
                }
                self.maintenance["stage"] = "commercial black tails"
            if self._maintenance_should_stop():
                return
            tails = {"analyzed": 0, "trimmed": 0, "skipped": 0, "failed": 0}
            tail_service = CommercialTailService(connection, self.config)
            for row in tail_service.pending_assets(limit=25):
                if self._maintenance_should_stop():
                    break
                tails["analyzed"] += 1
                try:
                    outcome = tail_service.analyze_asset(int(row["id"]))
                    tails[outcome] += 1
                except Exception:
                    tails["failed"] += 1
            with self.lock:
                self.maintenance["commercial_tails"] = tails
                self.maintenance["stage"] = "exact duplicate hashes"
            if self._maintenance_should_stop():
                return
            duplicate_settings = self.config.data.get("duplicate_detection", {})
            duplicates = DuplicateService(connection, self.config)
            duplicate_result = duplicates.hash_pending(
                limit=max(
                    1,
                    int(duplicate_settings.get("hash_limit_per_maintenance", 25)),
                )
            )
            with self.lock:
                self.maintenance["duplicates"] = {
                    "processed": duplicate_result.processed,
                    "hashed": duplicate_result.hashed,
                    "reused_analysis": duplicate_result.reused_analysis,
                    "errors": duplicate_result.errors,
                }
                self.maintenance["stage"] = "normalization"
            normalization = {
                "processed": 0,
                "analyzed": 0,
                "review": 0,
                "errors": 0,
                "preview_candidates": 0,
                "previews_created": 0,
                "preview_errors": 0,
            }
            service = NormalizationService(
                connection, self.config, cancel_event=self.maintenance_cancel_event
            )
            if enabled(self.config, "normalization") and bool(self.settings.get("analyze_new_media", True)):
                pending = connection.execute(
                    """
                    SELECT n.asset_id
                    FROM normalization_profiles n
                    JOIN assets a ON a.id = n.asset_id
                    WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND n.analysis_status = 'pending'
                      AND a.metadata_status = 'ready'
                      AND a.integrity_status IN ('ready', 'not_scanned')
                    ORDER BY n.asset_id
                    """
                ).fetchall()
                for row in pending:
                    if self._maintenance_should_stop():
                        break
                    normalization["processed"] += 1
                    try:
                        asset_id = int(row["asset_id"])
                        reused = duplicates.apply_normalization_recipe(asset_id)
                        if reused:
                            stored = connection.execute(
                                "SELECT analysis_status FROM normalization_profiles WHERE asset_id=?",
                                (asset_id,),
                            ).fetchone()
                            status = str(stored["analysis_status"])
                        else:
                            status = service.analyze_asset(asset_id).status
                        if status == "analyzed":
                            normalization["analyzed"] += 1
                        else:
                            normalization["review"] += 1
                    except ProcessCancelled:
                        break
                    except Exception:
                        normalization["errors"] += 1
            with self.lock:
                self.maintenance["stage"] = "aspect review previews"
            review_rows = connection.execute(
                """
                SELECT n.asset_id, n.source_preview_relative_path
                FROM normalization_profiles n
                JOIN assets a ON a.id=n.asset_id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND n.analysis_status='review'
                  AND a.metadata_status='ready'
                  AND a.integrity_status IN ('ready', 'not_scanned')
                ORDER BY n.asset_id
                """
            ).fetchall()
            normalization["preview_candidates"] = len(review_rows)
            preview_root = self.config.storage_root("previews")
            for row in (review_rows if enabled(self.config, "normalization") else []):
                if self._maintenance_should_stop():
                    break
                relative = row["source_preview_relative_path"]
                if relative and (preview_root / Path(str(relative))).is_file():
                    continue
                try:
                    service.create_source_preview(int(row["asset_id"]))
                    normalization["previews_created"] += 1
                except ProcessCancelled:
                    break
                except Exception:
                    normalization["preview_errors"] += 1
            with self.lock:
                self.maintenance["normalization"] = normalization
                self.maintenance["stage"] = "breakpoints"
            if self._maintenance_should_stop():
                return
            breakpoints = {"processed": 0, "natural": 0, "midpoint": 0, "errors": 0}
            if bool(self.settings.get("analyze_breakpoints", True)):
                analyzer = BreakpointAnalyzer(
                    connection, self.config, cancel_event=self.maintenance_cancel_event
                )
                analysis_limit = max(
                    1,
                    int(
                        self.settings.get(
                            "breakpoint_analysis_limit_per_maintenance", 25
                        )
                    ),
                )
                for row in analyzer.pending_assets(limit=analysis_limit):
                    if self._maintenance_should_stop():
                        break
                    breakpoints["processed"] += 1
                    try:
                        result = analyzer.analyze_asset(int(row["id"]))
                        if result.status == "error":
                            breakpoints["errors"] += 1
                        elif connection.execute(
                            "SELECT 1 FROM asset_break_candidates WHERE asset_id=? AND selected=1 AND method='midpoint' LIMIT 1",
                            (result.asset_id,),
                        ).fetchone():
                            breakpoints["midpoint"] += 1
                        else:
                            breakpoints["natural"] += 1
                    except ProcessCancelled:
                        break
                    except Exception:
                        breakpoints["errors"] += 1
            with self.lock:
                self.maintenance["breakpoints"] = breakpoints
                self.maintenance["stage"] = "audio loudness"
            if self._maintenance_should_stop():
                return
            audio = {
                "processed": 0,
                "analyzed": 0,
                "review": 0,
                "not_applicable": 0,
                "correction_needed": 0,
                "errors": 0,
            }
            audio_settings = self.config.data.get("audio_normalization", {})
            if bool(audio_settings.get("analyze_during_maintenance", True)):
                service = AudioNormalizationService(
                    connection, self.config, cancel_event=self.maintenance_cancel_event
                )
                limit = max(
                    1, int(audio_settings.get("analysis_limit_per_maintenance", 2))
                )
                for row in service.pending_assets(limit=limit):
                    if self._maintenance_should_stop():
                        break
                    audio["processed"] += 1
                    try:
                        result = service.analyze_asset(int(row["id"]))
                        if result.status == "error":
                            audio["errors"] += 1
                        else:
                            audio[result.status] = audio.get(result.status, 0) + 1
                            audio["correction_needed"] += int(
                                result.needs_correction
                            )
                    except ProcessCancelled:
                        break
                    except Exception:
                        audio["errors"] += 1
            with self.lock:
                self.maintenance["audio"] = audio
                self.maintenance["stage"] = "schedules"
            if self._maintenance_should_stop():
                return
            scheduler = ScheduleBuilder(connection, self.config)
            horizon = max(
                1,
                int(
                    self.settings.get(
                        "schedule_horizon_days",
                        self.config.data.get("scheduling", {}).get(
                            "horizon_days", 7
                        ),
                    )
                ),
            )
            today = datetime.now(scheduler.timezone).date()
            created = 0
            channel_slugs = [
                item["slug"]
                for item in self.channels()
                if not item["is_guide"] and not item.get("is_synthetic", False)
            ]
            for offset in range(horizon):
                if self._maintenance_should_stop():
                    break
                target = today + timedelta(days=offset)
                for slug in channel_slugs:
                    if self._maintenance_should_stop():
                        break
                    if not scheduler.generation_is_current(str(slug), target):
                        scheduler.generate_day(str(slug), target)
                        created += 1
            with self.lock:
                self.maintenance["schedules_created"] = created
                self.maintenance["stage"] = "guide"
            if self._maintenance_should_stop():
                return
            rendered = False
            with self.guide_lock:
                renderer = GuideRenderer(connection, self.config)
                if renderer.needs_refresh():
                    result = renderer.render(versioned=True)
                    rendered = True
                else:
                    result = None
            if result is not None:
                self._schedule_guide_reload(float(result["duration_seconds"]))
            cleanup_result = None
            if bool(
                self.config.data.get("generated_cleanup", {}).get(
                    "run_during_maintenance", True
                )
            ):
                with self.lock:
                    self.maintenance["stage"] = "generated cleanup"
                cleanup_result = self._perform_generated_cleanup(
                    connection, force=False
                )
            with self.lock:
                self.maintenance["guide_rendered"] = rendered
                self.maintenance["generated_cleanup"] = cleanup_result
                self.maintenance["state"] = (
                    "deferred" if self._maintenance_should_stop() else "idle"
                )
                self.maintenance["stage"] = None
        except Exception as exc:
            with self.lock:
                self.maintenance["state"] = "error"
                self.maintenance["error"] = str(exc)
                self.maintenance["failed_stage"] = self.maintenance.get("stage")
                self.maintenance["stage"] = None
        finally:
            connection.close()
            self._refresh_pending_actions()
            with self.lock:
                if self.maintenance["state"] == "running" and self._maintenance_should_stop():
                    self.maintenance["state"] = "deferred"
                    self.maintenance["stage"] = None
                self.maintenance["completed_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )

    def _maintenance_should_stop(self) -> bool:
        return self.stop_event.is_set() or self.maintenance_cancel_event.is_set()

    def _generated_cleanup_worker(self) -> None:
        connection = connect_database(self.config.database_path)
        try:
            try:
                self._perform_generated_cleanup(connection, force=True)
            except Exception:
                # _perform_generated_cleanup records the actionable error in
                # runtime status for the Dashboard.
                pass
        finally:
            connection.close()

    def _database_backup_worker(self) -> None:
        connection = connect_database(self.config.database_path)
        try:
            try:
                self._perform_database_backup(connection, force=True)
            except Exception:
                # _perform_database_backup records the actionable Dashboard error.
                pass
        finally:
            connection.close()

    def _database_housekeeping_worker(self, *, force: bool, trigger: str) -> None:
        with self.exclusive_operation_lock:
            connection = connect_database(self.config.database_path)
            try:
                with self.lock:
                    self.database_housekeeping.update(
                        {
                            "state": "running", "trigger": trigger,
                            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "completed_at": None, "error": None,
                        }
                    )
                result = DatabaseHousekeepingService(connection, self.config).run(
                    force=force, trigger=trigger
                ).as_dict()
                with self.lock:
                    self.database_housekeeping.update(result)
            finally:
                connection.close()

    def _database_compaction_worker(self) -> None:
        with self.exclusive_operation_lock:
            self.internet_pause_event.set()
            try:
                with self.lock:
                    self.database_compaction.update(
                        {
                            "state": "running",
                            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "completed_at": None,
                            "error": None,
                        }
                    )
                result = DatabaseCompactionService(self.config).run(trigger="manual")
                with self.lock:
                    self.database_compaction.update(result)
            finally:
                self.internet_pause_event.clear()

    def _internet_cache_cleanup_worker(self) -> None:
        import json

        with self.exclusive_operation_lock:
            self.internet_pause_event.set()
            started = datetime.now().astimezone().isoformat(timespec="seconds")
            with self.lock:
                self.cache_cleanup.update(
                    {
                        "state": "running", "started_at": started,
                        "completed_at": None, "files_removed": 0,
                        "bytes_removed": 0, "errors": [],
                    }
                )
            connection = connect_database(self.config.database_path)
            try:
                result = PreparationService(connection, self.config).empty_cache(
                    active_path=self._active_media_path()
                )
                completed = datetime.now().astimezone().isoformat(timespec="seconds")
                payload = {
                    "state": "complete" if not result["errors"] else "error",
                    "completed_at": completed,
                    "files_removed": int(result["files_removed"]),
                    "bytes_removed": int(result["bytes_removed"]),
                    "errors": list(result["errors"]),
                }
                connection.execute(
                    """INSERT INTO maintenance_runs(
                           run_type,trigger_kind,state,started_at,completed_at,detail_json,error
                       ) VALUES ('cache_cleanup','manual',?,?,?,?,?)""",
                    (
                        "complete" if not result["errors"] else "failed",
                        started, completed, json.dumps(payload, sort_keys=True),
                        "; ".join(result["errors"]) or None,
                    ),
                )
                connection.commit()
                with self.lock:
                    self.cache_cleanup.update(payload)
            except Exception as exc:
                with self.lock:
                    self.cache_cleanup.update(
                        {
                            "state": "error",
                            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "errors": [str(exc)],
                        }
                    )
            finally:
                connection.close()
                self.internet_pause_event.clear()

    def _perform_database_backup(
        self, connection: sqlite3.Connection, *, force: bool
    ) -> dict[str, Any]:
        with self.database_backup_lock:
            started = datetime.now().astimezone()
            with self.lock:
                self.database_backup.update(
                    {
                        "state": "running",
                        "started_at": started.isoformat(timespec="seconds"),
                        "completed_at": None,
                        "created": False,
                        "skipped": False,
                        "reason": None,
                        "error": None,
                        "removed_count": 0,
                    }
                )
            try:
                service = DatabaseBackupService(self.config)
                result = service.create(connection, force=force)
                inventory = service.inventory()
                payload = result.as_dict()
                with self.lock:
                    self.database_backup.update(
                        {
                            **inventory,
                            **payload,
                            "state": "completed",
                            "latest_path": payload["path"] or inventory["latest_path"],
                            "latest_size_bytes": (
                                payload["size_bytes"]
                                if payload["path"]
                                else inventory["latest_size_bytes"]
                            ),
                            "error": None,
                        }
                    )
                return payload
            except Exception as exc:
                with self.lock:
                    self.database_backup.update(
                        {
                            "state": "error",
                            "completed_at": datetime.now()
                            .astimezone()
                            .isoformat(timespec="seconds"),
                            "error": str(exc),
                        }
                    )
                raise

    def _perform_generated_cleanup(self, connection, *, force: bool) -> dict[str, Any]:
        started = datetime.now().astimezone()
        with self.generated_cleanup_lock:
            with self.lock:
                self.generated_cleanup.update(
                    {
                        "state": "running",
                        "started_at": started.isoformat(timespec="seconds"),
                        "completed_at": None,
                        "files_removed": 0,
                        "bytes_removed": 0,
                        "files_examined": 0,
                        "skipped": False,
                        "reason": None,
                        "errors": [],
                    }
                )
            try:
                summary = GeneratedAssetCleanup(connection, self.config).run(
                    force=force
                ).as_dict()
                completed = datetime.now().astimezone().isoformat(timespec="seconds")
                with self.lock:
                    self.generated_cleanup.update(
                        {
                            **summary,
                            "state": "error" if summary["errors"] else "idle",
                            "completed_at": completed,
                        }
                    )
                return summary
            except Exception as exc:
                completed = datetime.now().astimezone().isoformat(timespec="seconds")
                with self.lock:
                    self.generated_cleanup.update(
                        {
                            "state": "error",
                            "completed_at": completed,
                            "errors": [str(exc)],
                        }
                    )
                raise

    def _audio_analysis_worker(self, *, idle_only: bool = False) -> None:
        if not enabled(self.config, "audio_normalization"):
            return
        started = datetime.now().astimezone()
        with self.lock:
            self.audio_analysis_batch.update(
                {
                    **self._new_audio_batch_state(),
                    "state": "running",
                    "started_at": started.isoformat(timespec="seconds"),
                }
            )
        connection = connect_database(self.config.database_path)
        try:
            service = AudioNormalizationService(
                connection,
                self.config,
                cancel_event=self.maintenance_cancel_event if idle_only else None,
            )
            pending = service.pending_assets()
            with self.lock:
                self.audio_analysis_batch["total"] = len(pending)
            for row in pending:
                if self.stop_event.is_set():
                    raise RuntimeError("CRTV Sim stopped before audio analysis completed")
                if idle_only and (
                    not self._tv_is_idle() or self.maintenance_cancel_event.is_set()
                ):
                    break
                asset_id = int(row["id"])
                path = str(row["source_relative_path"])
                with self.lock:
                    self.audio_analysis_batch["current_asset_id"] = asset_id
                    self.audio_analysis_batch["current_path"] = path
                try:
                    result = service.analyze_asset(asset_id)
                    with self.lock:
                        if result.status == "error":
                            self.audio_analysis_batch["failed"] += 1
                        else:
                            key = (
                                result.status
                                if result.status in {"analyzed", "review", "not_applicable"}
                                else "review"
                            )
                            self.audio_analysis_batch[key] += 1
                            self.audio_analysis_batch["correction_needed"] += int(
                                result.needs_correction
                            )
                except ProcessCancelled:
                    break
                except Exception as exc:
                    with self.lock:
                        self.audio_analysis_batch["failed"] += 1
                        failures = self.audio_analysis_batch["failures"]
                        failures.append({"asset_id": asset_id, "path": path, "error": str(exc)})
                        del failures[:-10]
                finally:
                    with self.lock:
                        self.audio_analysis_batch["processed"] += 1
            with self.lock:
                self.audio_analysis_batch["state"] = (
                    "deferred"
                    if idle_only and self.maintenance_cancel_event.is_set()
                    else "completed"
                )
        except Exception as exc:
            with self.lock:
                self.audio_analysis_batch["state"] = "error"
                self.audio_analysis_batch["error"] = str(exc)
        finally:
            connection.close()
            self._refresh_pending_actions()
            with self.lock:
                self.audio_analysis_batch["current_asset_id"] = None
                self.audio_analysis_batch["current_path"] = None
                self.audio_analysis_batch["completed_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )

    def _audio_normalization_worker(self, *, idle_only: bool = False) -> None:
        if not enabled(self.config, "audio_normalization"):
            return
        started = datetime.now().astimezone()
        with self.lock:
            self.audio_normalization_batch.update(
                {
                    **self._new_audio_batch_state(),
                    "state": "running",
                    "started_at": started.isoformat(timespec="seconds"),
                }
            )
        connection = connect_database(self.config.database_path)
        try:
            service = AudioNormalizationService(
                connection,
                self.config,
                cancel_event=self.maintenance_cancel_event if idle_only else None,
            )
            pending = service.correction_candidates()
            with self.lock:
                self.audio_normalization_batch["total"] = len(pending)
                self.audio_normalization_batch["correction_needed"] = len(pending)
            for row in pending:
                if self.stop_event.is_set():
                    raise RuntimeError("CRTV Sim stopped before audio normalization completed")
                if idle_only and (
                    not self._tv_is_idle() or self.maintenance_cancel_event.is_set()
                ):
                    break
                asset_id = int(row["id"])
                path = str(row["source_relative_path"])
                with self.lock:
                    self.audio_normalization_batch["current_asset_id"] = asset_id
                    self.audio_normalization_batch["current_path"] = path
                try:
                    service.normalize_asset(asset_id)
                    with self.lock:
                        self.audio_normalization_batch["normalized"] += 1
                except ProcessCancelled:
                    break
                except Exception as exc:
                    with self.lock:
                        self.audio_normalization_batch["failed"] += 1
                        failures = self.audio_normalization_batch["failures"]
                        failures.append({"asset_id": asset_id, "path": path, "error": str(exc)})
                        del failures[:-10]
                finally:
                    with self.lock:
                        self.audio_normalization_batch["processed"] += 1
            with self.lock:
                self.audio_normalization_batch["state"] = (
                    "deferred"
                    if idle_only and self.maintenance_cancel_event.is_set()
                    else "completed"
                )
        except Exception as exc:
            with self.lock:
                self.audio_normalization_batch["state"] = "error"
                self.audio_normalization_batch["error"] = str(exc)
        finally:
            connection.close()
            self._refresh_pending_actions()
            with self.lock:
                self.audio_normalization_batch["current_asset_id"] = None
                self.audio_normalization_batch["current_path"] = None
                self.audio_normalization_batch["completed_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )

    def _normalization_batch_worker(self, *, idle_only: bool = False) -> None:
        if not enabled(self.config, "normalization"):
            return
        started = datetime.now().astimezone()
        policy = str(
            self.config.data.get("normalization", {}).get(
                "output_policy", "proxy"
            )
        )
        with self.lock:
            self.normalization_batch.update(
                {
                    "state": "running",
                    "policy": policy,
                    "total": 0,
                    "processed": 0,
                    "converted": 0,
                    "reused": 0,
                    "skipped": 0,
                    "not_needed": 0,
                    "failed": 0,
                    "current_asset_id": None,
                    "current_path": None,
                    "started_at": started.isoformat(timespec="seconds"),
                    "completed_at": None,
                    "error": None,
                    "failures": [],
                }
            )

        connection = connect_database(self.config.database_path)
        try:
            approved = connection.execute(
                """
                SELECT a.id, a.source_relative_path, m.width, m.height,
                       n.*
                FROM assets a
                JOIN normalization_profiles n ON n.asset_id = a.id
                JOIN asset_media_info m ON m.asset_id = a.id
                WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND a.review_status = 'ready'
                  AND a.metadata_status = 'ready'
                  AND a.integrity_status = 'ready'
                  AND n.analysis_status = 'analyzed'
                ORDER BY a.id
                """
            ).fetchall()
            normalizer = NormalizationService(
                connection,
                self.config,
                cancel_event=self.maintenance_cancel_event if idle_only else None,
            )
            correctable = [
                row
                for row in approved
                if normalizer.requires_conversion(row)
            ]
            if policy == "replace_with_backup":
                pending = [
                    row
                    for row in correctable
                    if row["proxy_status"] != "promoted"
                ]
            else:
                pending = [
                    row
                    for row in correctable
                    if row["proxy_status"] not in {"ready", "promoted"}
                ]

            with self.lock:
                self.normalization_batch["total"] = len(pending)
                self.normalization_batch["skipped"] = (
                    len(correctable) - len(pending)
                )
                self.normalization_batch["not_needed"] = (
                    len(approved) - len(correctable)
                )

            promoter = PromotionService(connection, self.config)
            for row in pending:
                if self.stop_event.is_set():
                    raise RuntimeError("CRTV Sim stopped before conversion completed")
                if idle_only and (
                    not self._tv_is_idle() or self.maintenance_cancel_event.is_set()
                ):
                    break
                asset_id = int(row["id"])
                relative_path = str(row["source_relative_path"])
                with self.lock:
                    self.normalization_batch["current_asset_id"] = asset_id
                    self.normalization_batch["current_path"] = relative_path
                try:
                    if policy == "replace_with_backup":
                        donor_id = DuplicateService(
                            connection, self.config
                        ).prepare_reusable_proxy(asset_id)
                        promoter.replace_with_backup(asset_id)
                    else:
                        donor_id = None
                        result = normalizer.create_proxy(asset_id)
                        if not result.ok:
                            raise RuntimeError(result.error or "Correction failed")
                except ProcessCancelled:
                    break
                except Exception as exc:
                    with self.lock:
                        self.normalization_batch["failed"] += 1
                        failures = self.normalization_batch["failures"]
                        failures.append(
                            {
                                "asset_id": asset_id,
                                "path": relative_path,
                                "error": str(exc),
                            }
                        )
                        del failures[:-10]
                else:
                    with self.lock:
                        self.normalization_batch["converted"] += 1
                        self.normalization_batch["reused"] += int(donor_id is not None)
                finally:
                    with self.lock:
                        self.normalization_batch["processed"] += 1

            with self.lock:
                self.normalization_batch["state"] = (
                    "deferred"
                    if idle_only and self.maintenance_cancel_event.is_set()
                    else "completed"
                )
        except Exception as exc:
            with self.lock:
                self.normalization_batch["state"] = "error"
                self.normalization_batch["error"] = str(exc)
        finally:
            connection.close()
            self._refresh_pending_actions()
            with self.lock:
                self.normalization_batch["current_asset_id"] = None
                self.normalization_batch["current_path"] = None
                self.normalization_batch["completed_at"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
