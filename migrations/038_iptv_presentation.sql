ALTER TABLE iptv_feeds ADD COLUMN presentation TEXT NOT NULL DEFAULT 'preserve'
    CHECK(presentation IN ('preserve', 'fill_4_3', 'stretch_4_3'));
