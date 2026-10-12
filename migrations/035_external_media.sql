ALTER TABLE storage_roots ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'managed'
    CHECK(source_kind IN ('managed','external'));
ALTER TABLE storage_roots ADD COLUMN absolute_path TEXT;
CREATE TABLE external_sources (
    root_id INTEGER PRIMARY KEY REFERENCES storage_roots(id),
    series_title TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'unknown',
    last_scan_at TEXT,
    scan_status TEXT NOT NULL DEFAULT 'pending',
    scan_error TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE external_assignments (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES external_sources(root_id),
    channel_slug TEXT NOT NULL REFERENCES channels(slug) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    UNIQUE(root_id,channel_slug,scope)
);
CREATE INDEX idx_external_assignment_channel ON external_assignments(channel_slug);
