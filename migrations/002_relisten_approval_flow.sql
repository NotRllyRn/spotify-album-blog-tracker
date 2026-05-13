-- Move relisten approval before automatic tracking starts.

ALTER TABLE release_lifecycle ADD COLUMN is_relisten BOOLEAN NOT NULL DEFAULT 0;

ALTER TABLE discord_prompt ADD COLUMN created_at TEXT;
ALTER TABLE discord_prompt ADD COLUMN expires_at TEXT;
ALTER TABLE discord_prompt ADD COLUMN context_json TEXT;

UPDATE discord_prompt
SET created_at = COALESCE(created_at, datetime('now'))
WHERE created_at IS NULL;

UPDATE release_lifecycle
SET is_relisten = 1
WHERE duplicate_state = 'found'
  AND status IN ('published', 'trashed_post');

UPDATE discord_prompt
SET state = 'expired'
WHERE prompt_type = 'relisten'
  AND state = 'pending';

DELETE FROM release_lifecycle
WHERE duplicate_state = 'found'
  AND status IN ('active', 'awaiting_75_decision', 'awaiting_relisten_decision', 'publishing');

CREATE INDEX IF NOT EXISTS idx_discord_prompt_release_type_state
ON discord_prompt (release_id, prompt_type, state);
