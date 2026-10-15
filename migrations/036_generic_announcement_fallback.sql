-- Cache the actual prerecorded audio selection, independently of AI profile policy.
ALTER TABLE announcement_opportunities ADD COLUMN fallback_mode TEXT;
ALTER TABLE announcement_opportunities ADD COLUMN fallback_audio_identity TEXT;
ALTER TABLE announcement_opportunities ADD COLUMN fallback_reason TEXT;
