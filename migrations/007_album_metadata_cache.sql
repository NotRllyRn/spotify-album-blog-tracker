CREATE TABLE IF NOT EXISTS album_metadata_cache (
    spotify_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('ready', 'unresolved')),
    lastfm_url TEXT,
    lastfm_mbid TEXT,
    genres_json TEXT NOT NULL DEFAULT '[]',
    lastfm_listeners INTEGER,
    diagnostic_code TEXT,
    resolver_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_album_metadata_cache_listeners
ON album_metadata_cache (lastfm_listeners DESC);
