-- Track the current Spotify saved-album library for the listen-to list.

CREATE TABLE IF NOT EXISTS saved_library_album (
    spotify_id TEXT PRIMARY KEY,
    spotify_uri TEXT NOT NULL,
    spotify_url TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    artists_json TEXT NOT NULL,
    normalized_artists_json TEXT NOT NULL,
    album_type TEXT NOT NULL,
    release_type TEXT NOT NULL,
    cover_url TEXT NOT NULL,
    added_at TEXT NOT NULL,
    is_posted_listened BOOLEAN NOT NULL DEFAULT 0,
    wordpress_post_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_saved_library_album_posted
ON saved_library_album (is_posted_listened);

CREATE INDEX IF NOT EXISTS idx_saved_library_album_added_at
ON saved_library_album (added_at);
