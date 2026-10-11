from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from retrotv.config import load_config  # noqa: E402
from retrotv.db import apply_migrations, connect_database, transaction, utc_now  # noqa: E402
from retrotv.promotion import PromotionService, sha256_file  # noqa: E402


@dataclass(frozen=True)
class Collision:
    restored_asset_id: int
    promoted_asset_id: int
    source_relative_path: Path
    corrected_relative_path: Path
    source_path: Path
    corrected_path: Path
    parked_path: Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Repair an exact restored-original/previously-promoted MP4 collision. "
            "The command refuses any pair whose files do not match the recorded hashes."
        )
    )
    result.add_argument("--config", required=True)
    result.add_argument("--asset", type=int, action="append", required=True)
    result.add_argument(
        "--apply",
        action="store_true",
        help="Perform the repair; without this flag the command only validates.",
    )
    return result


def discover(connection, config, asset_ids: list[int], token: str) -> list[Collision]:
    media_root = config.storage_root("media").resolve()
    normalized_root = config.storage_root("normalized").resolve()
    collisions: list[Collision] = []
    for asset_id in dict.fromkeys(asset_ids):
        restored = connection.execute(
            """
            SELECT a.*, n.proxy_status, n.proxy_relative_path
            FROM assets a
            JOIN normalization_profiles n ON n.asset_id = a.id
            WHERE a.id = ?
            """,
            (asset_id,),
        ).fetchone()
        if restored is None:
            raise ValueError(f"Asset {asset_id} does not exist or has no normalization profile")
        if restored["proxy_status"] != "ready" or not restored["proxy_relative_path"]:
            raise ValueError(f"Asset {asset_id} does not have a validated correction ready")

        source_relative = Path(restored["source_relative_path"])
        corrected_relative = source_relative.with_suffix(".mp4")
        source = (media_root / source_relative).resolve()
        corrected = (media_root / corrected_relative).resolve()
        proxy = (normalized_root / Path(restored["proxy_relative_path"])).resolve()
        source.relative_to(media_root)
        corrected.relative_to(media_root)
        proxy.relative_to(normalized_root)
        if not source.is_file() or not corrected.is_file() or not proxy.is_file():
            raise FileNotFoundError(
                f"Asset {asset_id} requires source, existing MP4, and validated proxy files"
            )

        owner = connection.execute(
            """
            SELECT id FROM assets
            WHERE source_root_id = ? AND source_relative_path = ? AND id <> ?
            """,
            (
                restored["source_root_id"],
                corrected_relative.as_posix(),
                asset_id,
            ),
        ).fetchone()
        if owner is None:
            raise ValueError(f"Asset {asset_id} has no indexed owner for {corrected_relative}")
        owner_id = int(owner["id"])
        prior = connection.execute(
            """
            SELECT * FROM normalization_promotions
            WHERE asset_id = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
            """,
            (owner_id,),
        ).fetchone()
        if prior is None:
            raise ValueError(f"Existing MP4 asset {owner_id} has no active promotion record")
        if prior["original_source_relative_path"] != source_relative.as_posix():
            raise ValueError(f"Asset {asset_id} is not the recorded original for asset {owner_id}")
        if sha256_file(source) != prior["original_sha256"]:
            raise ValueError(f"Asset {asset_id} does not match the recorded original hash")
        if sha256_file(corrected) != prior["corrected_sha256"]:
            raise ValueError(f"Existing MP4 asset {owner_id} does not match its recorded hash")
        if connection.execute(
            "SELECT 1 FROM normalization_promotions WHERE asset_id = ?", (asset_id,)
        ).fetchone():
            raise ValueError(f"Restored asset {asset_id} already has promotion history")

        parked = corrected.with_name(f".{corrected.name}.retrotv-old-{token}.bak")
        if parked.exists():
            raise FileExistsError(f"Repair parking path already exists: {parked}")
        collisions.append(
            Collision(
                asset_id,
                owner_id,
                source_relative,
                corrected_relative,
                source,
                corrected,
                parked,
            )
        )
    return collisions


def migrate_references(connection, collisions: list[Collision]) -> None:
    now = utc_now()
    with transaction(connection):
        for item in collisions:
            old_id = item.promoted_asset_id
            new_id = item.restored_asset_id
            connection.execute(
                "UPDATE schedule_entries SET asset_id = ? WHERE asset_id = ?",
                (new_id, old_id),
            )
            connection.execute(
                "DELETE FROM asset_break_candidates WHERE asset_id = ?",
                (new_id,),
            )
            connection.execute(
                "DELETE FROM asset_break_analyses WHERE asset_id = ?",
                (new_id,),
            )
            connection.execute(
                "UPDATE asset_break_candidates SET asset_id = ? WHERE asset_id = ?",
                (new_id, old_id),
            )
            connection.execute(
                "UPDATE asset_break_analyses SET asset_id = ? WHERE asset_id = ?",
                (new_id, old_id),
            )
            connection.execute(
                "UPDATE integrity_scans SET asset_id = ? WHERE asset_id = ?",
                (new_id, old_id),
            )
            connection.execute(
                "UPDATE review_items SET asset_id = ? WHERE asset_id = ?",
                (new_id, old_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO asset_collection_memberships(
                    asset_id, collection_id, membership_source, enabled
                )
                SELECT ?, collection_id, membership_source, enabled
                FROM asset_collection_memberships WHERE asset_id = ?
                """,
                (new_id, old_id),
            )
            connection.execute(
                """
                UPDATE normalization_promotions
                SET asset_id = ?, status = 'restored', restored_at = ?
                WHERE asset_id = ?
                """,
                (new_id, now, old_id),
            )
            connection.execute("DELETE FROM assets WHERE id = ?", (old_id,))


def main() -> int:
    args = parser().parse_args()
    config = load_config(args.config)
    connection = connect_database(config.database_path)
    apply_migrations(connection, config.project_root / "migrations")
    token = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        collisions = discover(connection, config, args.asset, token)
        for item in collisions:
            print(
                f"validated restored asset {item.restored_asset_id} -> "
                f"promoted asset {item.promoted_asset_id}: {item.source_relative_path}"
            )
        if not args.apply:
            print(f"Validated {len(collisions)} collision(s); no changes made.")
            return 0

        backup_dir = config.database_path.parent / "database-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        database_backup = backup_dir / f"retrotv-before-promotion-repair-{token}.sqlite3"
        backup_connection = sqlite3.connect(database_backup)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()
        print(f"database backup: {database_backup}")

        parked: list[Collision] = []
        try:
            for item in collisions:
                os.replace(item.corrected_path, item.parked_path)
                parked.append(item)
            migrate_references(connection, collisions)
        except Exception:
            for item in reversed(parked):
                if item.parked_path.exists() and not item.corrected_path.exists():
                    os.replace(item.parked_path, item.corrected_path)
            raise

        promoter = PromotionService(connection, config)
        failures = 0
        for item in collisions:
            try:
                result = promoter.replace_with_backup(item.restored_asset_id)
            except Exception as exc:
                failures += 1
                print(f"FAILED asset {item.restored_asset_id}: {exc}", file=sys.stderr)
                print(f"  previous corrected copy retained at {item.parked_path}", file=sys.stderr)
            else:
                item.parked_path.unlink(missing_ok=True)
                print(f"promoted asset {result.asset_id}: {result.source_path}")
                print(f"  verified original backup: {result.backup_path}")
        if failures:
            print(f"Repair completed with {failures} promotion failure(s).", file=sys.stderr)
            return 1
        print(f"Successfully repaired and promoted {len(collisions)} asset(s).")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
