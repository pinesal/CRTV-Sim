from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from retrotv.config import AppConfig
from retrotv import __version__
from retrotv.branding import portable_executable


FEATURE_MIGRATION = "020_internet_media.sql"
RESTORE_NAME = "internet-media-pre-feature"


class RestorePointError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(path: Path, relative_to: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


@dataclass(frozen=True)
class RestoreResult:
    restore_directory: str
    database_restored: str
    settings_restored: list[str]
    post_upgrade_database: str | None
    previous_build: str | None
    media_root: str
    managed_cache: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class RestorePointService:
    """Create and restore the one-time checkpoint preceding internet media."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.root = (
            config.storage_root("state") / "restore-points" / RESTORE_NAME
        ).resolve()
        self.manifest_path = self.root / "manifest.json"

    @staticmethod
    def migration_pending(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return True
        return connection.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_name=?",
            (FEATURE_MIGRATION,),
        ).fetchone() is None

    def inventory(self) -> dict[str, object]:
        if not self.manifest_path.is_file():
            return {"available": False, "directory": str(self.root)}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "available": False,
                "directory": str(self.root),
                "error": str(exc),
            }
        return {
            "available": True,
            "directory": str(self.root),
            "created_at": manifest.get("created_at"),
            "previous_build": manifest.get("previous_build"),
        }

    def create(self, source: sqlite3.Connection) -> dict[str, object]:
        if self.manifest_path.is_file():
            return {"created": False, **self.inventory()}

        self.root.mkdir(parents=True, exist_ok=True)
        database = self.root / "database.sqlite3"
        partial = self.root / "database.sqlite3.partial"
        partial.unlink(missing_ok=True)
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()
            if check is None or str(check[0]).casefold() != "ok":
                raise RestorePointError("Restore-point database integrity check failed")
        finally:
            target.close()
        os.replace(partial, database)

        files = [_record(database, self.root)]
        copied_settings: list[str] = []
        for source_path, name in (
            (self.config.profile_path, "profile.toml"),
            (self.config.profile_path.with_suffix(".settings.json"), "settings.json"),
        ):
            if not source_path.is_file():
                continue
            destination = self.root / name
            shutil.copy2(source_path, destination)
            files.append(_record(destination, self.root))
            copied_settings.append(name)

        portable_root = os.environ.get("RETROTV_PORTABLE_ROOT")
        previous_build = os.environ.get("RETROTV_PREVIOUS_BUILD")
        current_build = Path(portable_root).resolve() if portable_root else None
        if not previous_build and portable_root:
            current = current_build
            pointer = current / "previous-build.path"
            if pointer.is_file():
                raw = pointer.read_text(encoding="utf-8-sig").strip()
                pointed = Path(raw)
                resolved_pointer = (
                    pointed.resolve()
                    if pointed.is_absolute()
                    else (current / pointed).resolve()
                )
                if not _same_path(resolved_pointer, current):
                    previous_build = str(resolved_pointer)
            candidate = current.with_name(current.name + ".previous")
            if not previous_build and candidate.is_dir():
                previous_build = str(candidate)

        executable = None
        if previous_build:
            candidate = Path(previous_build).expanduser().resolve()
            exe = portable_executable(candidate)
            if (
                (current_build is None or not _same_path(candidate, current_build))
                and candidate.is_dir()
                and exe is not None
            ):
                executable = {
                    "directory": str(candidate),
                    "executable": str(exe),
                    "executable_size": exe.stat().st_size,
                    "executable_sha256": _sha256(exe),
                }

        has_channels = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channels'"
        ).fetchone()
        original_channels = (
            [
                dict(row)
                for row in source.execute(
                    "SELECT slug,display_name,channel_number,enabled FROM channels ORDER BY id"
                )
            ]
            if has_channels
            else []
        )
        manifest = {
            "schema_version": 1,
            "feature_migration": FEATURE_MIGRATION,
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "application_version": __version__,
            "database_destination": str(self.config.database_path),
            "profile_destination": str(self.config.profile_path),
            "settings_destination": str(
                self.config.profile_path.with_suffix(".settings.json")
            ),
            "settings_files": copied_settings,
            "files": files,
            "previous_build": executable,
            "original_channels": original_channels,
            "python": sys.version,
        }
        temporary = self.root / "manifest.json.partial"
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.manifest_path)
        return {"created": True, **self.inventory()}

    def verify(self) -> dict[str, object]:
        if not self.manifest_path.is_file():
            raise RestorePointError("No internet-media restore point exists")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        for record in manifest.get("files", []):
            path = (self.root / str(record["path"])).resolve()
            if self.root not in path.parents:
                raise RestorePointError("Restore manifest contains an unsafe path")
            if not path.is_file():
                raise RestorePointError(f"Restore file is missing: {path}")
            if path.stat().st_size != int(record["size"]) or _sha256(path) != record["sha256"]:
                raise RestorePointError(f"Restore file failed verification: {path}")
        previous = manifest.get("previous_build")
        if previous:
            exe = Path(str(previous["executable"])).resolve()
            if not exe.is_file() or _sha256(exe) != previous["executable_sha256"]:
                raise RestorePointError("Previous portable build failed verification")
        return manifest

    def restore(self) -> RestoreResult:
        manifest = self.verify()
        destination = Path(str(manifest["database_destination"])).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        post_upgrade = None
        if destination.is_file():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            post_upgrade = destination.with_name(
                f"{destination.stem}.post-internet-media-{stamp}{destination.suffix}"
            )
            source = sqlite3.connect(destination)
            target = sqlite3.connect(post_upgrade)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()

        temporary = destination.with_name(destination.name + ".restore-partial")
        shutil.copy2(self.root / "database.sqlite3", temporary)
        os.replace(temporary, destination)

        restored_database = sqlite3.connect(destination)
        restored_database.row_factory = sqlite3.Row
        try:
            integrity = restored_database.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise RestorePointError("Restored database failed its integrity check")
            expected_channels = manifest.get("original_channels", [])
            if expected_channels:
                actual_channels = [
                    dict(row)
                    for row in restored_database.execute(
                        "SELECT slug,display_name,channel_number,enabled FROM channels ORDER BY id"
                    )
                ]
                if actual_channels != expected_channels:
                    raise RestorePointError("Restored database did not contain the original channels")
        finally:
            restored_database.close()

        restored: list[str] = []
        for stored_name, destination_key in (
            ("profile.toml", "profile_destination"),
            ("settings.json", "settings_destination"),
        ):
            stored = self.root / stored_name
            if stored.is_file():
                target_path = Path(str(manifest[destination_key])).resolve()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stored, target_path)
                restored.append(str(target_path))

        previous = manifest.get("previous_build")
        return RestoreResult(
            str(self.root),
            str(destination),
            restored,
            str(post_upgrade) if post_upgrade else None,
            str(previous["directory"]) if previous else None,
            str(self.config.storage_root("media")),
            str(
                (
                    self.config.storage_root("state")
                    / str(self.config.data.get("internet_media", {}).get("cache_directory", "internet-cache"))
                ).resolve()
            ),
        )
