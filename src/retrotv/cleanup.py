from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
import time

from retrotv.db import utc_now
from retrotv.promotion import sha256_file


@dataclass
class CleanupSummary:
    skipped: bool = False
    reason: str | None = None
    files_removed: int = 0
    bytes_removed: int = 0
    files_examined: int = 0
    previews_removed: int = 0
    backups_removed: int = 0
    backups_retained: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class GeneratedAssetCleanup:
    """Prune reproducible support media and expired verified recovery copies."""

    def __init__(self, connection, config):
        self.connection = connection
        self.config = config
        self.settings = config.data.get("generated_cleanup", {})
        self.state_generated = config.storage_root("state") / "generated"

    def run(self, *, force: bool = False, now: float | None = None) -> CleanupSummary:
        summary = CleanupSummary()
        if not force and not bool(self.settings.get("enabled", True)):
            summary.skipped = True
            summary.reason = "Automatic generated-file cleanup is disabled"
            return summary

        moment = time.time() if now is None else float(now)
        marker = self.state_generated / ".cleanup-marker"
        interval_hours = max(
            1.0, float(self.settings.get("minimum_interval_hours", 24))
        )
        if (
            not force
            and marker.is_file()
            and moment - marker.stat().st_mtime < interval_hours * 3600
        ):
            summary.skipped = True
            summary.reason = "Generated-file cleanup is not due yet"
            return summary

        protected = self._protected_paths(moment)
        temporary_hours = max(
            1.0, float(self.settings.get("temporary_retention_hours", 1))
        )
        self._prune_temporary_files(
            summary, protected, moment, temporary_hours * 3600
        )
        self._prune_guides(summary, protected)
        self._prune_announcements(summary, protected, moment)
        self._prune_graphics(summary, protected, moment)
        self._prune_continuity(summary, protected, moment)
        self._prune_aspect_previews(summary, protected, moment)
        self._prune_normalization_backups(summary, protected, moment)

        if not summary.errors:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        return summary

    def _protected_paths(self, now: float) -> set[Path]:
        protected: set[Path] = set()
        guide = self.connection.execute(
            """
            SELECT output_relative_path FROM guide_renders
            WHERE status='valid'
            ORDER BY activated_at DESC, generated_at DESC LIMIT 1
            """
        ).fetchone()
        if guide is not None and guide["output_relative_path"]:
            protected.add(self._stored_path(str(guide["output_relative_path"])))

        rows = self.connection.execute(
            """
            SELECT ao.fallback_path, ao.validated_output_path, ao.ends_at
            FROM announcement_opportunities ao
            JOIN schedule_generations sg ON sg.id=ao.generation_id
            WHERE sg.status='active'
            """
        ).fetchall()
        for row in rows:
            try:
                end = datetime.fromisoformat(row["ends_at"])
                if end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                if end < datetime.fromtimestamp(now, UTC) - timedelta(minutes=5):
                    continue
            except (TypeError, ValueError):
                continue
            for column in ("fallback_path", "validated_output_path"):
                if row[column]:
                    protected.add(self._stored_path(str(row[column])))

        guide_output = self._configured_path(
            self.config.data.get("guide", {}).get(
                "output", "./data/generated/guide.mp4"
            )
        )
        protected.add(guide_output)
        return {path.resolve() for path in protected}

    def _prune_temporary_files(
        self,
        summary: CleanupSummary,
        protected: set[Path],
        now: float,
        maximum_age: float,
    ) -> None:
        directories = self._output_directories()
        patterns = ("*.partial.mp4", ".*.tmp", ".guide-*.ass", ".guide-*-final.mp4")
        seen: set[Path] = set()
        for directory in directories:
            if not directory.is_dir():
                continue
            for pattern in patterns:
                for candidate in directory.rglob(pattern):
                    resolved = candidate.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    self._remove_if_old(
                        candidate, summary, protected, now, maximum_age
                    )

    def _prune_guides(
        self, summary: CleanupSummary, protected: set[Path]
    ) -> None:
        output = self._configured_path(
            self.config.data.get("guide", {}).get(
                "output", "./data/generated/guide.mp4"
            )
        )
        keep = max(1, int(self.settings.get("guide_versions_to_keep", 3)))
        versions = sorted(
            output.parent.glob(f"{output.stem}.runtime-*{output.suffix}"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for candidate in versions[keep:]:
            self._remove(candidate, summary, protected)

    def _prune_announcements(
        self, summary: CleanupSummary, protected: set[Path], now: float
    ) -> None:
        output = self._configured_path(
            self.config.data.get("announcements", {}).get(
                "output_dir", "./data/generated/announcements"
            )
        )
        if not output.is_dir():
            return
        silent_age = max(
            1.0, float(self.settings.get("silent_retention_days", 7))
        ) * 86400
        narrated_age = max(
            1.0, float(self.settings.get("narrated_retention_days", 30))
        ) * 86400
        for candidate in output.glob("*.mp4"):
            if not candidate.resolve().is_relative_to(output.resolve()):
                continue
            maximum_age = (86400 if candidate.name.startswith("opportunity-") else
                           silent_age if candidate.stem.endswith("-silent") else narrated_age)
            if self._remove_if_old(
                candidate, summary, protected, now, maximum_age
            ):
                self._clear_announcement_reference(candidate)
        generic = output / "generic"
        if generic.is_dir():
            active = {
                Path(row["audio_path"]).resolve()
                for row in self.connection.execute(
                    "SELECT audio_path FROM announcement_generic_audio WHERE status='ready' AND audio_path IS NOT NULL"
                )
            }
            for candidate in generic.glob("*.wav"):
                if candidate.resolve() in active:
                    continue
                self._remove_if_old(
                    candidate, summary, protected, now, narrated_age
                )
        test_output = output / "tests"
        if test_output.is_dir():
            test_age = max(
                1.0, float(self.settings.get("temporary_retention_hours", 1))
            ) * 3600
            for candidate in test_output.glob("announcer-test-*.wav"):
                self._remove_if_old(candidate, summary, protected, now, test_age)

    def _prune_graphics(
        self, summary: CleanupSummary, protected: set[Path], now: float
    ) -> None:
        output = self._configured_path(
            self.config.data.get("graphics", {}).get(
                "output_dir", "./data/generated/graphics"
            )
        )
        if not output.is_dir():
            return
        maximum_age = max(
            1.0, float(self.settings.get("graphics_retention_days", 14))
        ) * 86400
        for candidate in output.glob("graphic-*.mp4"):
            self._remove_if_old(candidate, summary, protected, now, maximum_age)

    def _prune_continuity(
        self, summary: CleanupSummary, protected: set[Path], now: float
    ) -> None:
        if not self.state_generated.is_dir():
            return
        candidates = sorted(
            self.state_generated.glob("continuity*.mp4"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        retention = max(
            1.0, float(self.settings.get("obsolete_continuity_retention_days", 1))
        ) * 86400
        for candidate in candidates[1:]:
            self._remove_if_old(candidate, summary, protected, now, retention)

    def _prune_aspect_previews(
        self, summary: CleanupSummary, protected: set[Path], now: float
    ) -> None:
        preview_root = self.config.storage_root("previews").resolve()
        if not preview_root.is_dir():
            return
        maximum_age = max(
            1.0, float(self.settings.get("aspect_preview_retention_days", 7))
        ) * 86400
        open_review_ids = {
            int(row["asset_id"])
            for row in self.connection.execute(
                """
                SELECT asset_id FROM normalization_profiles
                WHERE analysis_status='review'
                """
            ).fetchall()
        }
        for pattern in (
            "*-source.jpg",
            "*-source-*.jpg",
            "*-normalized.jpg",
            "*-normalized-*.jpg",
            "*-normalized.preview.json",
            "*.partial.jpg",
            "*.json.partial",
        ):
            for candidate in preview_root.glob(pattern):
                try:
                    asset_id = int(candidate.name.split("-", 1)[0])
                except ValueError:
                    asset_id = -1
                # An open review needs its inline comparison to remain available.
                # Approval removes these files immediately, so retaining them here
                # cannot create permanent preview growth.
                if asset_id in open_review_ids:
                    continue
                if self._remove_if_old(
                    candidate, summary, protected, now, maximum_age
                ):
                    if candidate.suffix.lower() == ".jpg":
                        summary.previews_removed += 1
                    relative = candidate.relative_to(preview_root).as_posix()
                    self.connection.execute(
                        """
                        UPDATE normalization_profiles
                        SET source_preview_relative_path = CASE
                                WHEN source_preview_relative_path = ? THEN NULL
                                ELSE source_preview_relative_path END,
                            normalized_preview_relative_path = CASE
                                WHEN normalized_preview_relative_path = ? THEN NULL
                                ELSE normalized_preview_relative_path END
                        WHERE source_preview_relative_path = ?
                           OR normalized_preview_relative_path = ?
                        """,
                        (relative, relative, relative, relative),
                    )
        self.connection.commit()

    def _prune_normalization_backups(
        self, summary: CleanupSummary, protected: set[Path], now: float
    ) -> None:
        if not bool(self.settings.get("delete_normalization_backups", True)):
            return
        retention = max(
            1.0,
            float(self.settings.get("normalization_backup_retention_days", 30)),
        ) * 86400
        backup_root = self.config.storage_root("originals_backup").resolve()
        media_root = self.config.storage_root("media").resolve()
        candidates: list[tuple[str, object]] = []
        for table in (
            "normalization_promotions",
            "audio_normalization_promotions",
        ):
            rows = self.connection.execute(
                f"""
                SELECT p.id, p.asset_id, p.backup_relative_path,
                       p.corrected_sha256, p.created_at,
                       a.source_relative_path, r.logical_name
                FROM {table} p
                JOIN assets a ON a.id = p.asset_id
                JOIN storage_roots r ON r.id = p.backup_root_id
                WHERE p.backup_deleted_at IS NULL
                ORDER BY p.created_at
                """
            ).fetchall()
            candidates.extend((table, row) for row in rows)

        current_hashes: dict[int, str | None] = {}
        reachable_hashes: dict[int, dict[str, set[str]]] = {}
        for table, row in candidates:
            summary.files_examined += 1
            try:
                created = datetime.fromisoformat(str(row["created_at"])).timestamp()
            except (TypeError, ValueError):
                summary.backups_retained += 1
                continue
            if now - created < retention:
                continue
            if str(row["logical_name"]) != "originals_backup":
                summary.backups_retained += 1
                continue

            asset_id = int(row["asset_id"])
            try:
                backup = (backup_root / Path(row["backup_relative_path"])).resolve()
                backup.relative_to(backup_root)
                current = (media_root / Path(row["source_relative_path"])).resolve()
                current.relative_to(media_root)
            except (OSError, ValueError, TypeError) as exc:
                summary.errors.append(f"Backup record {table} {row['id']}: {exc}")
                continue

            if not backup.exists():
                self._mark_backup_path_deleted(str(row["backup_relative_path"]))
                continue
            if not backup.is_file() or not current.is_file():
                summary.backups_retained += 1
                continue
            if not self._backup_path_is_expired(
                str(row["backup_relative_path"]), now, retention
            ):
                summary.backups_retained += 1
                continue

            if asset_id not in current_hashes:
                try:
                    current_hashes[asset_id] = sha256_file(current)
                except OSError as exc:
                    summary.errors.append(f"{current}: {exc}")
                    current_hashes[asset_id] = None
            current_hash = current_hashes[asset_id]
            if current_hash is None:
                summary.backups_retained += 1
                continue
            if asset_id not in reachable_hashes:
                reachable_hashes[asset_id] = self._promotion_hash_graph(asset_id)
            if not self._hash_is_reachable(
                str(row["corrected_sha256"]),
                current_hash,
                reachable_hashes[asset_id],
            ):
                summary.backups_retained += 1
                continue

            if self._remove(backup, summary, protected):
                summary.backups_removed += 1
                self._mark_backup_path_deleted(str(row["backup_relative_path"]))
        self.connection.commit()

    def _promotion_hash_graph(self, asset_id: int) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {}
        for table in (
            "normalization_promotions",
            "audio_normalization_promotions",
        ):
            rows = self.connection.execute(
                f"SELECT original_sha256, corrected_sha256 FROM {table} WHERE asset_id=?",
                (asset_id,),
            ).fetchall()
            for row in rows:
                graph.setdefault(str(row["original_sha256"]), set()).add(
                    str(row["corrected_sha256"])
                )
        return graph

    @staticmethod
    def _hash_is_reachable(
        starting_hash: str, current_hash: str, graph: dict[str, set[str]]
    ) -> bool:
        pending = [starting_hash]
        visited: set[str] = set()
        while pending:
            value = pending.pop()
            if value == current_hash:
                return True
            if value in visited:
                continue
            visited.add(value)
            pending.extend(graph.get(value, ()))
        return False

    def _backup_path_is_expired(
        self, relative_path: str, now: float, retention: float
    ) -> bool:
        for table in (
            "normalization_promotions",
            "audio_normalization_promotions",
        ):
            rows = self.connection.execute(
                f"""
                SELECT created_at FROM {table}
                WHERE backup_relative_path=? AND backup_deleted_at IS NULL
                """,
                (relative_path,),
            ).fetchall()
            for row in rows:
                try:
                    created = datetime.fromisoformat(
                        str(row["created_at"])
                    ).timestamp()
                except (TypeError, ValueError):
                    return False
                if now - created < retention:
                    return False
        return True

    def _mark_backup_path_deleted(self, relative_path: str) -> None:
        deleted_at = utc_now()
        for table in (
            "normalization_promotions",
            "audio_normalization_promotions",
        ):
            self.connection.execute(
                f"""
                UPDATE {table} SET backup_deleted_at=?
                WHERE backup_relative_path=? AND backup_deleted_at IS NULL
                """,
                (deleted_at, relative_path),
            )

    def _remove_if_old(
        self,
        candidate: Path,
        summary: CleanupSummary,
        protected: set[Path],
        now: float,
        maximum_age: float,
    ) -> bool:
        if not candidate.is_file():
            return False
        summary.files_examined += 1
        try:
            if now - candidate.stat().st_mtime < maximum_age:
                return False
        except OSError as exc:
            summary.errors.append(f"{candidate}: {exc}")
            return False
        return self._remove(candidate, summary, protected)

    @staticmethod
    def _remove(
        candidate: Path, summary: CleanupSummary, protected: set[Path]
    ) -> bool:
        resolved = candidate.resolve()
        if resolved in protected or not candidate.is_file():
            return False
        try:
            size = candidate.stat().st_size
            candidate.unlink()
        except OSError as exc:
            summary.errors.append(f"{candidate}: {exc}")
            return False
        summary.files_removed += 1
        summary.bytes_removed += size
        return True

    def _clear_announcement_reference(self, path: Path) -> None:
        values = {str(path), str(path.resolve())}
        for value in values:
            self.connection.execute(
                "UPDATE announcement_opportunities SET fallback_path=NULL WHERE fallback_path=?",
                (value,),
            )
            self.connection.execute(
                "UPDATE announcement_opportunities SET validated_output_path=NULL WHERE validated_output_path=?",
                (value,),
            )
            self.connection.execute(
                "UPDATE announcement_jobs SET output_path=NULL WHERE output_path=?",
                (value,),
            )
        self.connection.commit()

    def _output_directories(self) -> tuple[Path, ...]:
        values = {
            self.state_generated.resolve(),
            self._configured_path(
                self.config.data.get("guide", {}).get(
                    "output", "./data/generated/guide.mp4"
                )
            ).parent.resolve(),
            self._configured_path(
                self.config.data.get("announcements", {}).get(
                    "output_dir", "./data/generated/announcements"
                )
            ).resolve(),
            self._configured_path(
                self.config.data.get("graphics", {}).get(
                    "output_dir", "./data/generated/graphics"
                )
            ).resolve(),
        }
        return tuple(values)

    def _stored_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.config.project_root / path

    def _configured_path(self, value: object) -> Path:
        return self._stored_path(str(value)).resolve()
