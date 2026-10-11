PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS programming_collections (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    collection_type TEXT NOT NULL CHECK (collection_type IN ('holiday', 'weekly')),
    channel_slug TEXT NOT NULL DEFAULT '*',
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE (slug, collection_type, channel_slug)
);

CREATE TABLE IF NOT EXISTS asset_collection_memberships (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    collection_id INTEGER NOT NULL
        REFERENCES programming_collections(id) ON DELETE CASCADE,
    membership_source TEXT NOT NULL DEFAULT 'folder',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (asset_id, collection_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_membership_collection
    ON asset_collection_memberships(collection_id, enabled);

CREATE TABLE IF NOT EXISTS calendar_rules (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    rule_type TEXT NOT NULL CHECK (rule_type IN ('holiday', 'weekly')),
    collection_slug TEXT NOT NULL,
    channel_slug TEXT,
    mode TEXT NOT NULL DEFAULT 'boost'
        CHECK (mode IN ('decorate', 'boost', 'replace')),
    priority INTEGER NOT NULL DEFAULT 100,
    start_month_day TEXT,
    end_month_day TEXT,
    weekdays_json TEXT,
    start_time TEXT,
    end_time TEXT,
    guide_label TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calendar_rules_channel
    ON calendar_rules(channel_slug, rule_type, enabled);

ALTER TABLE schedule_entries ADD COLUMN programming_owner TEXT NOT NULL DEFAULT 'base';
ALTER TABLE schedule_entries ADD COLUMN guide_label TEXT;
ALTER TABLE schedule_entries ADD COLUMN explanation_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS schedule_entry_influences (
    entry_id INTEGER NOT NULL
        REFERENCES schedule_entries(id) ON DELETE CASCADE,
    rule_slug TEXT NOT NULL,
    collection_slug TEXT NOT NULL,
    influence_type TEXT NOT NULL,
    weight_multiplier REAL NOT NULL DEFAULT 1.0,
    explanation TEXT NOT NULL,
    PRIMARY KEY (entry_id, rule_slug, influence_type)
);

CREATE INDEX IF NOT EXISTS idx_schedule_entry_influences_rule
    ON schedule_entry_influences(rule_slug, entry_id);
