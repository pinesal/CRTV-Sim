from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def apply_migrations(connection: sqlite3.Connection, migration_dir: Path) -> list[str]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row["migration_name"]
        for row in connection.execute(
            "SELECT migration_name FROM schema_migrations"
        )
    }
    newly_applied: list[str] = []
    for migration in sorted(migration_dir.glob("*.sql")):
        if migration.name in applied:
            continue
        sql = migration.read_text(encoding="utf-8")
        try:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(migration_name, applied_at) VALUES (?, ?)",
                (migration.name, utc_now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        newly_applied.append(migration.name)
    return newly_applied


def ensure_storage_root(
    connection: sqlite3.Connection,
    logical_name: str,
    *,
    storage_type: str = "local",
) -> int:
    connection.execute(
        """
        INSERT INTO storage_roots(logical_name, storage_type, enabled)
        VALUES (?, ?, 1)
        ON CONFLICT(logical_name) DO UPDATE SET
            storage_type = excluded.storage_type,
            enabled = 1
        """,
        (logical_name, storage_type),
    )
    row = connection.execute(
        "SELECT id FROM storage_roots WHERE logical_name = ?",
        (logical_name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Unable to create storage root: {logical_name}")
    return int(row["id"])


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise

