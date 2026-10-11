from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid


class ShutdownControl:
    """Drive-local control channel used when the Dashboard is unavailable."""

    SCHEMA_VERSION = 1

    def __init__(self, state_root: Path):
        self.directory = Path(state_root) / "control"
        self.descriptor_path = self.directory / "runtime.json"
        self.request_path = self.directory / "shutdown-request.json"
        self.status_path = self.directory / "shutdown-status.json"

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def begin_session(self, *, dashboard_url: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.request_path.unlink(missing_ok=True)
        self.status_path.unlink(missing_ok=True)
        self._write(
            self.descriptor_path,
            {
                "schema_version": self.SCHEMA_VERSION,
                "pid": os.getpid(),
                "started_at": self._now(),
                "dashboard_url": dashboard_url,
                "request_path": self.request_path.name,
                "status_path": self.status_path.name,
            },
        )

    def read_descriptor(self) -> dict[str, Any] | None:
        return self._read(self.descriptor_path)

    def submit_request(self, *, requester_pid: int | None = None) -> str:
        request_id = uuid.uuid4().hex
        self._write(
            self.request_path,
            {
                "schema_version": self.SCHEMA_VERSION,
                "request_id": request_id,
                "requested_at": self._now(),
                "requester_pid": requester_pid,
            },
        )
        return request_id

    def read_request(self) -> dict[str, Any] | None:
        payload = self._read(self.request_path)
        if payload is None or payload.get("schema_version") != self.SCHEMA_VERSION:
            return None
        request_id = payload.get("request_id")
        return payload if isinstance(request_id, str) and request_id else None

    def clear_request(self, request_id: str) -> None:
        payload = self.read_request()
        if payload is not None and payload.get("request_id") == request_id:
            self.request_path.unlink(missing_ok=True)

    def write_status(
        self,
        request_id: str,
        *,
        state: str,
        message: str,
        shutdown: dict[str, Any] | None = None,
    ) -> None:
        self._write(
            self.status_path,
            {
                "schema_version": self.SCHEMA_VERSION,
                "request_id": request_id,
                "updated_at": self._now(),
                "state": state,
                "message": message,
                "shutdown": shutdown,
            },
        )

    def read_status(self, request_id: str) -> dict[str, Any] | None:
        payload = self._read(self.status_path)
        if payload is None or payload.get("request_id") != request_id:
            return None
        return payload
