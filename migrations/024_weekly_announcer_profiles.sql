PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS announcement_weekly_profiles (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    collection_slug TEXT NOT NULL,
    override_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (override_enabled IN (0, 1)),
    identity_name TEXT NOT NULL,
    tagline TEXT NOT NULL DEFAULT '',
    spoken_tagline TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    script_model TEXT NOT NULL DEFAULT 'gpt-5.6-luna',
    speech_model TEXT NOT NULL DEFAULT 'gpt-4o-mini-tts',
    voice TEXT NOT NULL DEFAULT 'alloy',
    personality_prompt TEXT NOT NULL DEFAULT '',
    voice_instructions TEXT NOT NULL DEFAULT '',
    generic_script TEXT NOT NULL DEFAULT '',
    maximum_words INTEGER NOT NULL DEFAULT 55
        CHECK (maximum_words BETWEEN 10 AND 150),
    profile_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(channel_id, collection_slug)
);

CREATE TABLE IF NOT EXISTS announcement_weekly_generic_audio (
    profile_id INTEGER PRIMARY KEY
        REFERENCES announcement_weekly_profiles(id) ON DELETE CASCADE,
    profile_fingerprint TEXT NOT NULL,
    script_text TEXT NOT NULL,
    audio_path TEXT,
    provider TEXT NOT NULL DEFAULT 'openai',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'generating', 'ready', 'failed', 'stale')),
    detail TEXT,
    duration_seconds REAL,
    generated_at TEXT,
    updated_at TEXT NOT NULL
);

ALTER TABLE announcement_opportunities ADD COLUMN weekly_collection_slug TEXT;

CREATE INDEX IF NOT EXISTS idx_announcement_opportunities_weekly
    ON announcement_opportunities(
        channel_id, weekly_collection_slug, starts_at, render_status
    );
