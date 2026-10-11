PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS announcement_channel_profiles (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    script_model TEXT NOT NULL DEFAULT 'gpt-5.6-luna',
    speech_model TEXT NOT NULL DEFAULT 'gpt-4o-mini-tts',
    voice TEXT NOT NULL DEFAULT 'alloy',
    personality_prompt TEXT NOT NULL DEFAULT '',
    voice_instructions TEXT NOT NULL DEFAULT '',
    generic_script TEXT NOT NULL DEFAULT '',
    maximum_words INTEGER NOT NULL DEFAULT 55 CHECK (maximum_words BETWEEN 10 AND 150),
    profile_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcement_generic_audio (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
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

