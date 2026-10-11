PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS block_profiles (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    general_title TEXT NOT NULL,
    boundary_policy TEXT NOT NULL DEFAULT 'may_overrun'
        CHECK (boundary_policy IN ('hard', 'soft', 'must_fit', 'fill_short', 'may_overrun')),
    probability_override REAL
        CHECK (probability_override IS NULL
               OR (probability_override >= 0.0 AND probability_override <= 1.0)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    notes TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block_cues (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    offset_ms INTEGER NOT NULL CHECK (offset_ms >= 0),
    end_offset_ms INTEGER CHECK (end_offset_ms IS NULL OR end_offset_ms > offset_ms),
    cue_type TEXT NOT NULL
        CHECK (cue_type IN ('program', 'commercial_break', 'bumper', 'promo',
                            'station_id', 'other')),
    title TEXT NOT NULL,
    description TEXT,
    sequence_position INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (asset_id, offset_ms)
);

CREATE INDEX IF NOT EXISTS idx_block_cues_asset
    ON block_cues(asset_id, sequence_position);

ALTER TABLE schedule_entries
    ADD COLUMN protected_block INTEGER NOT NULL DEFAULT 0
        CHECK (protected_block IN (0, 1));
ALTER TABLE schedule_entries ADD COLUMN guide_listing_title TEXT;
