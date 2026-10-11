from __future__ import annotations

import subprocess
from typing import Any


def hidden_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a background command without creating a Windows console window."""
    kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(*args, **kwargs)
