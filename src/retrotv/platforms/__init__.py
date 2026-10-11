from retrotv.platforms.base import PlatformAdapter
from retrotv.platforms.linux import LinuxAdapter
from retrotv.platforms.windows import WindowsAdapter


def adapter_for(name: str) -> PlatformAdapter:
    normalized = name.strip().lower()
    if normalized == "windows":
        return WindowsAdapter()
    if normalized in {"linux", "raspberry-pi", "raspberry_pi"}:
        return LinuxAdapter()
    raise ValueError(f"Unsupported platform adapter: {name}")


__all__ = [
    "PlatformAdapter",
    "WindowsAdapter",
    "LinuxAdapter",
    "adapter_for",
]

