PRAGMA foreign_keys = ON;

-- Each active schedule carries a compact projection of recurring assignments,
-- the waiting queue, and series episode progress through that schedule date.
CREATE TABLE IF NOT EXISTS recurring_lineup_snapshots (
    generation_id INTEGER PRIMARY KEY
        REFERENCES schedule_generations(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    schedule_date TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    input_state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recurring_lineup_snapshot_date
    ON recurring_lineup_snapshots(channel_id, schedule_date DESC);

-- Housekeeping retains the most recent state before old schedule generations
-- are removed, so long-running queues and episode positions survive pruning.
CREATE TABLE IF NOT EXISTS recurring_lineup_checkpoints (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    checkpoint_date TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
