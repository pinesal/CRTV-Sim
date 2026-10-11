from __future__ import annotations

import os
import json
import queue
import shutil
import sqlite3
import socket
import subprocess
import threading
import time as time_module
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from retrotv.config import AppConfig
from retrotv.scheduling import ScheduleBuilder
from retrotv.synthetic import (
    SyntheticChannelError,
    SyntheticChannelReader,
    weather_channel_definition,
    weather_channel_reader,
)


def _focus_windows_process_window(process_id: int) -> bool:
    """Best-effort foreground activation for the player window on Windows.

    Windows may reject focus requests under its focus-stealing protections, so
    this is deliberately advisory. mpv's own focus-on option is also used
    as a second path. Failure here must never prevent playback.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd, _lparam):
            window_process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_process_id))
            if (
                window_process_id.value == process_id
                and user32.IsWindowVisible(hwnd)
                and not user32.GetWindow(hwnd, 4)  # GW_OWNER: skip owned windows
            ):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(collect, 0)
        if not found:
            return False
        hwnd = found[0]
        # SetForegroundWindow can be refused when the Dashboard browser is the
        # active process. Temporarily joining its input queue lets Windows
        # treat this as the user's Start TV action, rather than unsolicited
        # focus stealing. Every call remains best-effort and is detached again
        # immediately.
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        attached_foreground = bool(
            foreground_thread
            and foreground_thread != current_thread
            and user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        attached_target = bool(
            target_thread
            and target_thread != current_thread
            and user32.AttachThreadInput(current_thread, target_thread, True)
        )
        try:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached_target:
                user32.AttachThreadInput(current_thread, target_thread, False)
            if attached_foreground:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
    except Exception:
        return False


@dataclass(frozen=True)
class PlaybackTarget:
    entry_id: int
    asset_id: int | None
    title: str
    entry_type: str
    starts_at: datetime
    ends_at: datetime
    path: Path | None
    representation: str
    media_offset_seconds: float
    remaining_seconds: float
    channel_slug: str | None = None
    generation_id: str | None = None
    segment_id: str | None = None
    loop_duration_seconds: float | None = None
    video_filter: str | None = None
    missing_media: dict | None = None


class PlaybackError(RuntimeError):
    pass


def _open_windows_pipe(path: str, timeout_seconds: float = 10):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    if not kernel32.WaitNamedPipeW(path, 50):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), path)
    handle = create_file(
        path,
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0,
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), path)

    class WindowsPipe:
        def __init__(self, pipe_handle):
            self.handle = pipe_handle
            self.buffer = bytearray()
            self.closed = False

        def write(self, value: bytes) -> int:
            if self.closed:
                raise OSError("Named pipe is closed")
            written = wintypes.DWORD()
            data = ctypes.create_string_buffer(value)
            ok = kernel32.WriteFile(
                self.handle,
                data,
                len(value),
                ctypes.byref(written),
                None,
            )
            if not ok:
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), path)
            return int(written.value)

        def readline(self) -> bytes:
            deadline = time_module.monotonic() + timeout_seconds
            while b"\n" not in self.buffer:
                available = wintypes.DWORD()
                ok = kernel32.PeekNamedPipe(
                    self.handle,
                    None,
                    0,
                    None,
                    ctypes.byref(available),
                    None,
                )
                if not ok:
                    error = ctypes.get_last_error()
                    if error in {109, 232}:
                        return b""
                    raise OSError(error, ctypes.FormatError(error), path)
                if available.value == 0:
                    if time_module.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for mpv IPC data")
                    time_module.sleep(0.01)
                    continue
                chunk = ctypes.create_string_buffer(4096)
                received = wintypes.DWORD()
                ok = kernel32.ReadFile(
                    self.handle,
                    chunk,
                    len(chunk),
                    ctypes.byref(received),
                    None,
                )
                if not ok:
                    error = ctypes.get_last_error()
                    if error in {109, 232}:  # broken/no pipe
                        return b""
                    raise OSError(error, ctypes.FormatError(error), path)
                if received.value == 0:
                    return b""
                self.buffer.extend(chunk.raw[: received.value])
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            return bytes(line) + b"\n"

        def close(self) -> None:
            if not self.closed:
                kernel32.CloseHandle(self.handle)
                self.closed = True

    return WindowsPipe(handle)


def ensure_continuity_slate(config: AppConfig) -> Path:
    state_root = config.storage_root("state")
    target = state_root / "generated" / "continuity-v2.mp4"
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial.mp4")
    args = [
        config.ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "smptebars=size=640x480:rate=30",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        "10",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        os.fspath(partial),
    ]
    completed = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode:
        partial.unlink(missing_ok=True)
        raise PlaybackError(
            f"Unable to generate continuity slate: {completed.stderr[-1000:]}"
        )
    os.replace(partial, target)
    return target


class ScheduleResolver:
    def __init__(
        self,
        connection,
        config: AppConfig,
        *,
        synthetic_readers: Mapping[str, SyntheticChannelReader] | None = None,
        missing_media_handler=None,
    ):
        self.connection = connection
        self.config = config
        self.missing_media_handler = missing_media_handler
        self.scheduler = ScheduleBuilder(connection, config)
        self.synthetic_readers = dict(
            synthetic_readers
            if synthetic_readers is not None
            else self._default_synthetic_readers()
        )

    def _default_synthetic_readers(self) -> dict[str, SyntheticChannelReader]:
        weather = weather_channel_definition(self.config)
        return (
            {weather.slug: weather_channel_reader(self.config)}
            if weather.enabled else {}
        )

    def _is_watch_at_your_pace(self, channel_slug: str) -> bool:
        row = self.connection.execute(
            """SELECT p.playback_mode FROM channels c
               LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id
               WHERE c.slug=?""",
            (channel_slug,),
        ).fetchone()
        return bool(row and row["playback_mode"] == "watch_at_your_pace")

    @staticmethod
    def _watch_video_filter(episode: Mapping[str, object]) -> str | None:
        mode = episode.get("aspect_mode")
        if mode == "stretched_4_3":
            return "lavfi=[setsar=1,setdar=4/3]"
        if mode in {"pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"} and episode.get("crop_json"):
            try:
                crop = json.loads(str(episode["crop_json"]))
                return (
                    f"lavfi=[crop={int(crop['width'])}:{int(crop['height'])}:"
                    f"{int(crop['x'])}:{int(crop['y'])},setsar=1,setdar=4/3]"
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
        return None

    def _resolve_watch_filler(
        self, channel_slug: str, when: datetime, waiting_title: str
    ) -> PlaybackTarget:
        rows = self.connection.execute(
            """SELECT DISTINCT a.id,a.source_relative_path,a.display_title,
                              a.original_filename,m.duration_seconds
               FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
               LEFT JOIN asset_eligibility e ON e.asset_id=a.id AND e.enabled=1
               WHERE a.enabled_for_scheduling=1 AND a.review_status='ready'
                 AND a.integrity_status='ready' AND a.media_type IN ('commercial','filler')
                 AND (a.channel_slug=? OR e.channel_slug=? OR e.channel_slug IS NULL)
               ORDER BY a.id""",
            (channel_slug, channel_slug),
        ).fetchall()
        if rows:
            row = rows[int(when.timestamp()) % len(rows)]
            path = self.config.storage_root("media") / Path(row["source_relative_path"])
            if path.is_file():
                duration = max(1.0, float(row["duration_seconds"]))
                return PlaybackTarget(
                    int(row["id"]),
                    int(row["id"]),
                    row["display_title"] or row["original_filename"],
                    "watch_waiting_filler",
                    when,
                    when + timedelta(seconds=duration),
                    path,
                    "watch_waiting_filler",
                    0.0,
                    duration,
                    channel_slug,
                )
        slate = ensure_continuity_slate(self.config)
        return PlaybackTarget(
            -1,
            None,
            waiting_title,
            "watch_waiting_slate",
            when,
            when + timedelta(seconds=10),
            slate,
            "watch_waiting",
            0.0,
            10.0,
            channel_slug,
        )

    def _resolve_watch(self, channel_slug: str, when: datetime) -> PlaybackTarget:
        from retrotv.internet_media import WatchProgressService

        status = WatchProgressService(self.connection, self.config).status(channel_slug)
        if not status.get("position_confirmed"):
            return self._resolve_watch_filler(
                channel_slug,
                when,
                "Choose a starting position in Internet Media",
            )
        episode = status["current"]
        if episode is None:
            slate = ensure_continuity_slate(self.config)
            return PlaybackTarget(
                -1,
                None,
                "You're caught up",
                "watch_caught_up",
                when,
                when + timedelta(seconds=10),
                slate,
                "watch_caught_up",
                0.0,
                10.0,
                channel_slug,
            )
        path_value = episode.get("local_path")
        path = Path(str(path_value)).resolve() if path_value else None
        if (
            episode.get("preparation_state") != "ready"
            or path is None
            or not path.is_file()
        ):
            state = str(episode.get("preparation_state") or "cataloged").replace("_", " ")
            return self._resolve_watch_filler(
                channel_slug, when, f"Preparing {episode['display_title']} ({state})"
            )
        offset = max(0.0, float(status["position_seconds"]))
        duration = max(offset + 1.0, float(episode.get("duration_seconds") or offset + 1.0))
        remaining = max(1.0, duration - offset)
        return PlaybackTarget(
            int(episode["id"]),
            episode.get("asset_id") if episode.get("retained") else None,
            str(episode["display_title"]),
            "watch_episode",
            when,
            when + timedelta(seconds=remaining),
            path,
            "internet_cache" if not episode.get("retained") else "internet_retained",
            offset,
            remaining,
            channel_slug,
            video_filter=self._watch_video_filter(episode),
        )

    def _resolve_channel_fallback(
        self, channel_slug: str, when: datetime, _reason: str
    ) -> PlaybackTarget:
        """Keep an empty scheduled channel tuned while its schedule catches up."""
        channel = self.connection.execute(
            "SELECT display_name FROM channels WHERE slug=? AND enabled=1",
            (channel_slug,),
        ).fetchone()
        if channel is None:
            raise PlaybackError(f"Unknown or disabled channel: {channel_slug}")
        duration = 10.0
        path = None
        representation = "generated_continuity_fallback"
        title = f"{channel['display_name']} — Programming unavailable"
        try:
            from retrotv.graphics import GraphicsService

            graphics = GraphicsService(self.connection, self.config)
            graphic = graphics.select_asset(channel_slug, "interstitials", when)
            if graphic is not None:
                path = graphics.render_interstitial(
                    int(graphic["id"]), duration_seconds=duration, when=when
                )
                representation = "generated_channel_fallback"
                title = f"{channel['display_name']} — Station Interstitial"
        except Exception:
            path = None
        if path is None:
            path = ensure_continuity_slate(self.config)
        ends = when + timedelta(seconds=30)
        return PlaybackTarget(
            -1, None, title, "channel_fallback", when, ends, path,
            representation, 0.0, 30.0, channel_slug,
        )

    def resolve(
        self, channel_slug: str, when: datetime | None = None
    ) -> PlaybackTarget:
        when = when or datetime.now(self.scheduler.timezone)
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.scheduler.timezone)
        if channel_slug == "__guide__":
            return self._resolve_guide(when)
        if channel_slug in self.synthetic_readers:
            return self._resolve_synthetic(channel_slug, when)
        if self._is_watch_at_your_pace(channel_slug):
            return self._resolve_watch(channel_slug, when)
        generation = self.scheduler.active_generation(channel_slug, when.date())
        if generation is None:
            return self._resolve_channel_fallback(
                channel_slug,
                when,
                f"No active schedule for {when.date().isoformat()}",
            )
        selected = None
        for row in self.scheduler.entries(int(generation["id"])):
            starts = datetime.fromisoformat(row["starts_at"])
            ends = datetime.fromisoformat(row["ends_at"])
            if starts <= when < ends:
                selected = (row, starts, ends)
                break
        if selected is None:
            return self._resolve_channel_fallback(
                channel_slug,
                when,
                f"No schedule entry covers {when.isoformat()}",
            )
        row, starts, ends = selected
        elapsed = max(0.0, (when - starts).total_seconds())
        remaining = max(0.0, (ends - when).total_seconds())
        media_offset = float(row["media_start_ms"] or 0) / 1000 + elapsed
        path = None
        representation = "continuity"
        if row["asset_id"] is None:
            if row["entry_type"] == "graphic_interstitial":
                from retrotv.graphics import GraphicsService

                graphic_duration = max(
                    1.0,
                    float(
                        self.config.data.get("graphics", {}).get(
                            "interstitial_duration_seconds", 10
                        )
                    ),
                )
                try:
                    if row["graphic_asset_id"] is None:
                        raise ValueError("Scheduled graphic has no graphic asset")
                    path = GraphicsService(
                        self.connection, self.config
                    ).render_interstitial(
                        int(row["graphic_asset_id"]),
                        # A long schedule gap loops one short cached graphic.
                        # Never block a channel tune by rendering the entire
                        # unresolved gap as a new video.
                        duration_seconds=graphic_duration,
                        when=starts,
                    )
                    media_offset = elapsed % graphic_duration
                    representation = "generated_graphic_interstitial"
                except Exception:
                    # Missing or damaged graphics must not make a channel
                    # untunable. The continuity slate is already a short loop
                    # and is always safe to join at the wall clock.
                    path = ensure_continuity_slate(self.config)
                    media_offset = elapsed % 10.0
                    representation = "generated_continuity_fallback"
            elif row["entry_type"] in {
                "announcement_opportunity", "weekly_special_promo_opportunity"
            }:
                from retrotv.announcements import UpNextRenderer

                opportunity = self.connection.execute(
                    """
                    SELECT id, fallback_path, validated_output_path
                    FROM announcement_opportunities WHERE schedule_entry_id=?
                    """,
                    (int(row["id"]),),
                ).fetchone()
                if opportunity is None:
                    raise PlaybackError("Scheduled generated screen has no opportunity record")
                narrated = Path(opportunity["validated_output_path"]) if opportunity["validated_output_path"] else None
                fallback = Path(opportunity["fallback_path"]) if opportunity["fallback_path"] else None
                if narrated is not None and narrated.is_file():
                    path = narrated
                    representation = "generated_announcement_narrated"
                elif fallback is not None and fallback.is_file():
                    path = fallback
                    representation = "generated_announcement_silent"
                else:
                    try:
                        result = UpNextRenderer(self.connection, self.config).render_opportunity(
                            int(opportunity["id"]), use_tts=False
                        )
                    except Exception as exc:
                        raise PlaybackError(
                            f"Unable to prepare the scheduled generated screen: {exc}"
                        ) from exc
                    path = result.output
                    representation = "generated_announcement_silent"
                media_offset = elapsed
            else:
                path = ensure_continuity_slate(self.config)
                media_offset = elapsed % 10.0
                representation = "generated_continuity"
        else:
            # With the normal replace-and-backup policy a ready correction is
            # temporary staging, not an alternate library representation.
            # Only the legacy read-only proxy policy may play it directly.
            output_policy = str(
                self.config.data.get("normalization", {}).get(
                    "output_policy", "replace_with_backup"
                )
            )
            if (
                output_policy == "proxy"
                and row["proxy_status"] == "ready"
                and row["proxy_relative_path"]
            ):
                candidate = (
                    self.config.storage_root("normalized")
                    / Path(row["proxy_relative_path"])
                )
                if candidate.is_file():
                    path = candidate
                    representation = "corrected_staging"
            if path is None and row["source_relative_path"]:
                candidate = (
                    self.config.storage_root("media")
                    / Path(row["source_relative_path"])
                )
                if not candidate.is_file():
                    from dataclasses import replace
                    missing = {"asset_id": int(row["asset_id"]), "path": str(candidate),
                               "channel": channel_slug, "entry_id": int(row["id"])}
                    if self.missing_media_handler:
                        self.missing_media_handler(missing)
                    fallback = self._resolve_channel_fallback(channel_slug, when, "Missing scheduled media")
                    # Recheck periodically for a repaired path, but never cross
                    # the original entry boundary or shift later programming.
                    fallback_end = min(ends, fallback.ends_at)
                    return replace(fallback, entry_id=int(row["id"]), ends_at=fallback_end,
                                   remaining_seconds=(fallback_end - when).total_seconds(),
                                   generation_id=str(generation["id"]), missing_media=missing)
                path = candidate
                representation = (
                    "corrected_working"
                    if row["proxy_status"] == "promoted"
                    else "original"
                )
        return PlaybackTarget(
            int(row["id"]),
            row["asset_id"],
            row["title"],
            row["entry_type"],
            starts,
            ends,
            path,
            representation,
            media_offset,
            remaining,
            channel_slug,
        )

    def _resolve_synthetic(
        self, channel_slug: str, when: datetime
    ) -> PlaybackTarget:
        try:
            position = self.synthetic_readers[channel_slug].playback_position(
                now=when
            )
        except (OSError, ValueError, SyntheticChannelError) as exc:
            # A publication can disappear between channel selection and load.
            # Keep the player alive and retry on a short continuity boundary.
            print(f"Synthetic channel {channel_slug!r} is unavailable: {exc}")
            ends = when + timedelta(seconds=30)
            return PlaybackTarget(
                -2, None, f"{channel_slug.title()} — Temporarily unavailable",
                "channel_fallback", when, ends, ensure_continuity_slate(self.config),
                "generated_synthetic_fallback", 0.0, 30.0, channel_slug,
            )
        manifest = position.manifest
        starts = position.starts_at.astimezone(when.tzinfo)
        ends = position.ends_at.astimezone(when.tzinfo)
        return PlaybackTarget(
            -2,
            None,
            position.segment_title,
            "synthetic_channel",
            starts,
            ends,
            manifest.video_path,
            "synthetic_channel",
            position.offset_seconds,
            max(0.0, (ends - when).total_seconds()),
            channel_slug,
            manifest.generation_id,
            position.segment_id,
            manifest.duration_seconds,
        )

    def _resolve_guide(self, when: datetime) -> PlaybackTarget:
        row = self.connection.execute(
            """
            SELECT output_relative_path, duration_seconds
            FROM guide_renders
            WHERE status = 'valid'
            ORDER BY activated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            raw_path = Path(row["output_relative_path"])
            duration = max(0.1, float(row["duration_seconds"]))
        else:
            settings = self.config.data.get("guide", {})
            raw_path = Path(
                str(settings.get("output", "./data/generated/guide.mp4"))
            )
            duration = max(0.1, float(settings.get("page_seconds", 10)))
        path = (
            raw_path
            if raw_path.is_absolute()
            else (self.config.project_root / raw_path).resolve()
        )
        if not path.is_file():
            raise PlaybackError(
                "The Guide channel has not been rendered yet. "
                "Run render-guide or startup maintenance."
            )
        offset = when.timestamp() % duration
        return PlaybackTarget(
            -1,
            None,
            "CRTV Sim Guide",
            "guide",
            when,
            when + timedelta(days=365),
            path,
            "generated_guide",
            offset,
            365 * 24 * 60 * 60,
        )


class Player(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def load(self, target: PlaybackTarget) -> None: ...

    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    def set_paused(self, paused: bool) -> None:
        raise PlaybackError("Pause is not supported by this player")

    def playback_position(self) -> float | None:
        return None

    def is_paused(self) -> bool:
        return False

    def is_buffering(self) -> bool:
        return False

    def poll_completed(self) -> bool:
        return False

    @property
    def supports_progress(self) -> bool:
        return False

    def poll_action(self) -> str | None:
        return None

    def show_text(self, text: str, duration_ms: int = 2000) -> None:
        return

    def set_osd_font_size(self, size: int) -> None:
        return

    def adjust_volume(self, amount: float) -> None:
        return

    def toggle_mute(self) -> None:
        return


class MpvIpcPlayer(Player):
    def __init__(self, config: AppConfig, *, executable: str | None = None):
        self.config = config
        settings = config.data.get("player", {})
        self.executable = executable or _configured_executable(
            config, str(settings.get("mpv", "mpv"))
        )
        self.fullscreen = bool(settings.get("fullscreen", False))
        self.foreground_on_start = bool(settings.get("foreground_on_start", True))
        self.osd_color = str(settings.get("osd_color", "#00ff66"))
        self.osd_font = str(settings.get("osd_font", "VCR OSD Mono"))
        self.osd_font_size = max(24, min(96, int(settings.get("osd_font_size", 55))))
        self.osd_margin_x = max(0, int(settings.get("osd_margin_x", 48)))
        self.osd_margin_y = max(0, int(settings.get("osd_margin_y", 36)))
        raw_fonts_dir = Path(str(settings.get("fonts_dir", "assets/fonts")))
        self.fonts_dir = (
            raw_fonts_dir
            if raw_fonts_dir.is_absolute()
            else (config.project_root / raw_fonts_dir).resolve()
        )
        self.extra_args = [str(value) for value in settings.get("extra_args", [])]
        self.ipc_timeout = float(settings.get("ipc_timeout_seconds", 10))
        self.process: subprocess.Popen | None = None
        self.connection = None
        self.write_lock = threading.Lock()
        self.request_id = 0
        self.file_loaded = threading.Event()
        self.file_completed = threading.Event()
        self._ipc_path: str | None = None
        self._responses: dict[int, dict] = {}
        self._response_ready = threading.Condition()
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_error: Exception | None = None
        self._actions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._has_loaded_media = False

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        platform_name = str(
            self.config.data.get("platform", {}).get("name", "windows")
        )
        if platform_name == "windows":
            self._ipc_path = rf"\\.\pipe\retrotv-mpv-{os.getpid()}"
        else:
            state = self.config.storage_root("state")
            state.mkdir(parents=True, exist_ok=True)
            self._ipc_path = os.fspath(state / "mpv.sock")
            Path(self._ipc_path).unlink(missing_ok=True)
        args = [
            self.executable,
            "--idle=yes",
            "--keep-open=yes",
            "--force-window=yes",
            "--pause=yes",
            "--no-terminal",
            f"--input-ipc-server={self._ipc_path}",
            f"--input-conf={self._input_config()}",
            f"--osd-color={self.osd_color}",
            f"--osd-font={self.osd_font}",
            f"--osd-font-size={self.osd_font_size}",
            f"--osd-fonts-dir={self.fonts_dir}",
            "--osd-align-x=left",
            "--osd-align-y=top",
            f"--osd-margin-x={self.osd_margin_x}",
            f"--osd-margin-y={self.osd_margin_y}",
        ]
        if self.fullscreen:
            args.append("--fullscreen=yes")
        if self.foreground_on_start:
            # mpv 0.41 removed the older --focus-on-open spelling.  Use the
            # current option so a foreground preference can never prevent the
            # player from starting.
            args.append("--focus-on=open")
        args.extend(self.extra_args)
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise PlaybackError(f"mpv was not found: {self.executable}") from exc
        deadline = time_module.monotonic() + self.ipc_timeout
        last_error = None
        while time_module.monotonic() < deadline:
            if self.process.poll() is not None:
                raise PlaybackError("mpv exited before IPC became available")
            try:
                if platform_name == "windows":
                    self.connection = _open_windows_pipe(
                        self._ipc_path, self.ipc_timeout
                    )
                else:
                    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    connection.connect(self._ipc_path)
                    self.connection = connection.makefile("rwb", buffering=0)
                break
            except OSError as exc:
                last_error = exc
                time_module.sleep(0.05)
        if self.connection is None:
            self.stop()
            raise PlaybackError(f"Unable to connect to mpv IPC: {last_error}")
        self._reader_stop.clear()
        self._reader_error = None
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name="retrotv-mpv-ipc",
            daemon=True,
        )
        self._reader_thread.start()

    def _input_config(self) -> str:
        target = self.config.storage_root("state") / "generated" / "mpv-input.conf"
        target.parent.mkdir(parents=True, exist_ok=True)
        bindings = [
            f"{digit} script-message retrotv-action digit:{digit}"
            for digit in range(10)
        ]
        bindings.extend(
            [
                "ENTER script-message retrotv-action enter",
                "KP_ENTER script-message retrotv-action enter",
                "BS script-message retrotv-action backspace",
                "ESC script-message retrotv-action cancel",
                "g script-message retrotv-action guide",
                "UP script-message retrotv-action channel-up",
                "DOWN script-message retrotv-action channel-down",
                "PGUP script-message retrotv-action channel-up",
                "PGDWN script-message retrotv-action channel-down",
                "l script-message retrotv-action last-channel",
                "r script-message retrotv-action retune",
                "+ script-message retrotv-action volume-up",
                "- script-message retrotv-action volume-down",
                "m script-message retrotv-action mute",
                "SPACE script-message retrotv-action pause",
                "q script-message retrotv-action quit",
            ]
        )
        target.write_text("\n".join(bindings) + "\n", encoding="utf-8")
        return os.fspath(target)

    def _read_message(self):
        if self.connection is None:
            raise PlaybackError("mpv IPC is not connected")
        line = self.connection.readline()
        if not line:
            raise PlaybackError("mpv IPC closed while waiting for a response")
        try:
            message = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise PlaybackError("mpv returned invalid IPC JSON") from exc
        if message.get("event") == "file-loaded":
            self.file_loaded.set()
        if message.get("event") == "end-file" and message.get("reason") == "eof":
            self.file_completed.set()
        return message

    def _read_loop(self) -> None:
        while not self._reader_stop.is_set():
            try:
                message = self._read_message()
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._reader_stop.is_set():
                    self._reader_error = exc
                    with self._response_ready:
                        self._response_ready.notify_all()
                return
            request_id = message.get("request_id")
            if request_id is not None:
                with self._response_ready:
                    self._responses[int(request_id)] = message
                    self._response_ready.notify_all()
            if message.get("event") == "client-message":
                arguments = message.get("args") or []
                if len(arguments) >= 2 and arguments[0] == "retrotv-action":
                    self._actions.put(str(arguments[1]))

    def _command(self, command: list, timeout: float | None = None):
        if self.connection is None:
            raise PlaybackError("mpv IPC is not connected")
        with self.write_lock:
            self.request_id += 1
            request_id = self.request_id
            payload = json.dumps(
                {"command": command, "request_id": request_id},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                self.connection.write(payload)
            except OSError as exc:
                raise PlaybackError(f"mpv IPC write failed: {exc}") from exc
        deadline = time_module.monotonic() + (timeout or self.ipc_timeout)
        with self._response_ready:
            while request_id not in self._responses and self._reader_error is None:
                remaining = deadline - time_module.monotonic()
                if remaining <= 0:
                    break
                self._response_ready.wait(remaining)
            result = self._responses.pop(request_id, None)
            reader_error = self._reader_error
        if result is None:
            if reader_error is not None:
                raise PlaybackError(f"mpv IPC stopped: {reader_error}")
            raise PlaybackError(f"mpv command timed out: {command[0]}")
        if result.get("error") != "success":
            raise PlaybackError(f"mpv command failed: {result.get('error')}")
        return result.get("data")

    def load(self, target: PlaybackTarget) -> None:
        if target.path is None:
            raise PlaybackError("Continuity requires a generated slate")
        self.start()
        focus_after_initial_load = self.foreground_on_start and not self._has_loaded_media
        self.file_loaded.clear()
        self.file_completed.clear()
        options = {
            "start": f"{target.media_offset_seconds:.3f}",
            "pause": "yes",
            "vf": target.video_filter or "",
        }
        if target.entry_type in {
            "continuity", "graphic_interstitial", "guide", "synthetic_channel",
            "watch_waiting_slate", "channel_fallback",
        }:
            options["loop-file"] = "inf"
        self._command(
            ["loadfile", os.fspath(target.path), "replace", -1, options]
        )
        deadline = time_module.monotonic() + self.ipc_timeout
        while not self.file_loaded.is_set() and time_module.monotonic() < deadline:
            time_module.sleep(0.01)
        if not self.file_loaded.is_set():
            raise PlaybackError("mpv did not report file-loaded")
        self._command(
            ["seek", target.media_offset_seconds, "absolute+exact"]
        )
        reported = self._command(["get_property", "time-pos"])
        if reported is not None and abs(float(reported) - target.media_offset_seconds) > 2:
            raise PlaybackError(
                f"mpv seek verification differed by "
                f"{abs(float(reported) - target.media_offset_seconds):.2f}s"
            )
        self._command(["set_property", "pause", False])
        if focus_after_initial_load and self.process is not None:
            _focus_windows_process_window(self.process.pid)
        self._has_loaded_media = True

    def is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def set_paused(self, paused: bool) -> None:
        self._command(["set_property", "pause", bool(paused)])

    def playback_position(self) -> float | None:
        value = self._command(["get_property", "time-pos"])
        return None if value is None else max(0.0, float(value))

    def is_paused(self) -> bool:
        return bool(self._command(["get_property", "pause"]))

    def is_buffering(self) -> bool:
        try:
            return bool(self._command(["get_property", "paused-for-cache"]))
        except PlaybackError:
            return False

    def poll_completed(self) -> bool:
        if self.file_completed.is_set():
            self.file_completed.clear()
            return True
        # With --keep-open=yes mpv pauses at EOF instead of unloading the file,
        # so it may never send an end-file event. This property remains set
        # while the completed file is held open.
        return bool(self._command(["get_property", "eof-reached"]))

    @property
    def supports_progress(self) -> bool:
        return True

    def poll_action(self) -> str | None:
        try:
            return self._actions.get_nowait()
        except queue.Empty:
            return None

    def show_text(self, text: str, duration_ms: int = 2000) -> None:
        self._command(["show-text", text, duration_ms])

    def set_osd_font_size(self, size: int) -> None:
        self.osd_font_size = max(24, min(96, int(size)))
        self._command(["set_property", "osd-font-size", self.osd_font_size])

    def adjust_volume(self, amount: float) -> None:
        self._command(["add", "volume", amount])

    def toggle_mute(self) -> None:
        muted = bool(self._command(["get_property", "mute"]))
        self._command(["set_property", "mute", not muted])

    def stop(self) -> None:
        self._reader_stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
            self.connection = None
        self.process = None


class FfplayPlayer(Player):
    """Development fallback; mpv remains the production player contract."""

    def __init__(self, config: AppConfig):
        settings = config.data.get("player", {})
        self.executable = _configured_executable(
            config, str(settings.get("ffplay", "ffplay"))
        )
        self.fullscreen = bool(settings.get("fullscreen", False))
        self.foreground_on_start = bool(settings.get("foreground_on_start", True))
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        return

    def load(self, target: PlaybackTarget) -> None:
        if target.path is None:
            raise PlaybackError("Continuity requires a generated slate")
        self.stop()
        args = [
            self.executable,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-autoexit",
            "-ss",
            f"{target.media_offset_seconds:.3f}",
        ]
        if target.entry_type != "synthetic_channel":
            args.extend([
                "-t",
                f"{max(0.1, target.remaining_seconds):.3f}",
            ])
        args.extend([
            "-window_title",
            f"CRTV Sim — {target.title}",
        ])
        if target.entry_type in {
            "continuity", "graphic_interstitial", "guide", "synthetic_channel",
            "channel_fallback",
        }:
            args.extend(["-stream_loop", "-1"])
        if self.fullscreen:
            args.append("-fs")
        args.extend(["-i", os.fspath(target.path)])
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if self.foreground_on_start:
                _focus_windows_process_window(self.process.pid)
        except FileNotFoundError as exc:
            raise PlaybackError(f"ffplay was not found: {self.executable}") from exc

    def is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


class PlaybackController:
    def __init__(
        self,
        resolver: ScheduleResolver,
        player: Player,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
        skip_short_remaining_seconds: float = 1.0,
        action_handler: Callable[[str], None] | None = None,
    ):
        self.resolver = resolver
        self.player = player
        self.clock = clock or (
            lambda: datetime.now(self.resolver.scheduler.timezone)
        )
        self.sleep = sleep
        self.skip_short = skip_short_remaining_seconds
        self.stop_event = threading.Event()
        self.retune_event = threading.Event()
        self.current: PlaybackTarget | None = None
        self.action_handler = action_handler
        self.channel_slug: str | None = None
        self.channel_lock = threading.Lock()
        self._pending_watch_cleanup: list[int] = []

    def _checkpoint_watch(self, target: PlaybackTarget | None = None) -> None:
        active = target or self.current
        if active is None or active.entry_type != "watch_episode":
            return
        try:
            position = self.player.playback_position()
            if position is None:
                return
            from retrotv.internet_media import WatchProgressService

            WatchProgressService(
                self.resolver.connection, self.resolver.config
            ).save_position(str(active.channel_slug), active.entry_id, position)
        except (PlaybackError, OSError, ValueError, sqlite3.Error):
            # The previous 30-second checkpoint remains a safe replay point.
            return

    def tune_once(
        self, channel_slug: str, *, continue_synthetic: bool = False
    ) -> PlaybackTarget:
        target = self.resolver.resolve(channel_slug, self.clock())
        if (
            target.entry_type != "synthetic_channel"
            and target.entry_type != "watch_episode"
            and target.entry_type != "channel_fallback"
            and target.remaining_seconds <= self.skip_short
        ):
            raise PlaybackError(
                f"Only {target.remaining_seconds:.2f}s remains in entry {target.entry_id}"
            )
        current = self.current
        can_continue = bool(
            continue_synthetic
            and current is not None
            and current.entry_type in {"synthetic_channel", "channel_fallback"}
            and target.entry_type == current.entry_type
            and current.channel_slug == target.channel_slug
            and current.generation_id == target.generation_id
            and current.path == target.path
            and self.player.is_running()
        )
        if not can_continue:
            if self.current is not None and self.current != target:
                self._checkpoint_watch(self.current)
            if target.entry_type == "watch_episode" and not self.player.supports_progress:
                raise PlaybackError("Watch at Your Pace channels require the mpv player")
            self.player.load(target)
        self.current = target
        if self._pending_watch_cleanup:
            from retrotv.internet_media import PreparationService

            preparation = PreparationService(
                self.resolver.connection, self.resolver.config
            )
            for episode_id in self._pending_watch_cleanup:
                preparation.delete_completed_cache(episode_id)
            self._pending_watch_cleanup.clear()
        return target

    def run(self, channel_slug: str) -> None:
        with self.channel_lock:
            self.channel_slug = channel_slug
        failures = 0
        continue_synthetic = False
        try:
            while not self.stop_event.is_set():
                with self.channel_lock:
                    active_channel = self.channel_slug or channel_slug
                try:
                    target = self.tune_once(
                        active_channel,
                        continue_synthetic=continue_synthetic,
                    )
                    continue_synthetic = False
                    failures = 0
                    print(
                        f"Now playing: {target.title} "
                        f"at +{target.media_offset_seconds:.1f}s "
                        f"({target.representation})"
                    )
                    next_checkpoint = time_module.monotonic() + 30.0
                except PlaybackError as exc:
                    failures += 1
                    print(f"Playback recovery: {exc}")
                    if failures >= 3:
                        raise
                    self.sleep(1)
                    continue
                while not self.stop_event.is_set():
                    action = self.player.poll_action()
                    if action == "pause":
                        self._checkpoint_watch(target)
                        try:
                            self.player.set_paused(not self.player.is_paused())
                        except PlaybackError:
                            pass
                    if action and self.action_handler is not None:
                        self.action_handler(action)
                    if target.entry_type == "watch_episode":
                        if self.retune_event.is_set():
                            self._checkpoint_watch(target)
                            self.retune_event.clear()
                            continue_synthetic = False
                            break
                        if self.player.poll_completed():
                            from retrotv.internet_media import WatchProgressService

                            WatchProgressService(
                                self.resolver.connection, self.resolver.config
                            ).complete(str(target.channel_slug), target.entry_id)
                            self._pending_watch_cleanup.append(target.entry_id)
                            self.current = None
                            break
                        if time_module.monotonic() >= next_checkpoint:
                            try:
                                paused = self.player.is_paused()
                                buffering = self.player.is_buffering()
                            except PlaybackError:
                                paused = buffering = False
                            if not paused and not buffering:
                                self._checkpoint_watch(target)
                            next_checkpoint = time_module.monotonic() + 30.0
                        if not self.player.is_running():
                            print("Player exited early; resuming from the last watch checkpoint.")
                            self.current = None
                            break
                        self.sleep(0.5)
                        continue
                    remaining = (target.ends_at - self.clock()).total_seconds()
                    if remaining <= 0:
                        if target.entry_type == "watch_caught_up":
                            self.stop_event.set()
                        continue_synthetic = target.entry_type in {
                            "synthetic_channel", "channel_fallback"
                        }
                        break
                    if self.retune_event.is_set():
                        self.retune_event.clear()
                        continue_synthetic = False
                        break
                    if not self.player.is_running():
                        print("Player exited early; retuning from wall-clock position.")
                        continue_synthetic = False
                        break
                    self.sleep(min(0.5, remaining))
        finally:
            self._checkpoint_watch()
            self.player.stop()

    def request_retune(self) -> None:
        self.retune_event.set()

    def request_tune(self, channel_slug: str) -> None:
        with self.channel_lock:
            self.channel_slug = channel_slug
        self.retune_event.set()

    def stop(self) -> None:
        self.stop_event.set()


def _configured_executable(config: AppConfig, value: str) -> str:
    """Resolve explicitly pathed tools relative to the application root."""
    candidate = Path(os.path.expandvars(os.path.expanduser(value)))
    if candidate.is_absolute():
        return os.fspath(candidate)
    if candidate.parent != Path("."):
        return os.fspath((config.project_root / candidate).resolve())
    return value


def create_player(config: AppConfig, backend: str = "auto") -> Player:
    settings = config.data.get("player", {})
    portable_root = os.environ.get("RETROTV_PORTABLE_ROOT")
    if portable_root:
        bundled_mpv = (Path(portable_root) / "tools" / "mpv" / "mpv.exe").resolve()
        if bundled_mpv.is_file():
            print(f"Player backend: bundled mpv ({bundled_mpv})")
            return MpvIpcPlayer(config, executable=os.fspath(bundled_mpv))
        raise PlaybackError(
            f"Portable CRTV Sim requires its bundled mpv player: {bundled_mpv}"
        )
    if backend == "auto":
        mpv = _configured_executable(config, str(settings.get("mpv", "mpv")))
        backend = "mpv" if Path(mpv).is_file() or shutil.which(mpv) else "ffplay"
    if backend == "mpv":
        return MpvIpcPlayer(config)
    if backend == "ffplay":
        return FfplayPlayer(config)
    raise ValueError(f"Unknown player backend: {backend}")


def start_windows_keyboard(controller: PlaybackController) -> threading.Thread | None:
    if os.name != "nt":
        return None

    def worker() -> None:
        import msvcrt

        print("Controls: R retunes to wall clock; Q quits.")
        while not controller.stop_event.is_set():
            if not msvcrt.kbhit():
                time_module.sleep(0.05)
                continue
            key = msvcrt.getwch().casefold()
            if key == "q":
                controller.stop()
            elif key == "r":
                controller.request_retune()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread
