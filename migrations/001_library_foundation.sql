PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS storage_roots (
    id INTEGER PRIMARY KEY,
    logical_name TEXT NOT NULL UNIQUE,
    storage_type TEXT NOT NULL DEFAULT 'local',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES storage_roots(id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    ready_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    source_root_id INTEGER NOT NULL REFERENCES storage_roots(id),
    source_relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    fingerprint TEXT,
    fingerprint_algorithm TEXT,
    file_size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    media_type TEXT,
    duration_class TEXT,
    display_title TEXT,
    series_title TEXT,
    channel_slug TEXT,
    daypart_slug TEXT,
    collection_type TEXT,
    collection_slug TEXT,
    holiday_slug TEXT,
    metadata_status TEXT NOT NULL DEFAULT 'pending',
    integrity_status TEXT NOT NULL DEFAULT 'pending',
    review_status TEXT NOT NULL DEFAULT 'pending',
    enabled_for_scheduling INTEGER NOT NULL DEFAULT 0 CHECK (enabled_for_scheduling IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_scan_run_id INTEGER REFERENCES scan_runs(id),
    UNIQUE (source_root_id, source_relative_path)
);

CREATE INDEX IF NOT EXISTS idx_assets_fingerprint
    ON assets(fingerprint);
CREATE INDEX IF NOT EXISTS idx_assets_review
    ON assets(review_status, enabled_for_scheduling);
CREATE INDEX IF NOT EXISTS idx_assets_channel_daypart
    ON assets(channel_slug, daypart_slug, media_type);

CREATE TABLE IF NOT EXISTS asset_eligibility (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    channel_slug TEXT,
    daypart_slug TEXT,
    eligibility_source TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE (asset_id, channel_slug, daypart_slug, eligibility_source)
);

CREATE TABLE IF NOT EXISTS asset_media_info (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    container_format TEXT,
    duration_seconds REAL,
    bit_rate INTEGER,
    video_codec TEXT,
    audio_codec TEXT,
    width INTEGER,
    height INTEGER,
    frame_rate TEXT,
    sample_aspect_ratio TEXT,
    display_aspect_ratio TEXT,
    pixel_format TEXT,
    field_order TEXT,
    audio_sample_rate INTEGER,
    audio_channels INTEGER,
    raw_probe_json TEXT,
    probe_completed_at TEXT
);

CREATE TABLE IF NOT EXISTS integrity_scans (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    scan_run_id INTEGER REFERENCES scan_runs(id),
    scan_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    error_category TEXT,
    error_detail TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_integrity_asset
    ON integrity_scans(asset_id, completed_at DESC);

CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    reason_code TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_items_status
    ON review_items(status, reason_code);
