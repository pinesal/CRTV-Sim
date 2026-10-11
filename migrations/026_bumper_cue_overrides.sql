CREATE TABLE IF NOT EXISTS bumper_cue_overrides (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    position TEXT NOT NULL CHECK(position IN ('general','break_start','break_end','exclude')),
    series_title TEXT,
    updated_at TEXT NOT NULL,
    CHECK(series_title IS NULL OR position IN ('break_start','break_end'))
);
