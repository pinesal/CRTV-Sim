CREATE TABLE IF NOT EXISTS holiday_filename_reviews (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    holiday_slug TEXT NOT NULL,
    filename TEXT NOT NULL,
    matched_clue TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'dismissed', 'moved')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, holiday_slug)
);

CREATE INDEX IF NOT EXISTS idx_holiday_filename_reviews_status
    ON holiday_filename_reviews(status, holiday_slug);
