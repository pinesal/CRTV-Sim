PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS audio_normalization_profiles (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    source_signature TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (analysis_status IN (
            'pending', 'analyzing', 'analyzed', 'review', 'error', 'not_applicable'
        )),
    measured_i REAL,
    measured_tp REAL,
    measured_lra REAL,
    measured_thresh REAL,
    target_offset REAL,
    target_i REAL NOT NULL DEFAULT -18.0,
    target_tp REAL NOT NULL DEFAULT -1.5,
    target_lra REAL NOT NULL DEFAULT 11.0,
    needs_correction INTEGER NOT NULL DEFAULT 0 CHECK (needs_correction IN (0, 1)),
    approval_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (approval_status IN ('pending', 'approved', 'skipped', 'review')),
    correction_status TEXT NOT NULL DEFAULT 'not_needed'
        CHECK (correction_status IN (
            'not_needed', 'pending', 'normalizing', 'promoted', 'error', 'stale'
        )),
    detail TEXT,
    error TEXT,
    analyzed_at TEXT,
    corrected_at TEXT
);

CREATE TABLE IF NOT EXISTS audio_normalization_promotions (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    source_relative_path TEXT NOT NULL,
    backup_root_id INTEGER NOT NULL REFERENCES storage_roots(id),
    backup_relative_path TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    corrected_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'restored')),
    created_at TEXT NOT NULL,
    restored_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_audio_profiles_status
    ON audio_normalization_profiles(analysis_status, correction_status);

CREATE INDEX IF NOT EXISTS idx_audio_promotions_asset
    ON audio_normalization_promotions(asset_id, created_at DESC);
