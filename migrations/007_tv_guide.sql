PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guide_renders (
    id INTEGER PRIMARY KEY,
    window_starts_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,
    output_relative_path TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'valid', 'failed')),
    detail TEXT,
    generated_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_guide_renders_status
    ON guide_renders(status, generated_at);
