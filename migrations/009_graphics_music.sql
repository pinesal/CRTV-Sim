PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS graphic_audio_assets (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL UNIQUE,
    purpose TEXT CHECK (purpose IS NULL OR purpose IN ('up-next', 'interstitials')),
    holiday_slug TEXT,
    duration_seconds REAL,
    file_size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('valid', 'missing', 'error')),
    provenance TEXT NOT NULL DEFAULT 'user' CHECK (provenance IN ('user', 'ai')),
    provenance_json TEXT NOT NULL DEFAULT '{}',
    detail TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graphic_audio_selection
    ON graphic_audio_assets(channel_id, purpose, holiday_slug, validation_status);
