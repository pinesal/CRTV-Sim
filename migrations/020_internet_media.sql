PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channel_playback_preferences (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    playback_mode TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (playback_mode IN ('scheduled', 'watch_at_your_pace')),
    storage_mode TEXT NOT NULL DEFAULT 'keep'
        CHECK (storage_mode IN ('keep', 'save_space')),
    prepare_ahead INTEGER NOT NULL DEFAULT 1 CHECK (prepare_ahead BETWEEN 0 AND 20),
    same_date_order_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internet_sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('internet_archive', 'direct_video')),
    url TEXT NOT NULL,
    show_title TEXT NOT NULL,
    daypart_slug TEXT,
    source_relative_path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    refreshed_at TEXT,
    refresh_error TEXT,
    UNIQUE (channel_id, source_relative_path)
);

CREATE TABLE IF NOT EXISTS remote_episodes (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES internet_sources(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    remote_url TEXT NOT NULL,
    remote_filename TEXT NOT NULL,
    display_title TEXT NOT NULL,
    episode_date TEXT,
    date_source TEXT,
    duration_seconds REAL,
    size_bytes INTEGER,
    checksum_kind TEXT,
    checksum_value TEXT,
    metadata_status TEXT NOT NULL DEFAULT 'review'
        CHECK (metadata_status IN ('ready', 'review', 'excluded')),
    selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    manual_order INTEGER NOT NULL DEFAULT 0,
    catchup_status TEXT NOT NULL DEFAULT 'none'
        CHECK (catchup_status IN ('none', 'review', 'accepted', 'excluded')),
    availability_status TEXT NOT NULL DEFAULT 'available'
        CHECK (availability_status IN ('available', 'missing', 'changed')),
    source_etag TEXT,
    source_last_modified TEXT,
    local_path TEXT,
    retained INTEGER NOT NULL DEFAULT 0 CHECK (retained IN (0, 1)),
    aspect_mode TEXT,
    crop_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (source_id, stable_key)
);

CREATE INDEX IF NOT EXISTS idx_remote_episodes_playlist
    ON remote_episodes(source_id, selected, episode_date, manual_order);
CREATE INDEX IF NOT EXISTS idx_remote_episodes_local
    ON remote_episodes(local_path);

CREATE TABLE IF NOT EXISTS internet_preparation_jobs (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL UNIQUE REFERENCES remote_episodes(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'cataloged'
        CHECK (state IN (
            'cataloged', 'queued', 'downloading', 'validating', 'analyzing',
            'normalizing', 'awaiting_scan', 'ready', 'review', 'retry',
            'paused_space', 'failed', 'completed'
        )),
    priority INTEGER NOT NULL DEFAULT 100,
    temporary_path TEXT,
    final_path TEXT,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_internet_jobs_queue
    ON internet_preparation_jobs(state, priority, created_at);

CREATE TABLE IF NOT EXISTS watch_progress (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    current_episode_id INTEGER REFERENCES remote_episodes(id) ON DELETE SET NULL,
    position_seconds REAL NOT NULL DEFAULT 0 CHECK (position_seconds >= 0),
    state TEXT NOT NULL DEFAULT 'not_started'
        CHECK (state IN ('not_started', 'in_progress', 'caught_up')),
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO channel_playback_preferences(
    channel_id, playback_mode, storage_mode, prepare_ahead,
    same_date_order_json, updated_at
)
SELECT id, 'scheduled', 'keep', 1, '[]', strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM channels;
