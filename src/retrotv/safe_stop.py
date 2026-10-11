from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from retrotv.shutdown_control import ShutdownControl


def portable_root() -> Path:
    override = os.environ.get("RETROTV_PORTABLE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _message(message: str, *, title: str = "CRTV Sim Safe Stop", flags: int = 0) -> int:
    if os.name == "nt":
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(0, message, title, flags))
    print(message)
    return 1


def _confirm() -> bool:
    if os.name != "nt":
        return True
    # MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND
    return _message(
        "Safely shut down CRTV Sim?\n\n"
        "Current conversions and database work will be allowed to finish. "
        "This may take several minutes. A second message will appear when it "
        "is safe to use Windows Eject or turn off the computer.",
        flags=0x00000004 | 0x00000020 | 0x00010000,
    ) == 6


def _instance_running(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if handle.tell() == 0 and lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _legacy_api_shutdown(url: str) -> dict[str, object] | None:
    request = Request(
        f"{url.rstrip('/')}/api/runtime/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=6 * 60 * 60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def request_safe_stop(
    root: Path,
    *,
    timeout_seconds: float = 6 * 60 * 60,
    poll_seconds: float = 0.5,
) -> tuple[bool, str]:
    state_root = root / "user-data" / "state"
    lock_path = state_root / "retrotv.instance.lock"
    if not _instance_running(lock_path):
        return False, (
            "No running CRTV Sim application instance was detected.\n\n"
            "If FFmpeg, FFprobe, mpv, or RetroWeather processes are still visible, "
            "do not unplug the drive; restart Windows and use Eject afterward."
        )

    control = ShutdownControl(state_root)
    deadline = time.monotonic() + min(15.0, timeout_seconds)
    descriptor = control.read_descriptor()
    while descriptor is None and time.monotonic() < deadline:
        time.sleep(0.25)
        descriptor = control.read_descriptor()

    if descriptor is None:
        result = _legacy_api_shutdown("http://127.0.0.1:8765")
        shutdown = result.get("shutdown") if isinstance(result, dict) else None
        if isinstance(shutdown, dict) and shutdown.get("safe_to_remove") is True:
            return True, "CRTV Sim shut down safely. You may now use Windows Eject."
        return False, (
            "CRTV Sim is running, but this installation did not answer either "
            "the safe-stop control channel or the Dashboard API.\n\n"
            "Keep the drive connected. Restart Windows before disconnecting it."
        )

    request_id = control.submit_request(requester_pid=os.getpid())
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = control.read_status(request_id)
        if status is not None:
            if status.get("state") == "safe":
                return True, (
                    "CRTV Sim shut down safely.\n\n"
                    "All writing has finished and the database was flushed. You may now use "
                    "Windows Eject before unplugging the drive."
                )
            if status.get("state") == "error":
                return False, str(status.get("message") or "Safe shutdown failed")
        time.sleep(poll_seconds)
    return False, (
        "CRTV Sim did not confirm safe shutdown before the timeout. "
        "Keep the drive connected and restart Windows before disconnecting it."
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    assume_yes = "--yes" in arguments
    quiet = "--quiet" in arguments
    if not assume_yes and not _confirm():
        return 0
    success, message = request_safe_stop(portable_root())
    if not quiet:
        _message(
            message,
            flags=(0x00000040 if success else 0x00000010) | 0x00010000,
        )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
