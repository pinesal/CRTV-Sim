-- A queued deletion must never act on a different channel that reuses its ID.
ALTER TABLE channels ADD COLUMN management_identity TEXT;
UPDATE channels SET management_identity=lower(hex(randomblob(16)));
CREATE TRIGGER channel_management_identity AFTER INSERT ON channels
WHEN NEW.management_identity IS NULL
BEGIN
    UPDATE channels SET management_identity=lower(hex(randomblob(16))) WHERE id=NEW.id;
END;

CREATE TABLE channel_folder_state (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    folder_name TEXT NOT NULL,
    missing_since TEXT
);
