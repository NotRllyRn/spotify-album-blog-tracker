-- Initial schema migration

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS release_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spotify_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    release_type TEXT NOT NULL,
    raw_spotify_type TEXT NOT NULL,
    cover_url TEXT NOT NULL,
    release_date TEXT NOT NULL,
    total_tracks INTEGER NOT NULL,
    total_duration_ms INTEGER NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    completed_at TEXT,
    published_at TEXT,
    wordpress_post_id INTEGER,
    wordpress_media_id INTEGER,
    duplicate_state TEXT,
    duplicate_post_id INTEGER
);

CREATE TABLE IF NOT EXISTS release_artist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    spotify_id TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    FOREIGN KEY (release_id) REFERENCES release_lifecycle(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS release_track (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    spotify_id TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    disc_number INTEGER NOT NULL,
    track_number INTEGER NOT NULL,
    is_countable BOOLEAN NOT NULL DEFAULT 1,
    listened BOOLEAN NOT NULL DEFAULT 0,
    listened_at TEXT,
    listened_source TEXT,
    FOREIGN KEY (release_id) REFERENCES release_lifecycle(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS wordpress_post_cache (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    artists_json TEXT NOT NULL,
    normalized_artists_json TEXT NOT NULL,
    link TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_prompt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_type TEXT NOT NULL,
    release_id TEXT,
    wordpress_post_id INTEGER,
    discord_message_id TEXT UNIQUE NOT NULL,
    state TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);