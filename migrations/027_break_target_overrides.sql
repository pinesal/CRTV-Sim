CREATE TABLE IF NOT EXISTS asset_break_overrides (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    target_index INTEGER NOT NULL CHECK (target_index > 0),
    offset_ms INTEGER NOT NULL CHECK (offset_ms > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, target_index)
);

INSERT OR IGNORE INTO asset_break_overrides(asset_id,target_index,offset_ms,updated_at)
SELECT asset_id,1,selected_offset_ms,COALESCE(analyzed_at,datetime('now'))
FROM asset_break_analyses
WHERE status='manual' AND selected_offset_ms IS NOT NULL;
