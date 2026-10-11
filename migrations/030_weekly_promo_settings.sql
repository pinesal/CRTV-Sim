ALTER TABLE announcement_weekly_profiles ADD COLUMN promo_enabled INTEGER NOT NULL DEFAULT 1 CHECK (promo_enabled IN (0, 1));
ALTER TABLE announcement_weekly_profiles ADD COLUMN promo_narration_enabled INTEGER NOT NULL DEFAULT 1 CHECK (promo_narration_enabled IN (0, 1));
ALTER TABLE announcement_weekly_profiles ADD COLUMN promo_prompt TEXT NOT NULL DEFAULT '';
