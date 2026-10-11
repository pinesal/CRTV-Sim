PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shadow_files (
    id INTEGER PRIMARY KEY,
    shadow_relative_path TEXT NOT NULL UNIQUE,
    target_asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    target_relative_path TEXT NOT NULL,
    target_sha256 TEXT,
    media_type TEXT,
    channel_slug TEXT,
    daypart_slug TEXT,
    collection_type TEXT,
    collection_slug TEXT,
    holiday_slug TEXT,
    series_title TEXT,
    display_title TEXT,
    file_size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'review')),
    detail TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shadow_files_target
    ON shadow_files(target_asset_id, status);
CREATE INDEX IF NOT EXISTS idx_shadow_files_location
    ON shadow_files(channel_slug, daypart_slug, collection_type, collection_slug);

CREATE TABLE IF NOT EXISTS shadow_eligibility (
    id INTEGER PRIMARY KEY,
    shadow_id INTEGER NOT NULL REFERENCES shadow_files(id) ON DELETE CASCADE,
    channel_slug TEXT,
    daypart_slug TEXT,
    eligibility_source TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE (shadow_id, channel_slug, daypart_slug, eligibility_source)
);

CREATE INDEX IF NOT EXISTS idx_shadow_eligibility_lookup
    ON shadow_eligibility(channel_slug, daypart_slug, enabled);
