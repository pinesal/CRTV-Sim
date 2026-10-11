PRAGMA foreign_keys = ON;

-- Per-channel, user-controlled recurring lineup behavior.  Keeping this with
-- the library database lets a portable RetroTV installation carry its channel
-- programming choices with it without relying on a machine-specific profile.
CREATE TABLE IF NOT EXISTS channel_schedule_preferences (
    channel_id INTEGER PRIMARY KEY
        REFERENCES channels(id) ON DELETE CASCADE,
    recurring_enabled INTEGER NOT NULL DEFAULT 0 CHECK (recurring_enabled IN (0, 1)),
    unique_weekends INTEGER NOT NULL DEFAULT 0 CHECK (unique_weekends IN (0, 1)),
    updated_at TEXT NOT NULL
);
