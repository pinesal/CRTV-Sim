PRAGMA foreign_keys = ON;

ALTER TABLE remote_episodes
    ADD COLUMN asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL;

ALTER TABLE remote_episodes
    ADD COLUMN scheduled_aired_at TEXT;

ALTER TABLE internet_preparation_jobs
    ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0
    CHECK (cancel_requested IN (0, 1));

ALTER TABLE watch_progress
    ADD COLUMN position_confirmed INTEGER NOT NULL DEFAULT 0
    CHECK (position_confirmed IN (0, 1));

UPDATE watch_progress SET position_confirmed = 1;

CREATE INDEX IF NOT EXISTS idx_remote_episodes_asset
    ON remote_episodes(asset_id);

CREATE INDEX IF NOT EXISTS idx_internet_jobs_cancel
    ON internet_preparation_jobs(cancel_requested, state, priority);
