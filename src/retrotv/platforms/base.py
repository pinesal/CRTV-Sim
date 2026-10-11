from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class PlatformAdapter:
    name: str
    player_ipc_kind: str
    service_kind: str

    @staticmethod
    def logical_path(value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Logical media path must be relative and contained: {value}")
        return path.as_posix()

