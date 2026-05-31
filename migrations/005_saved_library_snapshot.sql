-- Complete saved-library identity snapshot used for incremental reconciliation.

CREATE TABLE IF NOT EXISTS saved_library_snapshot_item (
    spotify_id TEXT PRIMARY KEY,
    spotify_uri TEXT NOT NULL,
    added_at TEXT NOT NULL,
    position INTEGER NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_library_snapshot_position
ON saved_library_snapshot_item(position);

CREATE INDEX IF NOT EXISTS idx_saved_library_snapshot_added_at
ON saved_library_snapshot_item(added_at);
