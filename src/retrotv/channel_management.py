from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from retrotv.classification import resolve_channel_root, slugify
from retrotv.config import AppConfig
from retrotv.db import transaction, utc_now
from retrotv.internet_media import PreparationService


ACTIVE_PREPARATION_STATES = ("downloading", "validating", "analyzing", "normalizing")


class ChannelDeletionService:
    """Remove a channel's database state while leaving finished media in place."""

    def __init__(self, connection: sqlite3.Connection, config: AppConfig):
        self.connection = connection
        self.config = config

    def _channel(self, channel_slug: str):
        row = self.connection.execute(
            "SELECT id,slug,display_name,channel_number FROM channels WHERE slug=?",
            (slugify(channel_slug),),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown channel: {channel_slug}")
        return row

    def _source_paths(self, channel_id: int) -> list[Path]:
        media_root = self.config.storage_root("media").resolve()
        result: list[Path] = []
        for row in self.connection.execute(
            "SELECT source_relative_path FROM internet_sources WHERE channel_id=?",
            (channel_id,),
        ):
            path = (media_root / Path(str(row[0]))).resolve(strict=False)
            try:
                path.relative_to(media_root)
            except ValueError:
                continue
            if path.suffixes[-2:] == [".source", ".json"]:
                result.append(path)
        return sorted(set(result))

    def _job_rows(self, channel_id: int):
        return self.connection.execute(
            """SELECT j.id,j.state,j.temporary_path,j.final_path,e.local_path
               FROM internet_preparation_jobs j
               JOIN remote_episodes e ON e.id=j.episode_id
               JOIN internet_sources s ON s.id=e.source_id
               WHERE s.channel_id=?""",
            (channel_id,),
        ).fetchall()

    def preview(self, channel_slug: str) -> dict[str, object]:
        channel = self._channel(channel_slug)
        channel_id = int(channel["id"])
        assets = self.connection.execute(
            "SELECT COUNT(*) count FROM assets WHERE channel_slug=?", (channel["slug"],)
        ).fetchone()["count"]
        schedules = self.connection.execute(
            "SELECT COUNT(*) count FROM schedule_generations WHERE channel_id=?",
            (channel_id,),
        ).fetchone()["count"]
        sources = self.connection.execute(
            "SELECT COUNT(*) count FROM internet_sources WHERE channel_id=?",
            (channel_id,),
        ).fetchone()["count"]
        episodes = self.connection.execute(
            """SELECT COUNT(*) count FROM remote_episodes e
               JOIN internet_sources s ON s.id=e.source_id WHERE s.channel_id=?""",
            (channel_id,),
        ).fetchone()["count"]
        source_paths = self._source_paths(channel_id)
        jobs = self._job_rows(channel_id)
        incomplete_paths = {
            str(row["temporary_path"])
            for row in jobs
            if row["temporary_path"]
        }
        completed_paths = {
            str(row["local_path"] or row["final_path"])
            for row in jobs
            if row["local_path"] or row["final_path"]
        }
        channel_root = resolve_channel_root(
            self.config.storage_root("media"), str(channel["slug"])
        )
        return {
            "slug": str(channel["slug"]),
            "display_name": str(channel["display_name"]),
            "confirmation_text": f"DELETE {channel['display_name']}",
            "records": {
                "assets": int(assets),
                "schedule_generations": int(schedules),
                "internet_sources": int(sources),
                "remote_episodes": int(episodes),
                "preparation_jobs": len(jobs),
            },
            "source_files_to_delete": len(source_paths),
            "unfinished_files_to_delete": len(incomplete_paths),
            "finished_files_preserved": len(completed_paths),
            "channel_folder": str(channel_root),
            "scanner_will_recreate": channel_root.is_dir(),
        }

    def _request_cancellation(self, channel_id: int) -> None:
        now = utc_now()
        self.connection.execute(
            "UPDATE internet_sources SET enabled=0 WHERE channel_id=?", (channel_id,)
        )
        self.connection.execute(
            """UPDATE internet_preparation_jobs SET cancel_requested=1,updated_at=?
               WHERE episode_id IN (
                   SELECT e.id FROM remote_episodes e
                   JOIN internet_sources s ON s.id=e.source_id WHERE s.channel_id=?
               ) AND state IN ('downloading','validating','analyzing','normalizing')""",
            (now, channel_id),
        )
        self.connection.execute(
            """UPDATE internet_preparation_jobs
               SET state='cataloged',cancel_requested=0,retry_after=NULL,
                   error_detail='Preparation cancelled because channel was deleted',updated_at=?
               WHERE episode_id IN (
                   SELECT e.id FROM remote_episodes e
                   JOIN internet_sources s ON s.id=e.source_id WHERE s.channel_id=?
               ) AND state IN ('queued','retry','paused_space','failed')""",
            (now, channel_id),
        )
        self.connection.commit()

    def _wait_for_cancellation(self, channel_id: int, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        placeholders = ",".join("?" for _ in ACTIVE_PREPARATION_STATES)
        while True:
            count = self.connection.execute(
                f"""SELECT COUNT(*) count FROM internet_preparation_jobs j
                    JOIN remote_episodes e ON e.id=j.episode_id
                    JOIN internet_sources s ON s.id=e.source_id
                    WHERE s.channel_id=? AND j.state IN ({placeholders})""",
                (channel_id, *ACTIVE_PREPARATION_STATES),
            ).fetchone()["count"]
            if not count:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "A download or conversion is still stopping. Wait a moment and delete the channel again."
                )
            time.sleep(0.1)

    def _remove_incomplete_files(self, jobs) -> int:
        preparation = PreparationService(self.connection, self.config)
        removed = 0
        candidates: set[Path] = set()
        for row in jobs:
            if row["temporary_path"]:
                temporary = Path(str(row["temporary_path"]))
                candidates.add(temporary)
                candidates.add(temporary.with_name(temporary.name + ".compat.mp4"))
        for path in candidates:
            if preparation._is_managed_incomplete(path) and path.is_file():
                path.unlink()
                removed += 1
        return removed

    def delete(
        self,
        channel_slug: str,
        *,
        confirmation_text: str,
        acknowledge_rescan: bool,
        timeout_seconds: float = 45.0,
    ) -> dict[str, object]:
        channel = self._channel(channel_slug)
        required = f"DELETE {channel['display_name']}"
        if confirmation_text != required:
            raise ValueError(f"Type {required!r} exactly to delete this channel")
        if acknowledge_rescan is not True:
            raise ValueError(
                "Acknowledge that preserved media can make the scanner recreate the channel"
            )

        channel_id = int(channel["id"])
        source_paths = self._source_paths(channel_id)
        self._request_cancellation(channel_id)
        # Remove the source definitions immediately so a restart cannot
        # re-enable preparation while an active worker is winding down.
        for path in source_paths:
            path.unlink(missing_ok=True)
        self._wait_for_cancellation(channel_id, timeout_seconds)
        jobs = self._job_rows(channel_id)
        removed_incomplete = self._remove_incomplete_files(jobs)

        channel_slug = str(channel["slug"])
        asset_ids = [
            int(row[0])
            for row in self.connection.execute(
                "SELECT id FROM assets WHERE channel_slug=?", (channel_slug,)
            )
        ]
        with transaction(self.connection):
            self.connection.execute(
                "DELETE FROM schedule_entries WHERE channel_id=?", (channel_id,)
            )
            self.connection.execute(
                "DELETE FROM schedule_generations WHERE channel_id=?", (channel_id,)
            )
            if asset_ids:
                placeholders = ",".join("?" for _ in asset_ids)
                self.connection.execute(
                    f"DELETE FROM schedule_entries WHERE asset_id IN ({placeholders})",
                    asset_ids,
                )
                self.connection.execute(
                    f"DELETE FROM normalization_promotions WHERE asset_id IN ({placeholders})",
                    asset_ids,
                )
                self.connection.execute(
                    f"DELETE FROM audio_normalization_promotions WHERE asset_id IN ({placeholders})",
                    asset_ids,
                )
                self.connection.execute(
                    f"UPDATE shadow_files SET target_asset_id=NULL WHERE target_asset_id IN ({placeholders})",
                    asset_ids,
                )
                self.connection.execute(
                    f"UPDATE content_normalization_recipes SET source_asset_id=NULL WHERE source_asset_id IN ({placeholders})",
                    asset_ids,
                )
                self.connection.execute(
                    f"DELETE FROM assets WHERE id IN ({placeholders})", asset_ids
                )
            self.connection.execute(
                "DELETE FROM asset_eligibility WHERE channel_slug=?", (channel_slug,)
            )
            self.connection.execute(
                "DELETE FROM shadow_eligibility WHERE channel_slug=?", (channel_slug,)
            )
            self.connection.execute(
                "DELETE FROM shadow_files WHERE channel_slug=?", (channel_slug,)
            )
            collection_ids = [
                int(row[0])
                for row in self.connection.execute(
                    "SELECT id FROM programming_collections WHERE channel_slug=?",
                    (channel_slug,),
                )
            ]
            if collection_ids:
                placeholders = ",".join("?" for _ in collection_ids)
                self.connection.execute(
                    f"DELETE FROM programming_collections WHERE id IN ({placeholders})",
                    collection_ids,
                )
            self.connection.execute(
                "DELETE FROM calendar_rules WHERE channel_slug=?", (channel_slug,)
            )
            self.connection.execute("DELETE FROM channels WHERE id=?", (channel_id,))

        issue = self.connection.execute("PRAGMA foreign_key_check").fetchone()
        if issue is not None:
            raise RuntimeError("Channel deletion left an invalid database reference")
        channel_root = resolve_channel_root(
            self.config.storage_root("media"), channel_slug
        )
        return {
            "ok": True,
            "deleted": channel_slug,
            "source_files_deleted": len(source_paths),
            "unfinished_files_deleted": removed_incomplete,
            "media_preserved": True,
            "scanner_will_recreate": channel_root.is_dir(),
            "channel_folder": str(channel_root),
        }
