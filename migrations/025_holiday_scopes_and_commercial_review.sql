ALTER TABLE assets ADD COLUMN holiday_weekly_slug TEXT;
ALTER TABLE shadow_files ADD COLUMN holiday_weekly_slug TEXT;

CREATE TABLE IF NOT EXISTS commercial_duration_reviews (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    source_signature TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    threshold_seconds REAL NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('pending','approved','disabled')),
    reviewed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commercial_duration_reviews_decision
    ON commercial_duration_reviews(decision);
