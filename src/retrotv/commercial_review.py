from __future__ import annotations

from pathlib import Path

from retrotv.classification import classify_path, slugify, CHANNEL_DAYPARTS
from retrotv.db import utc_now


class CommercialReviewService:
    """Keep long-ad decisions attached to asset identity and source content."""

    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    @property
    def threshold_seconds(self) -> float:
        return float(self.config.data.get("library", {}).get("long_commercial_minutes", 3)) * 60

    def reconcile(self) -> int:
        threshold = self.threshold_seconds
        now = utc_now()
        changed = 0
        rows = self.connection.execute(
            """SELECT a.id,a.review_status,a.enabled_for_scheduling,a.integrity_status,
                      a.media_type,a.fingerprint,m.duration_seconds,
                      COALESCE(h.original_sha256,a.fingerprint) AS signature,
                      r.source_signature,r.decision,
                      r.duration_seconds AS review_duration,
                      r.threshold_seconds AS review_threshold
               FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
               LEFT JOIN asset_content_hashes h ON h.asset_id=a.id
               LEFT JOIN commercial_duration_reviews r ON r.asset_id=a.id
               WHERE a.media_type='commercial' OR r.asset_id IS NOT NULL"""
        ).fetchall()
        for row in rows:
            asset_id = int(row["id"])
            current = row["media_type"] == "commercial" and row["duration_seconds"] is not None
            over = current and float(row["duration_seconds"]) > threshold
            signature = str(row["signature"] or "")
            prior = str(row["decision"] or "")
            decision = prior if row["source_signature"] == signature else "pending"
            if not over:
                self.connection.execute("DELETE FROM commercial_duration_reviews WHERE asset_id=?", (asset_id,))
                self._resolve_item(asset_id, now)
                if prior == "pending" and row["review_status"] == "open" and not self._other_open_items(asset_id):
                    self.connection.execute(
                        "UPDATE assets SET review_status='ready',enabled_for_scheduling=1 WHERE id=?", (asset_id,)
                    )
                    changed += 1
                continue
            if row["review_status"] == "ignored" and prior != "disabled":
                continue
            if decision not in {"approved", "disabled"}:
                decision = "pending"
            if (row["source_signature"] != signature or prior != decision
                    or row["review_duration"] != float(row["duration_seconds"])
                    or row["review_threshold"] != threshold):
                self.connection.execute(
                    """INSERT INTO commercial_duration_reviews(
                     asset_id,source_signature,duration_seconds,threshold_seconds,
                     decision,reviewed_at,updated_at)
                   VALUES (?,?,?,?,?,NULL,?)
                   ON CONFLICT(asset_id) DO UPDATE SET
                     source_signature=excluded.source_signature,
                     duration_seconds=excluded.duration_seconds,
                     threshold_seconds=excluded.threshold_seconds,
                     decision=excluded.decision,
                     reviewed_at=CASE WHEN excluded.decision='pending' THEN NULL
                                      ELSE commercial_duration_reviews.reviewed_at END,
                     updated_at=excluded.updated_at""",
                    (asset_id, signature, float(row["duration_seconds"]), threshold, decision, now),
                )
            if decision == "pending":
                self._open_item(asset_id, float(row["duration_seconds"]), threshold, now)
                if row["review_status"] != "open" or row["enabled_for_scheduling"]:
                    self.connection.execute(
                        "UPDATE assets SET review_status='open',enabled_for_scheduling=0 WHERE id=?", (asset_id,)
                    )
                    changed += 1
            else:
                self._resolve_item(asset_id, now)
                if decision == "disabled":
                    desired = "open" if self._other_open_items(asset_id) else "ready"
                    if row["enabled_for_scheduling"] or row["review_status"] != desired:
                        self.connection.execute(
                            "UPDATE assets SET review_status=?,enabled_for_scheduling=0 WHERE id=?", (desired, asset_id)
                        )
                elif (not self._other_open_items(asset_id)
                      and (row["review_status"] != "ready" or not row["enabled_for_scheduling"])):
                    self.connection.execute(
                        "UPDATE assets SET review_status='ready',enabled_for_scheduling=1 WHERE id=?", (asset_id,)
                    )
        self.connection.commit()
        return changed

    def decide(self, asset_id: int, decision: str) -> None:
        if decision not in {"approved", "disabled"}:
            raise ValueError("Choose approve or disable")
        self.reconcile()
        row = self.connection.execute(
            "SELECT decision FROM commercial_duration_reviews WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Asset is not a long commercial")
        now = utc_now()
        self.connection.execute(
            """UPDATE commercial_duration_reviews SET decision=?,reviewed_at=?,updated_at=?
               WHERE asset_id=?""",
            (decision, now, now, asset_id),
        )
        self._resolve_item(asset_id, now)
        if decision == "disabled":
            desired = "open" if self._other_open_items(asset_id) else "ready"
            self.connection.execute(
                "UPDATE assets SET review_status=?,enabled_for_scheduling=0 WHERE id=?", (desired, asset_id)
            )
        elif not self._other_open_items(asset_id):
            self.connection.execute(
                "UPDATE assets SET review_status='ready',enabled_for_scheduling=1 WHERE id=?", (asset_id,)
            )
        self.connection.commit()

    def move_to_category(self, asset_id: int, category: str) -> str:
        allowed = {"episodes", "shorts", "blocks", "promos", "filler"}
        if category not in allowed:
            raise ValueError("Choose a supported destination category")
        row = self.connection.execute(
            "SELECT source_relative_path,media_type FROM assets WHERE id=?", (asset_id,)
        ).fetchone()
        if row is None or row["media_type"] != "commercial":
            raise ValueError("Asset is not a commercial")
        relative = Path(row["source_relative_path"])
        parts = list(relative.parts)
        indexes = [index for index, value in enumerate(parts[:-1]) if value.casefold() == "commercials"]
        if len(indexes) != 1:
            raise ValueError("Cannot identify one commercial folder in this asset path")
        parts[indexes[0]] = category
        target_relative = Path(*parts)
        root = self.config.storage_root("media").resolve()
        source = (root / relative).resolve()
        target = (root / target_relative).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(root):
            raise ValueError("Destination escapes the media library")
        if not source.is_file():
            raise ValueError("Commercial file is missing")
        if target.exists():
            raise ValueError(f"Destination already exists: {target}")
        known_channels = {str(item["slug"]) for item in self.connection.execute("SELECT slug FROM channels")}
        holidays = {slugify(str(value)) for value in self.config.data.get("seasonal", {}).get("holidays", {})}
        dayparts = CHANNEL_DAYPARTS | {slugify(str(item["slug"])) for item in
                                       self.config.data.get("scheduling", {}).get("dayparts", [])
                                       if isinstance(item, dict) and item.get("slug")}
        classification = classify_path(
            target_relative.as_posix(), known_channels=known_channels,
            known_holidays=holidays, known_dayparts=dayparts,
        )
        if classification.review_reasons:
            raise ValueError("Destination needs Library review: " + classification.review_reasons[0][1])
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        try:
            self.connection.execute(
                """UPDATE assets SET source_relative_path=?,original_filename=?,
                     media_type=?,channel_slug=?,daypart_slug=?,collection_type=?,
                     collection_slug=?,holiday_slug=?,holiday_weekly_slug=?,
                     series_title=?,review_status='ready',enabled_for_scheduling=1
                   WHERE id=?""",
                (target_relative.as_posix(), target.name, classification.media_type,
                 classification.channel_slug, classification.daypart_slug,
                 classification.collection_type, classification.collection_slug,
                 classification.holiday_slug, classification.holiday_weekly_slug,
                 classification.series_title, asset_id),
            )
            self.connection.execute("DELETE FROM asset_eligibility WHERE asset_id=?", (asset_id,))
            self.connection.executemany(
                """INSERT INTO asset_eligibility(asset_id,channel_slug,daypart_slug,
                     eligibility_source,weight,enabled) VALUES (?,?,?,?,?,1)""",
                [(asset_id, item.channel_slug, item.daypart_slug, item.source, item.weight)
                 for item in classification.eligibility],
            )
            self.connection.execute("DELETE FROM commercial_duration_reviews WHERE asset_id=?", (asset_id,))
            self._resolve_item(asset_id, utc_now())
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            target.rename(source)
            raise
        return target_relative.as_posix()

    def _other_open_items(self, asset_id: int) -> bool:
        return self.connection.execute(
            """SELECT 1 FROM review_items WHERE asset_id=? AND status='open'
               AND reason_code!='long_commercial' LIMIT 1""", (asset_id,)
        ).fetchone() is not None

    def _open_item(self, asset_id: int, duration: float, threshold: float, now: str) -> None:
        if self.connection.execute(
            """SELECT 1 FROM review_items WHERE asset_id=? AND status='open'
               AND reason_code='long_commercial'""", (asset_id,)
        ).fetchone() is None:
            self.connection.execute(
                """INSERT INTO review_items(asset_id,reason_code,detail,status,created_at)
                   VALUES (?, 'long_commercial', ?, 'open', ?)""",
                (asset_id, f"Commercial is {duration / 60:.1f} minutes; threshold is {threshold / 60:g} minutes", now),
            )

    def _resolve_item(self, asset_id: int, now: str) -> None:
        self.connection.execute(
            """UPDATE review_items SET status='resolved',resolved_at=?
               WHERE asset_id=? AND reason_code='long_commercial' AND status='open'""",
            (now, asset_id),
        )
