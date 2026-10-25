from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from retrotv.database_backup import DatabaseBackupService
from retrotv.db import utc_now


@dataclass
class MaintenanceResult:
    state: str = "complete"
    started_at: str | None = None
    completed_at: str | None = None
    trigger: str = "automatic"
    database_size_before: int = 0
    database_size_after: int = 0
    reclaimable_bytes: int = 0
    rows_removed: dict[str, int] = field(default_factory=dict)
    backup: dict[str, object] | None = None
    reason: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DatabaseHousekeepingService:
    """Bound database history while preserving scheduling and user state."""

    def __init__(self, connection: sqlite3.Connection, config) -> None:
        self.connection = connection
        self.config = config
        self.settings = config.data.get("database_housekeeping", {})

    def due(self, *, now: datetime | None = None) -> bool:
        current = self._now(now)
        row = self.connection.execute(
            """SELECT completed_at FROM maintenance_runs
               WHERE run_type='housekeeping' AND state='complete'
               ORDER BY completed_at DESC LIMIT 1"""
        ).fetchone()
        if row is None or not row["completed_at"]:
            return True
        try:
            completed = datetime.fromisoformat(str(row["completed_at"]))
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=UTC)
            interval = max(1.0, float(self.settings.get("minimum_interval_hours", 24)))
            if interval <= 24 and current.date() != completed.astimezone(current.tzinfo).date():
                return True
            return current.astimezone(UTC) - completed.astimezone(UTC) >= timedelta(hours=interval)
        except ValueError:
            return True

    def run(
        self, *, trigger: str = "automatic", force: bool = False,
        now: datetime | None = None,
    ) -> MaintenanceResult:
        current = self._now(now)
        result = MaintenanceResult(
            started_at=current.isoformat(timespec="seconds"),
            trigger=trigger,
            database_size_before=self._database_size(),
        )
        if not force and not bool(self.settings.get("enabled", True)):
            result.state = "skipped"
            result.completed_at = current.isoformat(timespec="seconds")
            result.reason = "Automatic database housekeeping is disabled"
            return result
        if not force and not self.due(now=current):
            result.state = "skipped"
            result.completed_at = current.isoformat(timespec="seconds")
            result.reason = "Database housekeeping is not due yet"
            return result

        run_id = self._begin_run(trigger, result.started_at)
        try:
            # Force a fresh, verified snapshot. A same-day backup is atomically
            # replaced only after the new copy passes its integrity checks.
            backup = DatabaseBackupService(self.config).create(
                self.connection, force=True, now=current
            )
            result.backup = backup.as_dict()
            result.rows_removed = self._purge(current)
            result.database_size_after = self._database_size()
            result.reclaimable_bytes = self._reclaimable_bytes()
            result.completed_at = self._now(None).isoformat(timespec="seconds")
            self._finish_run(run_id, "complete", result)
        except Exception as exc:
            self.connection.rollback()
            result.state = "failed"
            result.error = str(exc)
            result.completed_at = self._now(None).isoformat(timespec="seconds")
            self._finish_run(run_id, "failed", result)
        return result

    def _purge(self, current: datetime) -> dict[str, int]:
        schedule_days = max(1, int(self.settings.get("schedule_retention_days", 30)))
        operation_days = max(1, int(self.settings.get("operation_retention_days", 30)))
        missing_days = max(1, int(self.settings.get("missing_asset_retention_days", 90)))
        schedule_date = (current.date() - timedelta(days=schedule_days)).isoformat()
        operation_cutoff = (current.astimezone(UTC) - timedelta(days=operation_days)).isoformat()
        missing_cutoff = (current.astimezone(UTC) - timedelta(days=missing_days)).isoformat()
        removed: dict[str, int] = {}

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._checkpoint_schedule_history(schedule_date)

            generation_ids = [
                int(row[0]) for row in self.connection.execute(
                    """SELECT id FROM schedule_generations
                       WHERE schedule_date < ? AND locked=0""",
                    (schedule_date,),
                )
            ]
            if generation_ids:
                marks = ",".join("?" for _ in generation_ids)
                removed["schedule_entries"] = int(self.connection.execute(
                    f"SELECT COUNT(*) FROM schedule_entries WHERE generation_id IN ({marks})",
                    generation_ids,
                ).fetchone()[0])
                cursor = self.connection.execute(
                    f"DELETE FROM schedule_generations WHERE id IN ({marks})",
                    generation_ids,
                )
                removed["schedule_generations"] = max(0, cursor.rowcount)
            else:
                removed["schedule_entries"] = 0
                removed["schedule_generations"] = 0

            # Old scan rows remain referenced by the current asset record unless
            # those references are detached first.
            self.connection.execute(
                "UPDATE assets SET last_scan_run_id=NULL WHERE last_scan_run_id IN "
                "(SELECT id FROM scan_runs WHERE COALESCE(completed_at,started_at) < ?)",
                (operation_cutoff,),
            )
            self.connection.execute(
                "UPDATE integrity_scans SET scan_run_id=NULL WHERE scan_run_id IN "
                "(SELECT id FROM scan_runs WHERE COALESCE(completed_at,started_at) < ?)",
                (operation_cutoff,),
            )
            removed["scan_runs"] = self._delete_count(
                "DELETE FROM scan_runs WHERE COALESCE(completed_at,started_at) < ?",
                (operation_cutoff,),
            )
            removed["resolved_reviews"] = self._delete_count(
                "DELETE FROM review_items WHERE status <> 'open' AND resolved_at IS NOT NULL AND resolved_at < ?",
                (operation_cutoff,),
            )
            removed["completed_preparation_jobs"] = self._delete_count(
                """DELETE FROM internet_preparation_jobs
                   WHERE state='completed' AND completed_at IS NOT NULL AND completed_at < ?""",
                (operation_cutoff,),
            )

            # Promotion rows are deliberately a protection condition: their
            # hashes and backup paths may still be needed for recovery.
            removed["missing_assets"] = self._delete_count(
                """DELETE FROM assets
                   WHERE integrity_status='missing' AND review_status='ignored'
                     AND source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed')
                     AND last_seen_at < ?
                     AND NOT EXISTS (SELECT 1 FROM schedule_entries se WHERE se.asset_id=assets.id)
                     AND NOT EXISTS (SELECT 1 FROM remote_episodes re WHERE re.asset_id=assets.id)
                     AND NOT EXISTS (SELECT 1 FROM normalization_promotions np WHERE np.asset_id=assets.id)
                     AND NOT EXISTS (SELECT 1 FROM audio_normalization_promotions ap WHERE ap.asset_id=assets.id)
                     AND NOT EXISTS (SELECT 1 FROM review_items ri WHERE ri.asset_id=assets.id AND ri.status='open')""",
                (missing_cutoff,),
            )
            removed["orphaned_eligibility"] = self._delete_count(
                "DELETE FROM asset_eligibility WHERE NOT EXISTS "
                "(SELECT 1 FROM assets a WHERE a.id=asset_eligibility.asset_id)"
            )

            # Bound the housekeeping audit itself without deleting the current run.
            removed["maintenance_runs"] = self._delete_count(
                """DELETE FROM maintenance_runs
                   WHERE id <> ? AND completed_at IS NOT NULL AND completed_at < ?""",
                (self._active_run_id, operation_cutoff),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return removed

    def _checkpoint_schedule_history(self, cutoff: str) -> None:
        now = utc_now()
        recurring = self.connection.execute(
            """SELECT r.channel_id,r.schedule_date,r.state_json,r.state_hash
               FROM recurring_lineup_snapshots r
               JOIN schedule_generations sg ON sg.id=r.generation_id
               JOIN (
                   SELECT channel_id,schedule_date,MAX(id) AS generation_id
                   FROM schedule_generations
                   WHERE status='active' AND schedule_date<?
                   GROUP BY channel_id,schedule_date
               ) latest ON latest.generation_id=sg.id
               WHERE r.schedule_date<?
                 AND NOT EXISTS (
                     SELECT 1 FROM schedule_generations newer
                     JOIN recurring_lineup_snapshots nr ON nr.generation_id=newer.id
                     WHERE newer.channel_id=r.channel_id
                       AND newer.status='active'
                       AND newer.schedule_date>r.schedule_date
                       AND newer.schedule_date<?
                 )
               ORDER BY r.channel_id,r.schedule_date DESC""",
            (cutoff, cutoff, cutoff),
        ).fetchall()
        for row in recurring:
            self.connection.execute(
                """INSERT INTO recurring_lineup_checkpoints(
                       channel_id,checkpoint_date,state_json,state_hash,updated_at
                   ) VALUES (?,?,?,?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET
                       checkpoint_date=excluded.checkpoint_date,
                       state_json=excluded.state_json,
                       state_hash=excluded.state_hash,
                       updated_at=excluded.updated_at
                   WHERE excluded.checkpoint_date > recurring_lineup_checkpoints.checkpoint_date""",
                (
                    row["channel_id"],row["schedule_date"],row["state_json"],
                    row["state_hash"],now,
                ),
            )
        series = self.connection.execute(
            """SELECT se.channel_id,
                      COALESCE(a.series_title,a.display_title,a.original_filename) AS context_key,
                      se.asset_id,MAX(se.ends_at) AS last_used_at
               FROM schedule_entries se
               JOIN schedule_generations sg ON sg.id=se.generation_id
               JOIN assets a ON a.id=se.asset_id
               WHERE sg.schedule_date < ? AND sg.locked=0 AND se.is_program=1
               GROUP BY se.channel_id,context_key""",
            (cutoff,),
        ).fetchall()
        blocks = self.connection.execute(
            """SELECT se.channel_id,CAST(se.asset_id AS TEXT) AS context_key,
                      se.asset_id,MAX(se.starts_at) AS last_used_at
               FROM schedule_entries se
               JOIN schedule_generations sg ON sg.id=se.generation_id
               WHERE sg.schedule_date < ? AND sg.locked=0
                 AND se.protected_block=1 AND se.asset_id IS NOT NULL
               GROUP BY se.channel_id,se.asset_id""",
            (cutoff,),
        ).fetchall()
        # Legacy unscoped checkpoints remain readable by older clients. New
        # base-only checkpoints must not inherit a weekly shadow's last episode.
        base_series = self.connection.execute(
            """SELECT se.channel_id,'base:' || COALESCE(json_extract(se.explanation_json,'$.selection.series'),
                      a.series_title,a.display_title,a.original_filename) AS context_key,
                      se.asset_id,MAX(se.ends_at) AS last_used_at
               FROM schedule_entries se JOIN schedule_generations sg ON sg.id=se.generation_id
               JOIN assets a ON a.id=se.asset_id
               WHERE sg.schedule_date<? AND sg.status='active' AND se.is_program=1
                 AND COALESCE(se.programming_owner,'base')='base'
                 AND COALESCE(CASE WHEN json_type(se.explanation_json,'$.selection.collection_type') IS NOT NULL
                     THEN json_extract(se.explanation_json,'$.selection.collection_type') ELSE a.collection_type END,'')=''
               GROUP BY se.channel_id,context_key""", (cutoff,),
        ).fetchall()
        for kind, rows in (("series", series), ("series", base_series), ("block", blocks)):
            for row in rows:
                self.connection.execute(
                    """INSERT INTO schedule_history_checkpoints(
                           channel_id,context_type,context_key,asset_id,last_used_at,updated_at
                       ) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(channel_id,context_type,context_key) DO UPDATE SET
                           asset_id=excluded.asset_id,last_used_at=excluded.last_used_at,
                           updated_at=excluded.updated_at
                       WHERE excluded.last_used_at > schedule_history_checkpoints.last_used_at""",
                    (row["channel_id"], kind, row["context_key"], row["asset_id"], row["last_used_at"], now),
                )

    def _begin_run(self, trigger: str, started_at: str | None) -> int:
        cursor = self.connection.execute(
            """INSERT INTO maintenance_runs(run_type,trigger_kind,state,started_at)
               VALUES ('housekeeping',?,'running',?)""",
            (trigger, started_at or utc_now()),
        )
        self.connection.commit()
        self._active_run_id = int(cursor.lastrowid)
        return self._active_run_id

    def _finish_run(self, run_id: int, state: str, result: MaintenanceResult) -> None:
        self.connection.execute(
            """UPDATE maintenance_runs SET state=?,completed_at=?,detail_json=?,error=?
               WHERE id=?""",
            (state, result.completed_at, json.dumps(result.as_dict(), sort_keys=True), result.error, run_id),
        )
        self.connection.commit()

    def _delete_count(self, sql: str, parameters: tuple[Any, ...] = ()) -> int:
        cursor = self.connection.execute(sql, parameters)
        return max(0, int(cursor.rowcount))

    def _database_size(self) -> int:
        path = Path(self.config.database_path)
        return sum(
            candidate.stat().st_size for candidate in (
                path, Path(str(path) + "-wal"), Path(str(path) + "-shm")
            ) if candidate.is_file()
        )

    def _reclaimable_bytes(self) -> int:
        page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
        free_pages = int(self.connection.execute("PRAGMA freelist_count").fetchone()[0])
        return page_size * free_pages

    def _now(self, supplied: datetime | None) -> datetime:
        if supplied is not None:
            return supplied if supplied.tzinfo else supplied.replace(tzinfo=UTC)
        name = str(self.config.data.get("application", {}).get("timezone", "UTC"))
        try:
            return datetime.now(ZoneInfo(name))
        except Exception:
            return datetime.now().astimezone()


class DatabaseCompactionService:
    def __init__(self, config) -> None:
        self.config = config

    def preview(self) -> dict[str, int]:
        connection = sqlite3.connect(self.config.database_path, timeout=30)
        try:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            return {
                "database_size_bytes": Path(self.config.database_path).stat().st_size,
                "reclaimable_bytes": page_size * free_pages,
                "estimated_size_after": max(page_size, (page_count - free_pages) * page_size),
            }
        finally:
            connection.close()

    def run(self, *, trigger: str = "manual") -> dict[str, object]:
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        connection = sqlite3.connect(self.config.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            backup = DatabaseBackupService(self.config).create(connection, force=True).as_dict()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise RuntimeError("SQLite could not finish its compaction checkpoint")
            check = connection.execute("PRAGMA integrity_check").fetchall()
            if len(check) != 1 or str(check[0][0]).casefold() != "ok":
                raise RuntimeError("Database integrity check failed before compaction")
            before = Path(self.config.database_path).stat().st_size
            connection.execute("VACUUM")
            connection.execute("PRAGMA optimize")
            after = Path(self.config.database_path).stat().st_size
            completed = datetime.now().astimezone().isoformat(timespec="seconds")
            detail = {
                "state": "complete", "trigger": trigger, "started_at": started,
                "completed_at": completed, "database_size_before": before,
                "database_size_after": after, "bytes_reclaimed": max(0, before - after),
                "backup": backup, "error": None,
            }
            self._record(connection, "complete", trigger, started, completed, detail, None)
            return detail
        except Exception as exc:
            detail = {
                "state": "failed", "trigger": trigger, "started_at": started,
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": str(exc),
            }
            try:
                self._record(connection, "failed", trigger, started, detail["completed_at"], detail, str(exc))
            except sqlite3.Error:
                pass
            return detail
        finally:
            connection.close()

    @staticmethod
    def _record(connection, state, trigger, started, completed, detail, error) -> None:
        connection.execute(
            """INSERT INTO maintenance_runs(
                   run_type,trigger_kind,state,started_at,completed_at,detail_json,error
               ) VALUES ('compaction',?,?,?,?,?,?)""",
            (trigger, state, started, completed, json.dumps(detail, sort_keys=True), error),
        )
        connection.commit()
