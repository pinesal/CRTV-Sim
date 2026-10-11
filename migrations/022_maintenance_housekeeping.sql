PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schedule_history_checkpoints (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    context_type TEXT NOT NULL CHECK (context_type IN ('series', 'block')),
    context_key TEXT NOT NULL,
    asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    last_used_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, context_type, context_key)
);

CREATE INDEX IF NOT EXISTS idx_schedule_history_checkpoint_used
    ON schedule_history_checkpoints(channel_id, context_type, last_used_at DESC);

CREATE TABLE IF NOT EXISTS maintenance_runs (
    id INTEGER PRIMARY KEY,
    run_type TEXT NOT NULL CHECK (run_type IN ('housekeeping', 'compaction', 'cache_cleanup')),
    trigger_kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'complete', 'failed', 'skipped')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_lookup
    ON maintenance_runs(run_type, state, completed_at DESC);
