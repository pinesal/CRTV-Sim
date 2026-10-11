from __future__ import annotations

import subprocess
import time
from threading import Event


class ProcessCancelled(RuntimeError):
    """Raised after a maintenance-owned child process is stopped safely."""


def run_cancellable(
    args: list[str], timeout: float, cancel_event: Event
) -> subprocess.CompletedProcess[str]:
    """Run a quiet child process and stop it when the maintenance deadline arrives."""
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + timeout
    while True:
        if cancel_event.is_set():
            process.terminate()
            try:
                process.communicate(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise ProcessCancelled("Maintenance deadline reached; work was deferred")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue
