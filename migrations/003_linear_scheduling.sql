PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    channel_number INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS schedule_generations (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    schedule_date TEXT NOT NULL,
    seed TEXT NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'building',
    validation_status TEXT NOT NULL DEFAULT 'pending',
    validation_detail TEXT,
    generated_at TEXT NOT NULL,
    activated_at TEXT,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_schedule_generations_lookup
    ON schedule_generations(channel_id, schedule_date, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_one_active
    ON schedule_generations(channel_id, schedule_date)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS schedule_entries (
    id INTEGER PRIMARY KEY,
    generation_id INTEGER NOT NULL
        REFERENCES schedule_generations(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    asset_id INTEGER REFERENCES assets(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    media_start_ms INTEGER,
    media_end_ms INTEGER,
    entry_type TEXT NOT NULL,
    title TEXT NOT NULL,
    daypart_slug TEXT NOT NULL,
    sequence_group INTEGER,
    sequence_position INTEGER NOT NULL,
    is_program INTEGER NOT NULL DEFAULT 0 CHECK (is_program IN (0, 1)),
    CHECK (ends_at > starts_at),
    CHECK (
        (asset_id IS NULL AND media_start_ms IS NULL AND media_end_ms IS NULL)
        OR
        (asset_id IS NOT NULL AND media_start_ms IS NOT NULL
         AND media_end_ms IS NOT NULL AND media_end_ms > media_start_ms)
    )
);

CREATE INDEX IF NOT EXISTS idx_schedule_entries_timeline
    ON schedule_entries(channel_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_schedule_entries_generation
    ON schedule_entries(generation_id, sequence_position);
CREATE INDEX IF NOT EXISTS idx_schedule_entries_asset
    ON schedule_entries(asset_id, starts_at);
