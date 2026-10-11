PRAGMA foreign_keys = ON;

ALTER TABLE announcement_opportunities
    ADD COLUMN opportunity_type TEXT NOT NULL DEFAULT 'up_next';

CREATE INDEX IF NOT EXISTS idx_announcement_opportunities_type_due
    ON announcement_opportunities(opportunity_type, channel_id, starts_at);
