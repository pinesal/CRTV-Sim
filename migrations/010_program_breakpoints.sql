PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS asset_break_analyses (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    source_signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    selected_offset_ms INTEGER,
    selected_method TEXT,
    confidence REAL,
    detail TEXT,
    analyzed_at TEXT,
    CHECK (selected_offset_ms IS NULL OR selected_offset_ms > 0)
);

CREATE TABLE IF NOT EXISTS asset_break_candidates (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    offset_ms INTEGER NOT NULL,
    method TEXT NOT NULL,
    score REAL NOT NULL,
    signals_json TEXT NOT NULL DEFAULT '{}',
    selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK (offset_ms > 0)
);

CREATE INDEX IF NOT EXISTS idx_asset_break_candidates_asset
    ON asset_break_candidates(asset_id, selected, score DESC);

