CREATE TABLE iptv_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('direct','playlist')),
    last_refresh TEXT,
    refresh_error TEXT,
    catalog_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE iptv_feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES iptv_sources(id) ON DELETE CASCADE,
    selection_key TEXT NOT NULL,
    name TEXT NOT NULL,
    stream_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    availability TEXT NOT NULL DEFAULT 'unknown',
    detail TEXT,
    checked_at TEXT,
    UNIQUE(source_id, selection_key)
);
CREATE TABLE iptv_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES iptv_feeds(id) ON DELETE CASCADE,
    channel_slug TEXT NOT NULL REFERENCES channels(slug) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'all-day',
    weekly_slug TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    UNIQUE(feed_id,channel_slug,scope,weekly_slug)
);
CREATE INDEX iptv_channel ON iptv_assignments(channel_slug);
