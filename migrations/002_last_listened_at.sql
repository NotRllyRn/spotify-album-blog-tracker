-- Add last_listened_at column to track most recent listening progress per release.

ALTER TABLE release_lifecycle ADD COLUMN last_listened_at TEXT;
UPDATE release_lifecycle SET last_listened_at = last_seen WHERE last_listened_at IS NULL;
