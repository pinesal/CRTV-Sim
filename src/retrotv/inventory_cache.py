"""Reuse inventory snapshots without holding a database connection open."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable


class InventoryCache:
    def __init__(
        self, database_path: Path, loader: Callable[[], dict[str, object]],
        *, max_age: float = 300, change_delay: float = 60,
        retry_delay: float = 30, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database_path = database_path
        self.loader = loader
        self.max_age = max_age
        self.change_delay = change_delay
        self.retry_delay = retry_delay
        self.clock = clock
        self.lock = threading.Lock()
        self.data: dict[str, object] | None = None
        self.updated_at: str | None = None
        self.built_at = self.attempted_at = float("-inf")
        self.signature: tuple = ()
        self.epoch = self.built_epoch = 0
        self.revision = 0
        self.refreshing = False
        self.error: str | None = None

    def _signature(self) -> tuple:
        # Database/WAL changes also catch background workers and CLI edits.
        # No persistent watcher connection, table scan, or schema change is needed.
        parts = []
        for path in (self.database_path, Path(str(self.database_path) + "-wal")):
            try:
                stat = path.stat()
                parts.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except OSError:
                parts.append(None)
        return tuple(parts)

    def invalidate(self) -> None:
        with self.lock:
            self.epoch += 1

    def _state(self, signature: tuple) -> dict[str, object]:
        now = self.clock()
        invalidated = self.epoch != self.built_epoch
        expired = now - self.built_at >= self.max_age
        stale = self.data is None or invalidated or expired or signature != self.signature or bool(self.error)
        due = stale and (self.data is None or invalidated or expired
                         or self.error is not None
                         or now - self.attempted_at >= self.change_delay)
        if self.error and now - self.attempted_at < self.retry_delay:
            due = False
        return {
            "ready": self.data is not None, "updated_at": self.updated_at,
            "revision": self.revision, "stale": stale,
            "refreshing": self.refreshing, "refresh_due": bool(due),
            "error": self.error,
        }

    def _response(self, signature: tuple) -> dict[str, object]:
        return {**(self.data or {}), "cache": self._state(signature)}

    def get(self, refresh: str = "") -> dict[str, object]:
        """Return cached data, or rebuild when the caller requests a due refresh.

        Refreshing uses the requesting HTTP worker; concurrent requests return
        the previous snapshot immediately rather than starting duplicate work.
        """
        signature = self._signature()
        with self.lock:
            state = self._state(signature)
            rebuild = refresh == "force" or (refresh == "auto" and state["refresh_due"])
            if self.data is None and state["refresh_due"]:
                rebuild = True
            if self.refreshing or not rebuild:
                return self._response(signature)
            self.refreshing = True
            self.attempted_at = self.clock()
            epoch = self.epoch
        try:
            data = self.loader()
        except Exception as exc:
            with self.lock:
                self.error = str(exc) or type(exc).__name__
        else:
            with self.lock:
                self.data = data
                self.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
                self.built_at = self.clock()
                self.signature = signature
                self.built_epoch = epoch
                self.revision += 1
                self.error = None
        finally:
            with self.lock:
                self.refreshing = False
        signature = self._signature()
        with self.lock:
            return self._response(signature)
