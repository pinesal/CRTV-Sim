PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS asset_content_hashes (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    original_sha256 TEXT NOT NULL,
    working_sha256 TEXT NOT NULL,
    original_file_size INTEGER NOT NULL,
    working_file_size INTEGER NOT NULL,
    working_modified_ns INTEGER NOT NULL,
    hashed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_asset_content_original
    ON asset_content_hashes(original_sha256);
CREATE INDEX IF NOT EXISTS idx_asset_content_working
    ON asset_content_hashes(working_sha256);

CREATE TABLE IF NOT EXISTS duplicate_reviews (
    original_sha256 TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'intentional')),
    reviewed_member_count INTEGER NOT NULL DEFAULT 0,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS content_normalization_recipes (
    original_sha256 TEXT PRIMARY KEY,
    normalization_signature TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    source_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
