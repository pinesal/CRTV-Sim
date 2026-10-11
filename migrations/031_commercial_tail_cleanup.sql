CREATE TABLE IF NOT EXISTS commercial_tail_analyses (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    policy_version INTEGER NOT NULL,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    trim_at_seconds REAL,
    original_duration_seconds REAL,
    corrected_duration_seconds REAL,
    backup_relative_path TEXT,
    original_sha256 TEXT,
    corrected_sha256 TEXT,
    detail TEXT,
    analyzed_at TEXT NOT NULL
);

-- Existing rendered Up Next files may contain artwork from the outgoing
-- weekly special. Force both variants to be rendered with the corrected scope.
UPDATE announcement_opportunities
SET fallback_path=NULL, validated_output_path=NULL, render_status='pending'
WHERE opportunity_type='up_next';
