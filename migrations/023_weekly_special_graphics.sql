PRAGMA foreign_keys = ON;

ALTER TABLE graphic_assets ADD COLUMN weekly_collection_slug TEXT;
ALTER TABLE graphic_audio_assets ADD COLUMN weekly_collection_slug TEXT;

CREATE INDEX IF NOT EXISTS idx_graphic_assets_weekly_selection
    ON graphic_assets(
        channel_id, weekly_collection_slug, purpose, validation_status
    );

CREATE INDEX IF NOT EXISTS idx_graphic_audio_weekly_selection
    ON graphic_audio_assets(
        channel_id, weekly_collection_slug, purpose, validation_status
    );
