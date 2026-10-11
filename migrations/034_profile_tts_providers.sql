ALTER TABLE announcement_channel_profiles ADD COLUMN tts_provider TEXT NOT NULL DEFAULT 'openai' CHECK(tts_provider IN ('openai','breeze'));
ALTER TABLE announcement_channel_profiles ADD COLUMN breeze_speech_model TEXT NOT NULL DEFAULT 'breeze-tts-2';
ALTER TABLE announcement_channel_profiles ADD COLUMN breeze_voice_id TEXT NOT NULL DEFAULT '';
ALTER TABLE announcement_daypart_profiles ADD COLUMN tts_provider TEXT NOT NULL DEFAULT 'openai' CHECK(tts_provider IN ('openai','breeze'));
ALTER TABLE announcement_daypart_profiles ADD COLUMN breeze_speech_model TEXT NOT NULL DEFAULT 'breeze-tts-2';
ALTER TABLE announcement_daypart_profiles ADD COLUMN breeze_voice_id TEXT NOT NULL DEFAULT '';
ALTER TABLE announcement_weekly_profiles ADD COLUMN tts_provider TEXT NOT NULL DEFAULT 'openai' CHECK(tts_provider IN ('openai','breeze'));
ALTER TABLE announcement_weekly_profiles ADD COLUMN breeze_speech_model TEXT NOT NULL DEFAULT 'breeze-tts-2';
ALTER TABLE announcement_weekly_profiles ADD COLUMN breeze_voice_id TEXT NOT NULL DEFAULT '';
