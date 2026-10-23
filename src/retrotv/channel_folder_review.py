"""Review disappeared channel folders without treating outages as deletions."""
from __future__ import annotations

from pathlib import Path

from retrotv.classification import slugify
from retrotv.media_folders import ensure_channel_scaffold


class ChannelFolderReview:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    def inventory(self):
        root = self.config.storage_root("media")
        try:
            # Complete enumeration is required; an inaccessible root is not empty.
            names = {slugify(path.name) for path in root.iterdir() if path.is_dir()}
        except OSError as exc:
            return {"available": False, "items": [], "detail": f"Media library unavailable: {exc}"}
        items = []
        for row in self.connection.execute("""
            SELECT c.slug,c.display_name,c.management_identity,s.folder_name,s.missing_since
            FROM channel_folder_state s JOIN channels c ON c.id=s.channel_id
            WHERE s.missing_since IS NOT NULL AND c.slug NOT IN ('weather','__guide__') ORDER BY c.display_name
        """):
            if row["slug"] not in names:
                items.append({**dict(row), "path": str(root / row["folder_name"])})
        return {"available": True, "items": items, "detail": None}

    def recreate(self, slug, identity):
        row = self.connection.execute("""SELECT c.id,c.slug,c.display_name,c.management_identity,s.folder_name
            FROM channels c JOIN channel_folder_state s ON s.channel_id=c.id
            WHERE c.slug=? AND s.missing_since IS NOT NULL""", (slugify(slug),)).fetchone()
        if row is None or row["management_identity"] != identity:
            raise ValueError("The channel folder review changed; refresh before continuing")
        root = self.config.storage_root("media")
        # Confirm the root can actually be enumerated before creating anything.
        list(root.iterdir())
        name = str(row["folder_name"])
        if Path(name).name != name or name in {".", ".."} or slugify(name) != row["slug"]:
            raise ValueError("Invalid recorded channel folder")
        target = root / name
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Channel folder escapes the media library")
        from retrotv.asset_paths import require_writable_path
        # The scaffold contains empty directories only; never touch registered external roots.
        require_writable_path(self.connection, target)
        dayparts = [item["slug"] for item in self.config.data.get("scheduling", {}).get("dayparts", [])]
        raw_holidays = self.config.data.get("seasonal", {}).get("holidays", {})
        holidays = raw_holidays if isinstance(raw_holidays, dict) else [
            item.get("slug") if isinstance(item, dict) else item for item in raw_holidays]
        # A folder that reappeared is for the scanner to inspect. Do not fill
        # an existing/junction-backed tree just because this review was stale.
        created = [] if target.is_dir() else ensure_channel_scaffold(target, dayparts=dayparts, holidays=holidays)
        with self.connection:
            changed = self.connection.execute("UPDATE channels SET enabled=1 WHERE id=? AND management_identity=?", (row["id"], identity))
            if changed.rowcount != 1:
                raise ValueError("The channel changed during folder creation; refresh the review")
            self.connection.execute("UPDATE channel_folder_state SET missing_since=NULL WHERE channel_id=?", (row["id"],))
        return {"ok": True, "path": str(target), "folders_created": len(created),
                "detail": "Empty channel folders recreated. Rescan to discover any restored media."}
