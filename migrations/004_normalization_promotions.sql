PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS normalization_promotions (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    original_source_relative_path TEXT NOT NULL,
    corrected_source_relative_path TEXT NOT NULL,
    backup_root_id INTEGER NOT NULL REFERENCES storage_roots(id),
    backup_relative_path TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    corrected_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    restored_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_promotions_asset
    ON normalization_promotions(asset_id, created_at DESC);
