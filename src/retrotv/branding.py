"""Public branding and compatibility with previously named portable builds.

The retrotv package, profile paths and credential targets remain stable so
existing installations can be updated without migrating user data.
"""
from pathlib import Path


APP_NAME = "CRTV Sim"
PORTABLE_EXE = "CRTV-Sim.exe"
SAFE_STOP_EXE = "CRTV-Sim-Safe-Stop.exe"


def portable_executable(root: Path) -> Path | None:
    """Prefer the current launcher, but recognize legacy builds for recovery."""
    for name in (PORTABLE_EXE, "RetroTV.exe"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None
