PRAGMA foreign_keys = ON;

ALTER TABLE schedule_entries ADD COLUMN programming_block_id INTEGER;
ALTER TABLE schedule_entries ADD COLUMN graphic_asset_id INTEGER REFERENCES graphic_assets(id);

CREATE TABLE IF NOT EXISTS graphic_assets (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (purpose IN ('up-next', 'interstitials')),
    holiday_slug TEXT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('image', 'video')),
    duration_seconds REAL,
    file_size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('valid', 'missing', 'error')),
    provenance TEXT NOT NULL DEFAULT 'user' CHECK (provenance IN ('user', 'placeholder', 'ai')),
    provenance_json TEXT NOT NULL DEFAULT '{}',
    detail TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graphic_assets_selection
    ON graphic_assets(channel_id, purpose, holiday_slug, validation_status);

CREATE TABLE IF NOT EXISTS announcement_opportunities (
    id INTEGER PRIMARY KEY,
    schedule_entry_id INTEGER NOT NULL UNIQUE REFERENCES schedule_entries(id) ON DELETE CASCADE,
    generation_id INTEGER NOT NULL REFERENCES schedule_generations(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    programming_block_id INTEGER,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    fallback_path TEXT,
    validated_output_path TEXT,
    render_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (render_status IN ('pending', 'rendering', 'ready', 'failed', 'expired')),
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcement_opportunities_due
    ON announcement_opportunities(channel_id, starts_at, render_status);

CREATE TABLE IF NOT EXISTS announcement_jobs (
    id INTEGER PRIMARY KEY,
    opportunity_id INTEGER NOT NULL REFERENCES announcement_opportunities(id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'ready', 'failed', 'cancelled', 'stale')),
    output_path TEXT,
    detail TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_announcement_jobs_status
    ON announcement_jobs(status, requested_at);
