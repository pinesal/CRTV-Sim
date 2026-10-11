from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from retrotv.config import AppConfig


DAILY_BACKUP_RE = re.compile(r"^retrotv-(\d{4}-\d{2}-\d{2})\.sqlite3$")


class DatabaseBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseBackupResult:
    created: bool
    skipped: bool
    reason: str | None
    path: str | None
    size_bytes: int
    retained_count: int
    removed_count: int
    completed_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DatabaseBackupService:
    """Create verified, rotating snapshots with SQLite's online backup API."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.settings = config.data.get("database_backups", {})
        self.enabled = bool(self.settings.get("enabled", True))
        self.retention_count = max(1, int(self.settings.get("retention_count", 7)))

    @property
    def backup_directory(self) -> Path:
        configured = str(self.settings.get("directory", "backups")).strip() or "backups"
        path = Path(os.path.expandvars(os.path.expanduser(configured)))
        if not path.is_absolute():
            path = self.config.database_path.parent / path
        return path.resolve()

    def _now(self, supplied: datetime | None) -> datetime:
        if supplied is not None:
            return supplied if supplied.tzinfo else supplied.astimezone()
        timezone_name = str(
            self.config.data.get("application", {}).get("timezone", "UTC")
        )
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception:
            return datetime.now().astimezone()
        return datetime.now(timezone)

    @staticmethod
    def _check(connection: sqlite3.Connection, pragma: str) -> None:
        rows = connection.execute(f"PRAGMA {pragma}").fetchall()
        if len(rows) != 1 or str(rows[0][0]).casefold() != "ok":
            detail = "; ".join(str(row[0]) for row in rows[:5])
            raise DatabaseBackupError(f"Database {pragma} failed: {detail}")

    def inventory(self) -> dict[str, object]:
        directory = self.backup_directory
        backups = self._daily_backups(directory) if directory.is_dir() else []
        latest = backups[-1] if backups else None
        return {
            "enabled": self.enabled,
            "directory": str(directory),
            "retention_count": self.retention_count,
            "retained_count": len(backups),
            "latest_path": str(latest) if latest else None,
            "latest_size_bytes": latest.stat().st_size if latest else 0,
            "latest_modified_at": (
                datetime.fromtimestamp(latest.stat().st_mtime)
                .astimezone()
                .isoformat(timespec="seconds")
                if latest
                else None
            ),
        }

    def create(
        self,
        source: sqlite3.Connection,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> DatabaseBackupResult:
        current = self._now(now)
        completed_at = current.isoformat(timespec="seconds")
        if not self.enabled and not force:
            inventory = self.inventory()
            return DatabaseBackupResult(
                False,
                True,
                "Automatic database backups are disabled",
                inventory["latest_path"],
                int(inventory["latest_size_bytes"]),
                int(inventory["retained_count"]),
                0,
                completed_at,
            )

        directory = self.backup_directory
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"retrotv-{current.date().isoformat()}.sqlite3"
        partial = destination.with_name(destination.name + ".partial")
        if destination.is_file() and not force:
            inventory = self.inventory()
            return DatabaseBackupResult(
                False,
                True,
                "Today's verified backup already exists",
                str(destination),
                destination.stat().st_size,
                int(inventory["retained_count"]),
                0,
                completed_at,
            )

        # Never publish a snapshot sourced from a database already known to be bad.
        self._check(source, "quick_check")
        partial.unlink(missing_ok=True)
        try:
            target = sqlite3.connect(partial, timeout=30)
            try:
                source.backup(target)
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._check(target, "integrity_check")
                foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_keys:
                    raise DatabaseBackupError(
                        f"Backup has {len(foreign_keys)} foreign-key violation(s)"
                    )
            finally:
                target.close()
            os.replace(partial, destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        removed = self._enforce_retention(directory)
        inventory = self.inventory()
        return DatabaseBackupResult(
            True,
            False,
            None,
            str(destination),
            destination.stat().st_size,
            int(inventory["retained_count"]),
            removed,
            completed_at,
        )

    def create_for_shutdown(
        self, source: sqlite3.Connection, *, now: datetime | None = None
    ) -> DatabaseBackupResult:
        """Reuse a published, verified daily snapshot for up to 24 hours.

        Final daily filenames are published only after verification succeeds.
        Do not re-read/check the entire snapshot during shutdown, and never
        count partial, empty, or future-dated files as a recent backup.
        """
        current = self._now(now)
        directory = self.backup_directory
        backups = self._daily_backups(directory) if directory.is_dir() else []
        recent = []
        for path in backups:
            stat = path.stat()
            age = current.timestamp() - stat.st_mtime
            if stat.st_size > 0 and 0 <= age <= 24 * 60 * 60:
                recent.append((stat.st_mtime, path, stat.st_size))
        if recent:
            _modified, path, size = max(recent, key=lambda item: (item[0], item[1].name))
            return DatabaseBackupResult(
                False, True, "A verified backup from the last 24 hours already exists",
                str(path), size, len(backups), 0,
                current.isoformat(timespec="seconds"),
            )
        # A calendar-day check alone can accept a snapshot older than 24 hours
        # or create another just after midnight. Use elapsed age above instead.
        return self.create(source, force=True, now=current)

    @staticmethod
    def _daily_backups(directory: Path) -> list[Path]:
        return sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and DAILY_BACKUP_RE.fullmatch(path.name)
            ),
            key=lambda path: path.name,
        )

    def _enforce_retention(self, directory: Path) -> int:
        backups = self._daily_backups(directory)
        expired = backups[: max(0, len(backups) - self.retention_count)]
        for path in expired:
            path.unlink()
        return len(expired)
