PRAGMA foreign_keys = ON;

-- A channel can either use RetroTV's normal station presentation or run a
-- continuous, interruption-free stream of its actual programming.
ALTER TABLE channel_schedule_preferences
    ADD COLUMN presentation_format TEXT NOT NULL DEFAULT 'standard'
    CHECK (presentation_format IN ('standard', 'content_only'));
