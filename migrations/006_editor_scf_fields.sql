-- SCF human-curated fields editable through the Discord editor.
-- All default to safe non-curated values so existing rows behave identically
-- to before the editor existed.

ALTER TABLE release_lifecycle ADD COLUMN rating INTEGER;
ALTER TABLE release_lifecycle ADD COLUMN favorite BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE release_lifecycle ADD COLUMN notes TEXT;
ALTER TABLE release_lifecycle ADD COLUMN unreleased BOOLEAN NOT NULL DEFAULT 0;

ALTER TABLE release_track ADD COLUMN highlight BOOLEAN NOT NULL DEFAULT 0;
