PRAGMA foreign_keys = ON;

-- Keep the promotion history after a time-limited recovery copy is removed.
-- A NULL value means the tracked backup is still expected to exist.
ALTER TABLE normalization_promotions ADD COLUMN backup_deleted_at TEXT;
ALTER TABLE audio_normalization_promotions ADD COLUMN backup_deleted_at TEXT;

