PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS normalization_profiles (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    detected_mode TEXT,
    proposed_mode TEXT,
    override_mode TEXT NOT NULL DEFAULT 'auto',
    confidence REAL,
    crop_x INTEGER,
    crop_y INTEGER,
    crop_width INTEGER,
    crop_height INTEGER,
    sample_results_json TEXT,
    analysis_detail TEXT,
    analyzed_at TEXT,
    proxy_root_id INTEGER REFERENCES storage_roots(id),
    proxy_relative_path TEXT,
    proxy_status TEXT NOT NULL DEFAULT 'not_created',
    proxy_duration_seconds REAL,
    proxy_error TEXT,
    proxy_created_at TEXT,
    source_preview_relative_path TEXT,
    normalized_preview_relative_path TEXT,
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
);

CREATE INDEX IF NOT EXISTS idx_normalization_status
    ON normalization_profiles(analysis_status, proxy_status);
