# Spotify WordPress Album Tracker: Program Documentation

This document is a maintainer and operator guide for the Spotify WordPress Album Tracker. It describes how the program works from startup through playback polling, database persistence, WordPress publishing, Discord control, deployment, and tests.

The short version: this is a long-running async Python service. It watches Spotify playback, decides when the current listening session represents an album listen, stores release and track progress in SQLite, publishes completed releases to WordPress, and uses Discord as the human control plane for approvals, early publishing, undo, and status checks.

## Table Of Contents

- [Program Overview](#program-overview)
- [Startup And Runtime Flow](#startup-and-runtime-flow)
- [Configuration](#configuration)
- [Data Models And State](#data-models-and-state)
- [Database](#database)
- [Spotify Integration](#spotify-integration)
- [Tracker Loop And Core Logic](#tracker-loop-and-core-logic)
- [WordPress Publishing](#wordpress-publishing)
- [Discord Control Plane](#discord-control-plane)
- [Helpers](#helpers)
- [Function And Method Catalog](#function-and-method-catalog)
- [Testing](#testing)
- [Deployment And Operations](#deployment-and-operations)
- [Ambiguities And Current Behavior Notes](#ambiguities-and-current-behavior-notes)

## Program Overview

The program is built around one recurring question: "Is the user currently listening to a Spotify album in a way that should count toward an album post?" If yes, the tracker records progress. When enough tracks have been heard, it asks for or performs publishing actions.

The service ties together four systems:

- Spotify: source of playback state, album metadata, track lists, saved-library data, and artwork URLs.
- SQLite: local durable state for releases, tracks, saved-library albums and snapshots, WordPress post cache, prompts, audit events, and service state.
- WordPress: destination for published album posts, categories, tags, and uploaded artwork.
- Discord: control plane for the operator through slash commands, DMs, buttons, and modals.

Main components:

- `main.py`: process entry point and service orchestration.
- `src/config.py`: environment loading, path setup, token persistence, and config validation.
- `src/database.py`: SQLite connection, migrations, release storage, WordPress cache, Discord prompt state, audit log, and service state.
- `src/spotify_client.py`: async Spotify Web API client and OAuth token handling.
- `src/saved_library.py`: saved Spotify library synchronization and listen-to-list reconciliation.
- `src/tracker.py`: the main playback polling loop and release lifecycle logic.
- `src/publisher.py`: high-level WordPress publishing workflow.
- `src/wordpress_client.py`: low-level WordPress REST API client.
- `src/discord_bot.py`: Discord slash commands, buttons, modals, DMs, and presence updates.
- `src/lastfm_client.py`: async Last.fm client used to fill the SCF mood-tag repeater.
- `src/models.py`: enums and dataclasses used across the application.
- `src/utils.py`: normalization and release classification helpers.
- `src/inprogress.py`: pure helpers for `/inprogress` pagination.
- `src/logging_config.py`: console and rotating file logging.
- `scripts/migrate.py`: standalone migration runner.
- `migrations/*.sql`: SQLite schema evolution.
- `tests/test_unit.py`: unit tests for core logic and integration-adjacent behavior.

The fundamental idea is not "post every Spotify album immediately." The program is more conservative:

- It only tracks album-context playback, not playlists, shuffled album playback, local tracks, or non-track items.
- It tracks progress per countable track, not elapsed duration.
- It auto-skips singles for tracking.
- It checks for existing WordPress posts and requires approval before tracking duplicates as "Relisten" posts.
- It mirrors saved Spotify Albums and EPs into a local listen-to list for `/random` and library completion stats.
- It gives the operator Discord controls for early publishing, undoing a post, adding post content, removing tracked releases, picking a random listen-to album, and checking status.

## Startup And Runtime Flow

### Entry Point

`main.py` inserts `src/` into `sys.path`, configures logging, imports the core classes, and runs `asyncio.run(main())`.

`main()`:

1. Creates a `Service`.
2. Registers signal handlers for `SIGINT` and `SIGTERM`.
3. Calls `await service.start()`.
4. On error, logs the exception, stops the service if possible, and exits with status `1`.

The signal handler logs the signal, schedules `service.stop()`, and calls `sys.exit(0)`. Because the stop call is scheduled and then the process exits, shutdown is best-effort in the current implementation.

### `Service.__init__`

The service constructor wires the dependency graph:

1. `Config()`
2. `Database(config)`
3. `Publisher(config, db)`
4. `Tracker(config, db, publisher)`
5. `SavedLibraryService(db, tracker.spotify)`
6. `DiscordBot(config, db, tracker)`
7. `tracker.set_discord_bot(discord_bot)`

This creates a circular collaboration intentionally: the tracker can send Discord prompts, and Discord actions can call tracker and publisher behavior.

### `Service.start`

Startup order:

1. Log service startup.
2. Ensure Spotify authorization with `tracker.spotify.ensure_authorized()`.
3. Initialize SQLite and run migrations with `db.initialize()`.
4. Refresh the WordPress post cache and synchronize saved Spotify library albums with `_refresh_saved_library()`.
5. Start the Discord bot task.
6. Wait for Discord readiness with `discord_bot.wait_until_ready()`.
7. Start the tracker task.
8. Start a 24-hour saved-library sync loop.
9. Await all long-running tasks forever with `asyncio.gather`.

The tracker does not begin polling until Discord is logged in and slash commands are synced.

### `Service.stop`

Shutdown order:

1. Cancel the saved-library sync task if it exists.
2. `tracker.stop()`
3. `discord_bot.stop()`
4. `publisher.close()`
5. `db.close()`

This closes the Spotify HTTP client through the tracker, closes the Discord client, closes the WordPress HTTP client, and closes the SQLite connection.

## Configuration

Configuration is loaded by `Config` in `src/config.py`.

### Paths

Paths are relative to the project root:

- Database: `data/album_tracker.db`
- Spotify token file: `data/.spotify_tokens`
- Logs directory: `logs/`
- Env file: `.env`

`Config.__init__` creates `data/` automatically with `mkdir(parents=True, exist_ok=True)`.

### Environment Loading

If `.env` exists, `_load_env()` uses `python-dotenv` to load it. Environment variables already present in the process are also available through `os.getenv`.

### Environment Variables

Spotify:

- `SPOTIFY_CLIENT_ID`: required. Spotify application client ID.
- `SPOTIFY_CLIENT_SECRET`: required. Spotify application client secret.
- `SPOTIFY_REDIRECT_URI`: optional. Defaults to `https://musicblog.callita.day` in code. `.env.example` shows `http://localhost:8080/callback`.
- `SPOTIFY_ACCESS_TOKEN`: optional. If absent, loaded from `data/.spotify_tokens` when available.
- `SPOTIFY_REFRESH_TOKEN`: optional. If absent, loaded from `data/.spotify_tokens` when available.

WordPress:

- `WORDPRESS_URL`: optional in code because it defaults to `http://10.17.3.3:8085`, but treated as required after defaulting. Used for authenticated REST API calls.
- `WORDPRESS_PUBLIC_URL`: optional. Defaults to `https://musicblog.callita.day`. Used to rewrite WordPress links shown in Discord notifications.
- `WORDPRESS_USERNAME`: required. WordPress REST API username.
- `WORDPRESS_APP_PASSWORD`: required. WordPress application password.

Discord:

- `DISCORD_BOT_TOKEN`: required. Discord bot token.
- `DISCORD_USER_ID`: required. Numeric Discord user ID. It is immediately converted to `int`.

Last.fm and SCF auto-fill:

- `LASTFM_API_KEY`: optional unless `SPOTIFY_BLOG_TRACKER_FILL_SCF=1`. Last.fm API key used to look up `album.mbid` and the SCF `music_mood_tags` repeater on every Discord publish.
- `SPOTIFY_BLOG_TRACKER_FILL_SCF`: optional. Defaults to `1` (SCF auto-fill enabled after every Discord-published post). Set to `0` to opt out. When enabled (the default), `LASTFM_API_KEY` becomes required.

Logging:

- `LOG_LEVEL`: optional. Used by `logging_config._resolve_log_level`; defaults to `INFO`.

### Validation

`Config._validate()` requires Spotify client credentials, WordPress credentials, Discord bot token, and Discord user ID. When `SPOTIFY_BLOG_TRACKER_FILL_SCF=1`, `LASTFM_API_KEY` is added to the required list. Missing values raise `ValueError`.

Important behavior: `DISCORD_USER_ID` is converted with `int(os.getenv("DISCORD_USER_ID"))` before `_validate()` runs. If it is absent or non-numeric, object construction fails before the missing-config list can be produced.

### Persisted Spotify Tokens

`Config._load_persisted_tokens()` reads `data/.spotify_tokens` as JSON:

```json
{
  "access_token": "spotify-access-token",
  "refresh_token": "spotify-refresh-token"
}
```

`Config.save_tokens(access_token, refresh_token)` writes the same shape. These persisted tokens let later runs skip the initial OAuth code paste flow.

### Database URL

`Config.database_url` returns a SQLAlchemy-style URL:

```text
sqlite+aiosqlite:///.../data/album_tracker.db
```

The current app uses `aiosqlite` directly rather than SQLAlchemy, so this property is currently a convenience/compatibility value rather than a core runtime dependency.

## Data Models And State

Models live in `src/models.py`. They are plain enums and dataclasses shared by the tracker, database, publisher, and Discord bot.

### Enums

`ReleaseType`:

- `ALBUM = "Album"`
- `EP = "EP"`
- `SINGLE = "Single"`
- `COMPILATION = "Compilation"`

This value becomes a WordPress category and drives whether a release is auto-tracked. Singles are skipped for automatic tracking.

`LifecycleStatus`:

- `ACTIVE`: the release is being tracked.
- `AWAITING_75_DECISION`: the release reached at least 75 percent and a Discord prompt has been sent.
- `AWAITING_RELISTEN_DECISION`: legacy value. Current code should not create new rows with this status.
- `PUBLISHING`: a publish operation is in progress.
- `PUBLISHED_RECENTLY`: post-publish retention state. Rows remain briefly for undo/idempotency, then are purged after the retention window.

`PromptType`:

- `PROMPT_75_PERCENT = "75_percent"`: prompt to publish early or wait.
- `PROMPT_RELISTEN_APPROVAL = "relisten"`: prompt to approve tracking a duplicate as a relisten.
- `PROMPT_UNDO = "undo"`: prompt shown after publish for add content, undo post, or keep post.

`PromptState`:

- `PENDING`: prompt can still be handled.
- `ACCEPTED`: prompt action was accepted.
- `DECLINED`: prompt action was declined or intentionally left alone.
- `USED`: defined but not actively used by the current core paths.
- `EXPIRED`: prompt passed its expiration time.

### Dataclasses

`Artist`:

```python
Artist(
    spotify_id="artist-id",
    name="Artist Name",
    normalized_name="artist name",
)
```

`Track`:

```python
Track(
    spotify_id="track-id",
    title="Song Title",
    normalized_title="song title",
    duration_ms=245000,
    disc_number=1,
    track_number=3,
    is_countable=True,
    listened=False,
    listened_at=None,
    listened_source=None,
    highlight=False,
)
```

`is_countable` controls progress. Non-countable tracks do not affect completion. `highlight` is the SCF `music_tracks` row sub-field editable through the Discord editor.

`Release`:

```python
Release(
    spotify_id="album-id",
    title="Album Title",
    normalized_title="album title",
    artists=[Artist(...)],
    release_type=ReleaseType.ALBUM,
    raw_spotify_type="album",
    cover_url="https://...",
    release_date="2026-01-01",
    total_tracks=10,
    total_duration_ms=2400000,
    tracks=[Track(...)],
    progress=0.0,
    status=LifecycleStatus.ACTIVE,
    first_seen=datetime.now(),
    last_seen=datetime.now(),
    completed_at=None,
    published_at=None,
    wordpress_post_id=None,
    wordpress_media_id=None,
    is_relisten=False,
    duplicate_state="none",
    duplicate_post_id=None,
    rating=None,
    favorite=False,
    notes=None,
    unreleased=False,
)
```

`rating` (0-100), `favorite`, `notes`, and `unreleased` are the SCF human-curated fields exposed by the Discord editor. `release.tracks[i].highlight` carries the per-track row value. All four release fields and `track.highlight` round-trip through SQLite (migration 006) and through the SCF auto-fill payload on publish.

`PlaybackState`:

```python
PlaybackState(
    is_playing=True,
    shuffle_state=False,
    repeat_state="off",
    context={"type": "album", "uri": "spotify:album:..."},
    item={"type": "track", "album": {...}},
    progress_ms=90000,
    timestamp=1710000000000,
)
```

`WordPressPost` stores the cached WordPress post data used for duplicate detection:

```python
WordPressPost(
    id=123,
    title="Album Title",
    normalized_title="album title",
    artists=["Artist Name"],
    normalized_artists=["artist name"],
    link="https://musicblog.example/album-title",
)
```

`SavedLibraryAlbum` stores one current Spotify saved-library Album or EP:

```python
SavedLibraryAlbum(
    spotify_id="album-id",
    spotify_uri="spotify:album:album-id",
    spotify_url="https://open.spotify.com/album/album-id",
    title="Album Title",
    normalized_title="album title",
    artists=["Artist Name"],
    normalized_artists=["artist name"],
    album_type="album",
    release_type=ReleaseType.ALBUM,
    cover_url="https://...",
    added_at=datetime.now(),
    is_posted_listened=False,
    wordpress_post_id=None,
)
```

`SavedLibraryStats` contains total saved-library rows, posted/listened rows, and the posted/listened percentage. `SavedLibrarySyncResult` summarizes one saved-library synchronization run.

`SavedLibrarySnapshotItem` stores one lightweight identity row from the complete Spotify saved library:

```python
SavedLibrarySnapshotItem(
    spotify_id="album-id",
    spotify_uri="spotify:album:album-id",
    added_at=datetime.now(),
    position=0,
    last_seen_at=datetime.now(),
)
```

The snapshot model intentionally does not duplicate album title, artists, cover art, or posted/listened state. Those richer fields live in `SavedLibraryAlbum` only when the saved item qualifies for the listen-to list.

`DiscordPrompt` stores prompt state for persistent button handling:

```python
DiscordPrompt(
    id=1,
    prompt_type="75_percent",
    discord_message_id="123456789",
    state="pending",
    release_id="album-id",
    wordpress_post_id=None,
    created_at=datetime.now(),
    expires_at=None,
    context_json=None,
)
```

## Database

The database layer uses `aiosqlite`. `Database.initialize()` opens `data/album_tracker.db`, enables WAL mode, enables foreign keys, and runs pending SQL migrations from `migrations/`.

### Migrations

`001_initial_schema.sql` creates the base schema:

- `schema_version`
- `release_lifecycle`
- `release_artist`
- `release_track`
- `wordpress_post_cache`
- `discord_prompt`
- `audit_event`
- `service_state`

`002_relisten_approval_flow.sql` adds:

- `release_lifecycle.is_relisten`
- `discord_prompt.created_at`
- `discord_prompt.expires_at`
- `discord_prompt.context_json`
- index `idx_discord_prompt_release_type_state`

It also expires old relisten prompts and deletes active duplicate rows that were created before the approval-before-tracking flow.

`003_remove_terminal_lifecycle_statuses.sql` deletes old terminal history rows with statuses:

- `published`
- `trashed_post`
- `ignored_single`
- `deleted`

Current code keeps a published release only in `PUBLISHED_RECENTLY` for a short retention window, then purges it.

`004_saved_library_albums.sql` adds:

- `saved_library_album`
- indexes for posted/listened filtering and saved date ordering

This table mirrors the current Spotify saved library for items the app treats as Albums or EPs.

`005_saved_library_snapshot.sql` adds:

- `saved_library_snapshot_item`
- indexes for saved-library order and saved date lookup

This table is a complete ordered identity snapshot of the current Spotify saved library. It exists so saved-library sync can reconcile common additions and removals without requesting every `/me/albums` page.

`006_editor_scf_fields.sql` adds:

- `release_lifecycle.rating` (INTEGER, nullable)
- `release_lifecycle.favorite` (BOOLEAN, not null, default 0)
- `release_lifecycle.notes` (TEXT, nullable)
- `release_lifecycle.unreleased` (BOOLEAN, not null, default 0)
- `release_track.highlight` (BOOLEAN, not null, default 0)

These are the SCF human-curated fields editable through the Discord editor (see `src/editor_view.py`). They are read back through `db.get_release(...)` on every editor interaction, and propagated into the SCF auto-fill payload on publish (see `Publisher._build_scf_payload`).

### Tables

`schema_version`:

- `version INTEGER PRIMARY KEY`

Stores the latest applied migration version.

`release_lifecycle`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `spotify_id TEXT UNIQUE NOT NULL`
- `title TEXT NOT NULL`
- `normalized_title TEXT NOT NULL`
- `release_type TEXT NOT NULL`
- `raw_spotify_type TEXT NOT NULL`
- `cover_url TEXT NOT NULL`
- `release_date TEXT NOT NULL`
- `total_tracks INTEGER NOT NULL`
- `total_duration_ms INTEGER NOT NULL`
- `progress REAL NOT NULL DEFAULT 0.0`
- `status TEXT NOT NULL`
- `first_seen TEXT NOT NULL`
- `last_seen TEXT NOT NULL`
- `completed_at TEXT`
- `published_at TEXT`
- `wordpress_post_id INTEGER`
- `wordpress_media_id INTEGER`
- `duplicate_state TEXT`
- `duplicate_post_id INTEGER`
- `is_relisten BOOLEAN NOT NULL DEFAULT 0`

This table is the primary lifecycle record for each tracked Spotify album/release.

`release_artist`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `release_id INTEGER NOT NULL`
- `spotify_id TEXT NOT NULL`
- `name TEXT NOT NULL`
- `normalized_name TEXT NOT NULL`
- Foreign key: `release_id` references `release_lifecycle(id)` with `ON DELETE CASCADE`.

Artists are stored separately so a release can have multiple artists.

`release_track`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `release_id INTEGER NOT NULL`
- `spotify_id TEXT NOT NULL`
- `title TEXT NOT NULL`
- `normalized_title TEXT NOT NULL`
- `duration_ms INTEGER NOT NULL`
- `disc_number INTEGER NOT NULL`
- `track_number INTEGER NOT NULL`
- `is_countable BOOLEAN NOT NULL DEFAULT 1`
- `listened BOOLEAN NOT NULL DEFAULT 0`
- `listened_at TEXT`
- `listened_source TEXT`
- Foreign key: `release_id` references `release_lifecycle(id)` with `ON DELETE CASCADE`.

Tracks are ordered by disc and track number when reloaded.

`wordpress_post_cache`:

- `id INTEGER PRIMARY KEY`
- `title TEXT NOT NULL`
- `normalized_title TEXT NOT NULL`
- `artists_json TEXT NOT NULL`
- `normalized_artists_json TEXT NOT NULL`
- `link TEXT NOT NULL`

This table caches published WordPress posts and artist tags for duplicate detection.

`saved_library_album`:

- `spotify_id TEXT PRIMARY KEY`
- `spotify_uri TEXT NOT NULL`
- `spotify_url TEXT NOT NULL`
- `title TEXT NOT NULL`
- `normalized_title TEXT NOT NULL`
- `artists_json TEXT NOT NULL`
- `normalized_artists_json TEXT NOT NULL`
- `album_type TEXT NOT NULL`
- `release_type TEXT NOT NULL`
- `cover_url TEXT NOT NULL`
- `added_at TEXT NOT NULL`
- `is_posted_listened BOOLEAN NOT NULL DEFAULT 0`
- `wordpress_post_id INTEGER`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

This table is the listen-to list. It is kept as an exact mirror of current Spotify saved Albums and EPs, so albums removed from Spotify are deleted locally. `is_posted_listened` is the canonical completion flag and means the album has a matching published WordPress post.

`saved_library_snapshot_item`:

- `spotify_id TEXT PRIMARY KEY`
- `spotify_uri TEXT NOT NULL`
- `added_at TEXT NOT NULL`
- `position INTEGER NOT NULL`
- `last_seen_at TEXT NOT NULL`

This table stores the complete Spotify saved-library order for all saved albums, including items that are excluded from `saved_library_album`. It contains only identity and ordering metadata so incremental sync can detect additions and sparse removals with far fewer Spotify requests.

`discord_prompt`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `prompt_type TEXT NOT NULL`
- `release_id TEXT`
- `wordpress_post_id INTEGER`
- `discord_message_id TEXT UNIQUE NOT NULL`
- `state TEXT NOT NULL`
- `created_at TEXT`
- `expires_at TEXT`
- `context_json TEXT`

Prompts are looked up by Discord message ID when a button is clicked.

`audit_event`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `event_type TEXT NOT NULL`
- `data_json TEXT NOT NULL`
- `timestamp TEXT NOT NULL`

This is an append-only log of important lifecycle events.

`service_state`:

- `key TEXT PRIMARY KEY`
- `value TEXT NOT NULL`

Used for lightweight key-value state, including current playback string and WordPress cache validation metadata.

Saved-library sync stores validation state under:

- `spotify_saved_library.total`
- `spotify_saved_library.first_page_hash`
- `spotify_saved_library.last_synced_at`
- `spotify_saved_library.last_full_audit_at`

### Relationships And Retention

- `release_artist` and `release_track` are attached to `release_lifecycle` by numeric `release_id`.
- Deleting a release cascades to artists and tracks.
- `Database.delete_release()` also deletes Discord prompts by Spotify release ID.
- `Database.delete_published_releases_older_than()` deletes `PUBLISHED_RECENTLY` releases after the tracker retention window.
- `saved_library_album` is independent from `release_lifecycle`; publish and undo flows update it by Spotify album ID when a row exists.
- Saved-library removals are hard deletes so the table mirrors the current Spotify library, not historical library membership.
- Discord prompt rows are not general history; many are removed with their associated release.
- Audit events are not deleted by normal release deletion paths.

### Release Persistence Behavior

`Database.save_release()` uses `INSERT OR REPLACE` on `release_lifecycle`, then deletes and reinserts the release's artists and tracks.

This is simple and keeps the saved object authoritative, but it means:

- Track rows are rewritten whenever the release is saved.
- Artist rows are rewritten whenever the release is saved.
- The numeric `release_lifecycle.id` may change after replacement depending on SQLite behavior and conflict handling.

### Useful SQLite Queries

Open the actual database path:

```bash
sqlite3 data/album_tracker.db
```

Active releases:

```sql
SELECT spotify_id, title, release_type, progress, status, last_seen
FROM release_lifecycle
ORDER BY last_seen DESC;
```

Tracks for a release:

```sql
SELECT rt.disc_number, rt.track_number, rt.title, rt.is_countable, rt.listened, rt.listened_at
FROM release_track rt
JOIN release_lifecycle rl ON rl.id = rt.release_id
WHERE rl.spotify_id = 'spotify-album-id'
ORDER BY rt.disc_number, rt.track_number;
```

Pending prompts:

```sql
SELECT prompt_type, release_id, wordpress_post_id, discord_message_id, state, created_at, expires_at
FROM discord_prompt
WHERE state = 'pending'
ORDER BY id DESC;
```

Cached WordPress posts:

```sql
SELECT id, title, artists_json, link
FROM wordpress_post_cache
ORDER BY id DESC
LIMIT 20;
```

Saved-library listen-to list:

```sql
SELECT title, artists_json, added_at, is_posted_listened, wordpress_post_id
FROM saved_library_album
ORDER BY added_at DESC
LIMIT 20;
```

Saved-library stats:

```sql
SELECT COUNT(*) AS total,
       SUM(CASE WHEN is_posted_listened THEN 1 ELSE 0 END) AS listened
FROM saved_library_album;
```

Recent audit events:

```sql
SELECT event_type, data_json, timestamp
FROM audit_event
ORDER BY timestamp DESC
LIMIT 20;
```

WordPress cache validation state:

```sql
SELECT key, value
FROM service_state
WHERE key LIKE 'wordpress_post_cache.%';
```

### Database Quirks

`Database.get_service_state()` has an unreachable `await self.connection.commit()` after `return`. This does not affect reads, but it is dead code.

`Database._row_to_release()` includes compatibility logic around `is_relisten` and `duplicate_state`. The current schema has `is_relisten`, but the code still handles older row shapes.

## Spotify Integration

`SpotifyClient` wraps Spotify Web API calls with `httpx.AsyncClient`.

Base API URL:

```text
https://api.spotify.com/v1
```

Authorization URL:

```text
https://accounts.spotify.com/authorize
```

Token URL:

```text
https://accounts.spotify.com/api/token
```

### OAuth Behavior

`ensure_authorized()` returns immediately if both access and refresh tokens are available. Otherwise it calls `_authorize()`.

`_authorize()`:

1. Generates a PKCE code verifier and code challenge.
2. Builds the Spotify authorization URL.
3. Opens the URL in the local browser with `webbrowser.open`.
4. Calls `_wait_for_callback()`.
5. Exchanges the code for tokens with `_exchange_code_for_tokens`.

Current implementation note: `_wait_for_callback()` does not start a callback web server. It asks the user to paste the `code` parameter from the redirect URL into the terminal.

Requested scopes:

```text
user-read-playback-state user-read-recently-played user-library-read
```

`user-library-read` is required for saved-library synchronization and for checking whether a published album is saved in the user's Spotify library. Existing persisted tokens created before this scope was added may need to be regenerated through the authorization flow.

### Token Refresh

`_ensure_token()` refreshes the access token when:

- `token_expires_at` is missing, or
- `token_expires_at` is in the past.

Refresh uses:

- `grant_type=refresh_token`
- `refresh_token`
- `client_id`
- `client_secret`

The refreshed access token, and possibly refreshed refresh token, are saved to `data/.spotify_tokens`.

### Spotify Endpoints Used

`get_playback_state()`:

```http
GET /me/player
```

Returns current playback state, or `None` on HTTP `204 No Content`.

`get_album(album_id)`:

```http
GET /albums/{album_id}
```

Returns album metadata such as title, artists, images, release date, and Spotify album type.

`get_album_tracks(album_id)`:

```http
GET /albums/{album_id}/tracks?limit=50
```

Follows pagination using the `next` URL until no pages remain.

`get_recently_played(limit=50)`:

```http
GET /me/player/recently-played?limit={limit}
```

This method exists and is tested by type/import paths indirectly, but the tracker loop does not currently use recently played data.

`get_saved_albums_page(limit=50, offset=0, url=None)`:

```http
GET /me/albums?limit=50&offset=0
```

Returns one page of saved album items. Spotify's limit is capped at 50. When `url` is supplied, the client follows Spotify's paging `next` URL directly.

`get_all_saved_albums(first_page=None)` follows the saved-album paging `next` links until no pages remain.

`check_library_contains_album(album_id_or_uri)`:

```http
GET /me/library/contains?uris=spotify:album:{album_id}
```

Returns whether the album URI is saved in the user's Spotify library. The code uses the current generic library contains endpoint rather than the older album-specific contains endpoint.

## Last.fm Integration

The Last.fm client in `src/lastfm_client.py` is a read-only helper used exclusively by the SCF auto-fill in `Publisher`. It deliberately has no fuzzy-search ladder (the Discord flow always knows the Spotify album ID, so there is nothing to match), only two end uses:

### Endpoints Used

`album_getinfo(artist, album)`:

```http
GET https://ws.audioscrobbler.com/2.0/?method=album.getinfo&artist=<a>&album=<b>&api_key=<k>&format=json
```

Returns `{"mbid": str, "tags": [{"name": str}, ...]}`, or `{}` on missing key, blank inputs, HTTP errors, or unexpected payloads.

### Mood Tag Filtering

`pick_mood_tags(album_info, max_n=3)` flattens tag names, drops empty entries, applies the complement-script blocklist (`LFM_TAG_BLOCKLIST` in `lastfm_client.py`), and keeps the top `max_n`. The intent is to match exactly the tag set that `Wordpress-PostToAlbum-Script` would write for the same release, so the Discord-driven fill and the manual complement script can be used interchangeably on the same blog.

### Playback State Shape Used By The Tracker

The tracker depends on these fields:

```python
{
    "is_playing": True,
    "shuffle_state": False,
    "repeat_state": "off",
    "context": {
        "type": "album",
        "uri": "spotify:album:..."
    },
    "item": {
        "id": "track-id",
        "type": "track",
        "is_local": False,
        "album": {
            "id": "album-id",
            "album_type": "album",
            "uri": "spotify:album:..."
        }
    },
    "progress_ms": 123000,
    "timestamp": 1710000000000
}
```

## Saved Spotify Library Sync

`SavedLibraryService` in `src/saved_library.py` keeps the local listen-to list in sync with Spotify saved albums.

### Sync Timing

Startup calls `_refresh_saved_library()`, which refreshes the WordPress post cache first and then runs `SavedLibraryService.sync()`. A background loop repeats the same refresh every 24 hours while the service is running.

### Change Detection

The first saved-albums request always asks Spotify for the first 50 saved albums:

```http
GET /me/albums?limit=50&offset=0
```

The sync stores two validation values in `service_state`:

- `spotify_saved_library.total`: the Spotify endpoint's `total` value.
- `spotify_saved_library.first_page_hash`: a stable SHA-256 hash of first-page album IDs plus `added_at` values.

The sync also stores a complete lightweight ordered identity snapshot in `saved_library_snapshot_item`. That snapshot includes all saved Spotify albums returned by `/me/albums`, including singles and compilations that are not eligible for the listen-to list. The richer `saved_library_album` table remains limited to Albums and EPs.

If both validation values match the previous run, the sync stops without fetching later pages. If either value differs, the sync first tries an incremental reconciliation against the local snapshot:

- Head additions: fetch only enough head pages to find an existing snapshot item, then prepend the new Spotify items locally.
- Sparse removals: binary-search changed saved-album pages to identify removed IDs without walking every page.
- Mixed head additions plus removals: apply the detected head additions, then use the sparse-removal probe path.

The incremental path falls back to a full scan when the change is ambiguous, when more than 10 removals would need probing, when no nearby snapshot boundary can be found within 10 head pages, when validation state is missing, when the snapshot count does not match the last saved Spotify total, when the snapshot table is missing/empty for a non-empty library, or when the weekly full audit is due.

Full scans still follow every `next` link and reconcile all local state. They also refresh `spotify_saved_library.last_full_audit_at`.

### Reconciliation Rules

The sync includes saved items whose Spotify `album_type` is `album` and whose computed local `ReleaseType` is `Album` or `EP`. Singles and compilations are excluded from the listen-to list.

Release type is computed from the data already embedded in each saved-albums response item whenever possible. Albums with `total_tracks >= 7` are classified as `Album` immediately, and albums with 6 or fewer tracks use `album.tracks.items` from the `/me/albums` payload. The sync only calls `GET /albums/{album_id}/tracks` as a fallback when Spotify returns incomplete embedded track data for a low-track-count album.

For each included album, the sync stores Spotify ID, URI, Spotify URL, title, normalized title, artists, normalized artists, raw album type, computed release type, cover URL, and the `added_at` timestamp.

Existing WordPress posts are matched using the same duplicate-detection shape as the tracker:

- normalized album title must match
- normalized artist set must match exactly

Matching entries are marked `is_posted_listened=True` and store `wordpress_post_id`. Non-matching entries remain eligible for `/random`.

If an album disappears from the current Spotify saved-library response, its snapshot row is removed. Its `saved_library_album` row is also deleted when one exists, even if it was previously posted.

## Tracker Loop And Core Logic

`Tracker` in `src/tracker.py` is the heart of the application.

### Polling Intervals

Configured in `Tracker.__init__`:

- `active_interval = 3`: when actively tracking qualifying playback.
- `paused_interval = 8`: when playback exists but does not qualify.
- `idle_interval = 15`: when Spotify reports no active playback.
- `backoff_interval = 60`: after an exception in the outer run loop.

Retention constants:

- `RELISTEN_APPROVAL_TTL = 24 hours`
- `PUBLISHED_RELEASE_RETENTION = 24 hours`
- `PUBLISHED_RELEASE_CLEANUP_INTERVAL = 5 minutes`

### `Tracker.run`

`run()` sets `self.running = True` and loops while running:

1. Calls `_cleanup_published_releases_if_due()`.
2. Calls `_poll_once()`.
3. If an exception escapes, logs it and sleeps for `backoff_interval`.

The loop itself does not sleep directly on success. `_poll_once()`, `_handle_idle()`, and `_handle_non_qualifying()` perform the sleeps for their branch.

### `_poll_once`

One polling iteration:

1. Fetch current Spotify playback with `spotify.get_playback_state()`.
2. If no playback, call `_handle_idle()` and return.
3. Parse raw Spotify data into `PlaybackState`.
4. If playback does not qualify, call `_handle_non_qualifying(state)` and return.
5. Read album ID from `state.item["album"]["id"]`.
6. Load the release from SQLite.
7. If the loaded release is `PUBLISHED_RECENTLY` and retention expired, delete it and treat playback as a new release.
8. If no release exists, build a candidate release from Spotify.
9. If the candidate is a `Single`, skip tracking and treat it as non-qualifying.
10. If the candidate matches a cached WordPress post, send or reuse a relisten approval prompt and do not save the release yet.
11. If tracking is allowed, save the release.
12. Match the current Spotify track to a stored release track.
13. If matched and not already listened, mark the track listened and recompute progress.
14. At 75 percent, send a Discord prompt if one has not already been sent.
15. At 100 percent, publish the release.
16. If the track was already listened, only touch `last_seen`.
17. Save current playback state in `service_state` and update Discord presence.
18. Sleep for the active interval.

### Tracking Qualification

`_qualifies_for_tracking(state)` returns `True` only when all of these are true:

- Spotify says playback is active: `state.is_playing`.
- `state.item` exists.
- The item type is `track`.
- The item has an `album`.
- The Spotify album type is `album`.
- The track is not local.
- Playback has a context.
- The context type is `album`.
- The context URI equals the item's album URI.
- Shuffle is off.

This intentionally excludes playlist playback, shuffled album playback, paused playback, local files, podcasts/episodes, and loose track playback where Spotify does not report an album context.

### Release Creation

`_build_release_from_spotify(album_id)` fetches:

- album metadata through `SpotifyClient.get_album`
- all album tracks through `SpotifyClient.get_album_tracks`

It then:

- Converts Spotify artists to `Artist` objects.
- Converts Spotify tracks to `Track` objects.
- Marks tracks countable when they are not local and are playable.
- Computes release type with `compute_release_type`.
- Computes total duration from countable tracks.
- Creates an unsaved `Release` with `ACTIVE` status and `progress=0.0`.

`_create_tracked_release(release, is_relisten=False, duplicate_post_id=None)` sets relisten/duplicate fields, saves the release, and logs a `release_created` audit event.

### Progress Calculation

`_mark_track_listened(track, source)` mutates the in-memory track:

- `listened = True`
- `listened_at = datetime.now()`
- `listened_source = source`

`_recompute_release_progress(release)` counts:

```python
listened_count = count of tracks where is_countable and listened
countable_count = count of tracks where is_countable
progress = listened_count / countable_count
```

Progress is track-count based rather than duration based. A 10-track album reaches 50 percent after 5 countable tracks, even if those tracks are shorter than the remaining tracks.

When progress reaches `1.0` and the release is still `ACTIVE`, `_recompute_release_progress()` changes status to `PUBLISHING` and sets `completed_at`.

### 75 Percent Prompt

When a release reaches at least 75 percent:

1. `_has_75_prompt()` checks whether the release status is already `AWAITING_75_DECISION` or whether a Discord prompt exists.
2. `_send_75_prompt()` sets status to `AWAITING_75_DECISION`.
3. It logs `75_percent_prompt_sent`.
4. It asks Discord to DM a prompt.
5. If a Discord message is returned, it saves a `DiscordPrompt` with type `75_percent` and state `pending`.

The operator can:

- Publish now.
- Wait for full completion.

Waiting sets the prompt to `declined`, changes release status back to `ACTIVE`, and lets tracking continue.

### Completion And Publishing

`_handle_completion(release)` publishes the release unless it is already `PUBLISHED_RECENTLY`.

`_publish_release(release, as_relisten=False)`:

1. Sets status to `PUBLISHING`.
2. Marks relisten state if needed.
3. Saves the release.
4. Calls `publisher.publish_release`.
5. Sets status to `PUBLISHED_RECENTLY`.
6. Sets `published_at = datetime.now()`.
7. Saves the release.
8. Marks the matching `saved_library_album` row posted/listened if it exists.
9. Sends a Discord publish notification.
10. Checks whether the album is saved in the Spotify library.
11. If Spotify says the album is not saved, sends a Discord warning without blocking or rolling back the publish.
12. Logs `release_published`.

If publishing fails, it logs the error, resets status to `ACTIVE`, saves the release, and re-raises.

### Duplicate And Relisten Flow

Duplicate detection uses the cached WordPress post list. `_check_duplicate(release)` compares:

- normalized release title
- set of normalized release artist names

If both match a cached WordPress post, the album is considered a duplicate.

New duplicate flow:

1. A candidate release is built from Spotify but not saved.
2. `_start_tracking_or_prompt_for_relisten()` finds a duplicate post.
3. It checks for a live pending relisten prompt.
4. If none exists, `_send_relisten_approval_prompt()` DMs the operator.
5. The prompt expires after 24 hours.
6. Until approved, the release remains untracked.

Approval flow:

1. Discord calls `tracker.approve_relisten_tracking(prompt)`.
2. If prompt is expired, it is marked `expired`.
3. If the release does not exist, the tracker rebuilds it from Spotify.
4. If it is not a single, it saves it as `is_relisten=True`.
5. Prompt state becomes `accepted`.
6. Audit event `relisten_tracking_approved` is logged.

### Post-Publish Retention

Completed releases are not immediately deleted. They move to `PUBLISHED_RECENTLY` so button actions and idempotency checks have a short window to work.

Cleanup paths:

- `_cleanup_published_releases_if_due()` runs at most every 5 minutes.
- `_cleanup_published_releases()` deletes `PUBLISHED_RECENTLY` rows with `published_at` older than 24 hours.
- `_delete_published_release_if_expired()` handles the case where the user listens to the same album again after retention has expired but before periodic cleanup ran.

### Example Flows

First track of a new album:

1. Spotify reports album-context playback.
2. The release is not in SQLite.
3. The tracker fetches album metadata and tracks.
4. If not a single and not a duplicate, it saves an `ACTIVE` release.
5. The current track is marked listened.
6. Progress becomes `1 / countable_tracks`.

Album reaches 75 percent:

1. A newly listened track causes progress to cross `0.75`.
2. Release status becomes `AWAITING_75_DECISION`.
3. A Discord DM asks whether to publish now or wait.
4. "Publish now" publishes immediately.
5. "Wait for full completion" returns the release to `ACTIVE`.

Album completes:

1. Final countable track is marked listened.
2. Progress becomes `1.0`.
3. Status becomes `PUBLISHING`.
4. WordPress post is created with artwork, categories, and tags.
5. Status becomes `PUBLISHED_RECENTLY`.
6. Discord receives a notification with add content, undo post, and keep post buttons.

Duplicate album:

1. Tracker builds a candidate release.
2. Candidate title and artist set match a cached WordPress post.
3. The release is not saved.
4. Discord asks whether to track it as a Relisten.
5. If approved, it starts tracking as `is_relisten=True`.
6. When published, it includes the regular release category plus the `Relisten` category.

## WordPress Publishing

WordPress integration has two layers:

- `WordPressClient`: low-level REST API methods.
- `Publisher`: application-level publishing workflow.

### WordPress API Base

`WordPressClient.__init__` builds:

```text
{WORDPRESS_URL}/wp-json/wp/v2
```

Authentication uses Basic Auth:

```text
Authorization: Basic base64(username:application-password)
```

### WordPress REST Endpoints Used

Posts:

```http
GET /wp-json/wp/v2/posts
POST /wp-json/wp/v2/posts
POST /wp-json/wp/v2/posts/{post_id}
DELETE /wp-json/wp/v2/posts/{post_id}
```

Categories:

```http
GET /wp-json/wp/v2/categories?per_page=100
POST /wp-json/wp/v2/categories
```

Tags:

```http
GET /wp-json/wp/v2/tags
GET /wp-json/wp/v2/tags/{tag_id}
POST /wp-json/wp/v2/tags
```

Media:

```http
POST /wp-json/wp/v2/media
POST /wp-json/wp/v2/media/{media_id}
DELETE /wp-json/wp/v2/media/{media_id}
```

### Publishing A Release

`Publisher.publish_release(release, as_relisten=False)`:

1. Ensures required categories exist:
   - `Album`
   - `EP`
   - `Single`
   - `Compilation`
   - `Relisten`
2. Downloads the Spotify artwork from `release.cover_url`.
3. Uploads the artwork to WordPress media.
4. Resolves or creates artist tags.
5. Builds the category list from `release.release_type`.
6. Adds `Relisten` category if `as_relisten=True`.
7. When `Config.fill_scf_enabled` is true, computes the listen-count for the SCF `listen-count` field BEFORE `create_post`, so the about-to-be-created post is not double-counted (see [SCF Auto-Fill](#scf-auto-fill)).
8. Creates a published WordPress post.
9. When `Config.fill_scf_enabled` is true, fills the SCF `acf` block on the new post (see [SCF Auto-Fill](#scf-auto-fill)).
10. Stores `wordpress_post_id` and `wordpress_media_id` on the release object.
11. Forces a WordPress post-cache refresh.
12. Returns a `PublishResult` with `post`, `scf_pending_tags`, and `listen_count` (see [Data Models](#data-models-and-state)).

Post data shape:

```python
{
    "title": release.title,
    "content": "",
    "status": "publish",
    "categories": category_ids,
    "tags": tag_ids,
    "featured_media": media_id or 0,
}
```

### SCF Auto-Fill

When the `SPOTIFY_BLOG_TRACKER_FILL_SCF=1` feature flag is set, every Discord-published release gets the same SCF `acf` block that `Wordpress-PostToAlbum-Script` writes for the rest of the blog. The fill happens in two steps on the new post, both of which are no-ops when the flag is off:

1. `_build_scf_payload(release, listen_count, post)` serialises what's already in the in-memory `Release` (album metadata, track list, artwork URL, release date) plus one Last.fm call (`album.getinfo`) into the `acf` payload. It returns `(acf, fetch_status)` where `fetch_status["mood_tags"]` is `None` when Last.fm returned no usable tags and a list otherwise.
2. `_fill_post_scf(post_id, acf_payload)` `POST /wp/v2/posts/{id}` with `{"acf": ...}`. SCF keeps any un-supplied meta intact on update.

#### Fields

All sources are Spotify (in-memory on the `Release`) or derived from Spotify data, except `lastfm_release_id` and `music_mood_tags`, which come from Last.fm.

| SCF field | Type | Source |
| --- | --- | --- |
| `music_tracks` | repeater | countable tracks in the Release (`is_countable=True`) |
| `music_length_ms` | number | `sum(t.duration_ms for t in countable_tracks)` |
| `spotify_album_id` | text | `release.spotify_id` |
| `spotify_album_url` | url | `https://open.spotify.com/album/{id}` |
| `music_release_date` | date_picker | Spotify `release_date` (coerced YYYY/YYYY-MM to first-of-month, formatted `d/m/Y`) |
| `music_listened_at` | date_picker | the post's own `date` field (formatted `d/m/Y`) |
| `lastfm_release_id` | text | Last.fm `album.getinfo` → `album.mbid` (empty if absent) |
| `music_total_tracks` | number | `len(countable_tracks)` |
| `music_avg_track_ms` | number | `music_length_ms // music_total_tracks` |
| `music_explicit` | true_false | `any(t.explicit for t in countable_tracks)` |
| `music_mood_tags` | repeater | Top 3 Last.fm tags after the complement-script blocklist |
| `listen-count` | number | `_count_listen_index(release)` (see below) |

Each `music_tracks` row is built from a countable Track as `{disc_number, track_number, title, duration_ms, spotify_id, highlight:false, explicit: t.explicit}`. Track `explicit` is captured at tracking time in `_build_release_from_spotify` from the Spotify track payload; it defaults to `False` when the API does not return the field on simplified album-track objects.

The blocklist for mood tags is the same one used by `Wordpress-PostToAlbum-Script`:

```python
LFM_TAG_BLOCKLIST = (
    r"^\d{4}$", r"^aoty$", r"^best of \d{4}$",
    r"^seen live$", r"^favorites?$", r"^under \d+$",
)
```

#### `listen-count`

`Publisher._count_listen_index(release)` walks `db.get_wordpress_posts()` and counts the cached posts whose normalised title and normalised artist set match the release, then adds one. The result is written to SCF as `listen-count` and surfaced in the Discord embed (see [Publish Notification](#publish-notification)) as a `Listen count` field when the value is greater than `1`. The count is computed BEFORE `create_post` so the post being created is not double-counted.

#### Failure handling

`_build_scf_payload` swallows Last.fm HTTP errors and returns an empty `tags` list, so a Last.fm outage can never block a publish. The publish itself always succeeds, even if `_fill_post_scf` raises: the publish path logs the SCF error, marks the post in the result with `scf_pending_tags = ["scf_error"]`, and the operator still gets the standard publish notification. The only field that can plausibly be missing is `music_mood_tags`; the embed surfaces that gap so it is visible instead of silent.

### Categories

`_ensure_categories()` fetches categories and creates missing ones. The IDs are stored in `category_cache`.

Current implementation detail: it fetches categories inside the loop when a category is missing from the cache. This is simple but can repeat GET requests during a cold cache.

### Tags

`_resolve_tags(artist_names)` fetches all tags and creates missing artist tags.

`WordPressClient.create_tag()` handles WordPress "term already exists" style 400 responses by:

1. Looking for `term_id` in the error body.
2. Fetching that tag by ID, or
3. Falling back to an exact name lookup.

The in-memory tag cache is reconciled after creating or finding a tag.

### Artwork Upload

`_upload_artwork(release)`:

1. Downloads `release.cover_url` with a temporary `httpx.AsyncClient`.
2. Writes the bytes to a temporary `.jpg` file.
3. Uploads the file through `WordPressClient.upload_media`.
4. Sets alt text to `"{release.title} album art"`.
5. Deletes the temporary file.

If anything fails, it logs the error and returns `None`; publishing continues with `featured_media=0`.

### Post Cache Refresh

`Publisher.refresh_post_cache(force=False)` keeps `wordpress_post_cache` current for duplicate detection.

It uses two service-state keys:

- `wordpress_post_cache.x_wp_total`
- `wordpress_post_cache.first_page_hash`

When not forced, it asks `WordPressClient.get_posts()` to validate the first page with:

- previous `X-WP-Total`
- previous first-page SHA-256 hash

If both match, the post cache is considered current and tags are not fetched.

If changed or forced:

1. Fetch all posts.
2. Fetch tags.
3. Convert tag IDs on posts into artist tag names.
4. Normalize post titles and artist lists.
5. Save the full cache to SQLite.
6. Save validation metadata.

### WordPress Post And Tag Pagination

`WordPressClient.get_posts()`:

- Fetches page 1 first.
- Uses `X-WP-Total` and a hash of response content for cache validation.
- Parses `X-WP-TotalPages`.
- Fetches pages 2 through N.

`WordPressClient.get_tags()`:

- Fetches page 1.
- Reuses an in-memory full tag list when `X-WP-Total` and first-page hash match.
- If `X-WP-TotalPages` exists, uses it.
- If headers are absent but the first page is short, stops.
- Otherwise, keeps paging until a short page is returned.

### Updating Post Content From Discord

`format_discord_content_for_wordpress(raw_content)` converts modal text to safe paragraph HTML.

Input:

```text
 First & second
same paragraph

<script>blocked</script>
```

Output:

```html
<p>First &amp; second<br />same paragraph</p>

<p>&lt;script&gt;blocked&lt;/script&gt;</p>
```

Behavior:

- Normalizes line endings.
- Trims leading/trailing whitespace.
- Splits paragraphs on blank lines.
- Escapes HTML.
- Converts single newlines within a paragraph to `<br />`.

`Publisher.update_post_content(post_id, raw_content)` formats the content and sends:

```python
{"content": formatted_content}
```

to `WordPressClient.update_post`.

### Updating SCF After Publish

`Publisher.update_post_scf(post_id, partial_acf)` PATCHes the live WordPress post with the supplied SCF `acf` block:

```python
{"acf": partial_acf}
```

This is a one-line wrapper over `WordPressClient.update_post`. SCF accepts partial `acf` dicts; included fields replace, omitted fields are untouched. The Discord post-publish editor (see `src/editor_view.py`) routes every field edit through this helper.

`WordPressClient.get_post_acf(post_id)` is the read counterpart:

```text
GET /wp/v2/posts/{post_id}?context=edit
```

It returns the `acf` block of the live post, or `{}` if the post has no SCF fields yet. The post-publish editor calls this on every `snapshot()` to keep its embed in sync with the canonical values on WordPress.

### Trash/Undo

`Publisher.trash_post(post_id)` calls:

```python
wordpress.delete_post(post_id, force=False)
```

With `force=False`, WordPress moves the post to trash rather than permanently deleting it. On success, the publisher forces a post-cache refresh.

## Discord Control Plane

`DiscordBot` uses `discord.py`.

The bot acts as the operator interface. It sends DMs for prompts and exposes slash commands for status and manual actions. Only the configured `DISCORD_USER_ID` is authorized.

### Authorization

Every slash command checks:

```python
interaction.user.id == config.discord_user_id
```

Persistent prompt views also run `PromptView.interaction_check()`. Unauthorized users receive an ephemeral rejection message.

### Slash Commands

`/inprogress`:

- Shows active release lifecycles.
- Uses `Database.get_active_releases()`.
- Displays the most recently tracked release as a pinned featured item.
- Provides pagination and a select menu.
- Lets the operator manage a selected release.

`/current`:

- Fetches current Spotify playback live.
- Shows track, album, artists, playing/paused status, shuffle status, and whether the state counts for tracking.
- If the album is actively tracked, includes progress.
- Provides a button to post current content or publish early.

`/random`:

- Selects one random `saved_library_album` where `is_posted_listened=False`.
- Uses only cached database fields; it does not call Spotify for artwork or metadata.
- Shows title, artists, release type, saved date, Spotify ID, Spotify link, and cached cover thumbnail.
- Provides a Re-roll button that edits the original `/random` message with another random album.
- Returns a simple empty-state message when every saved-library Album/EP has already been posted/listened.

`/service`:

- Shows basic service status.
- Shows active release count.
- Shows saved-library Album/EP count.
- Shows posted/listened count and percentage.
- Shows database connected status.
- Attempts to show `last_poll` if present in `service_state`.
- Shows last saved-library sync time when present.
- Lists available commands.

Current note: `last_poll` is displayed but the tracker does not currently write it.

### Persistent Views And Buttons

75 percent prompt: `SeventyFivePromptView`

- `prompt_75_publish_now`: publish early.
- `prompt_75_wait`: continue tracking until full completion.

Relisten approval prompt: `RelistenApprovalPromptView`

- `prompt_relisten_approve_tracking`: approve duplicate tracking as relisten.

Published-post actions: `PublishedPostActionView`

- `prompt_edit_metadata`: opens the SCF editor (post-publish mode) in the user's DMs.
- `prompt_undo_post`: moves the WordPress post to trash, clears saved-library posted/listened state if present, and deletes the release from the tracking database.
- `prompt_keep_post`: marks the prompt declined and leaves the post alone.

In-progress page: `InProgressView`

- `inprogress_select`: select a release to manage.
- `inprogress_previous_page`: previous page.
- `inprogress_refresh_page`: refresh current page.
- `inprogress_next_page`: next page.

Release action view: `ReleaseActionView`

- `release_publish_early`: publish selected release now.
- `release_edit_metadata`: open the SCF editor (pre-publish mode) for the selected release.
- `release_remove_database`: ask for removal confirmation.
- `release_show_missing_songs`: list unlistened countable tracks.
- `release_back_to_inprogress`: return to the in-progress list.

Confirm remove view: `ConfirmRemoveView`

- `confirm_remove_release`: delete release state from SQLite.
- `cancel_remove_release`: cancel.

Current playback action view: `CurrentPlaybackActionView`

- `current_post_content`: preview publishing current playback.

Confirm current post view: `ConfirmCurrentPostView`

- `confirm_current_post`: publish current playback.
- `cancel_current_post`: cancel.

Post content modal: `PostContentModal`

- `post_content_body`: required paragraph text, max length 4000.

SCF editor view: `EditorView`

- `editor:bool:favorite`: toggle the SCF `music_favorite` flag inline.
- `editor:bool:unreleased`: toggle the SCF `unreleased` flag inline.
- `editor:modal:rating`: open a single-field modal for the SCF `music_rating` (integer 0-100).
- `editor:modal:notes`: open a single-field paragraph modal for the SCF `music_notes` (max 4000).
- `editor:modal:body`: open a single-field paragraph modal to replace the WP post `content` (post-publish only).
- `editor:open:tracks`: open the paginated track-highlight sub-view.
- `editor:nav:resync` (post-publish only): re-read the live SCF from WP into the editor.
- `editor:nav:refresh`: rebuild the embed from the current in-memory state.
- `editor:nav:done`: delete the editor message.

SCF editor track highlight view: `EditorTracksView`

- `editor:track:<spotify_id>:<page>`: toggle one track's `highlight` flag inline (in-memory pre-publish; PATCH `music_tracks` post-publish).
- `editor:nav:back_to_editor:<page>`: swap back to the parent editor.
- `editor:nav:tracks_prev:<page>`: previous page of track buttons.
- `editor:nav:tracks_next:<page>`: next page of track buttons.

Every button above uses a `custom_id` prefixed with `editor:` so persistent views are re-registered on bot ready. Track highlight buttons have a `spotify_id` in their custom_id and are not re-routable across bot restarts; the user can recover by re-opening the editor (see `handle_prompt_action`).

### Discord Prompt Handling

All button actions flow through `handle_prompt_action(interaction, action)` for prompt-backed views.

General behavior:

1. Defer ephemeral response for most actions.
2. Load the prompt by `interaction.message.id`.
3. Reject missing, already handled, or expired prompts.
4. Load the release if required.
5. Route by prompt type and action.
6. Send a success/error response.

`edit_metadata` (post-publish) and `add_content` are special: they do not defer first because the editor / modal needs the interaction token to respond.

### Publish Notification

After successful WordPress publishing, `send_publish_notification(release, result)` DMs a `discord.Embed` with all the existing fields plus what the SCF block exposed:

- title
- artist list
- post ID
- public WordPress link
- release type
- progress
- artwork thumbnail
- original post ID if relisten
- `Listen count` (only when `result.listen_count > 1`)
- `⚠️ SCF metadata` field (only when the SCF block wrote everything except mood tags)

The plain-text line above the embed reflects what was filled:

- `The release has been published to WordPress and SCF metadata was auto-filled.` when mood tags landed.
- `The release has been published to WordPress, but SCF mood tags could not be filled (Last.fm returned no tags).` when Last.fm had no usable tags for this release.
- `The release has been published to WordPress.` when `SPOTIFY_BLOG_TRACKER_FILL_SCF` is off.

It attaches the published-post actions view and saves a `PromptType.PROMPT_UNDO` prompt.

### Discord Presence

`update_presence(state)` changes the bot presence:

- If playback exists, status is online or idle and activity says it is listening to the current track by artist.
- If state is `None`, status is do-not-disturb and activity is cleared.

Presence updates are best-effort; exceptions are swallowed.

### Public WordPress Links

`_get_public_wordpress_link(raw_link)` rewrites the scheme and host of a WordPress link using `WORDPRESS_PUBLIC_URL`, while preserving the path, params, query, and fragment. This allows internal WordPress API URLs to become public-facing links in Discord.

### SCF Editor

`src/editor_view.py` exposes a persistent Discord embed for editing the SCF human-curated fields both before and after publish.

> **discord.py callback contract.** Every dynamic button callback has signature `(self, interaction)`. discord.py 2.x invokes `await item.callback(interaction)`; the static `@discord.ui.button` decorator adds the second positional argument automatically, but callbacks assigned via `button.callback = ...` must not declare `button` as a parameter. The editor's bool-toggles refresh label and style by mutating `self.children` in `_apply_field_edit` before `edit_message`, so the next render reflects the new state without holding per-button references.

Strategy-shaped sinks:

- `PrePublishSink(db, release)`: writes edits to the in-memory `Release` and calls `db.save_release(release)`. The next publish emits the new values via `_build_scf_payload`.
- `PostPublishSink(publisher, wordpress_client, post_id, initial_acf=None)`: re-reads the live `acf` block on every `snapshot()` and PATCHes updates through `Publisher.update_post_scf`.

Factory helpers:

- `open_pre_publish_editor(db, release, on_open)`: defer-reads the current SCF values off the release, constructs the `EditorView`, and lets the caller deliver it (typically via `_send_dm`).
- `open_post_publish_editor(publisher, wordpress_client, post_id, release_title, initial_acf, on_open)`: pre-fetches `GET /wp/v2/posts/{id}?context=edit`, constructs the `EditorView`, and lets the caller deliver it.

Field-name bridge (used by `PostPublishSink.update_field`):

- `rating` → `music_rating` (number, default 0).
- `favorite` → `music_favorite` (true_false).
- `notes` → `music_notes` (text).
- `unreleased` → `unreleased` (true_false).
- per-track `highlight` → single-row patch into `music_tracks` repeater.

The publish pipeline (`Publisher._build_scf_payload`) re-reads `release.rating / favorite / notes / unreleased / track.highlight` so pre-publish edits ride along with the SCF auto-fill on publish, without any merge step.

## Helpers

### `src/utils.py`

`normalize_text(text)`:

- Unicode NFKC normalization.
- Casefolding.
- Trims outer whitespace.
- Collapses repeated internal whitespace.
- Removes zero-width characters.

`normalize_artist_name(name)`:

- Removes commas.
- Calls `normalize_text`.

`normalize_artist_list(artists)`:

- Normalizes each artist name.

`compute_release_type(tracks, raw_spotify_type)`:

- `compilation` maps to `Compilation`.
- 7 or more tracks maps to `Album`.
- 30 minutes or more total duration maps to `Album`.
- 4 to 6 tracks under 30 minutes maps to `EP`.
- 1 to 3 tracks with any track at least 10 minutes maps to `EP`.
- 1 to 3 tracks under 30 minutes and no track at least 10 minutes maps to `Single`.
- Fallback is `Album`.

### `src/inprogress.py`

`INPROGRESS_PAGE_SIZE = 9`.

`build_inprogress_page(releases, page, page_size=9)`:

- Sorts releases by `last_seen` descending.
- Pins the most recent release as `featured`.
- Paginates the remaining releases.
- Clamps page indexes.
- Returns `None` if there are no releases.

`get_next_unlistened_track(release)`:

- Returns the first track in stored album order where `is_countable=True` and `listened=False`.
- Returns `None` when all countable tracks are listened.

### `src/logging_config.py`

`configure_logging(project_root, level=None)`:

- Creates `logs/`.
- Writes to `logs/album-tracker.log`.
- Adds a daily `TimedRotatingFileHandler`.
- Keeps 14 backups.
- Adds a console handler.
- Removes old album-tracker handlers before adding new ones, so repeated calls are idempotent.

`_resolve_log_level(level)`:

- Accepts an integer logging level.
- Accepts a string logging level.
- Falls back to `LOG_LEVEL` env var.
- Defaults to `INFO`.

### `scripts/migrate.py`

Standalone migration runner:

```bash
PYTHONPATH=src python3 scripts/migrate.py
```

It configures logging, creates `Config` and `Database`, calls `db.initialize()`, logs success, and closes the database.

## Function And Method Catalog

This catalog documents every class, function, and method in `main.py`, `scripts/migrate.py`, and `src/`.

### `main.py`

`Service.__init__(self)`:

- Inputs: none.
- Output: initialized service object.
- Creates config, database, publisher, tracker, saved-library service, and Discord bot.

`Service.start(self)`:

- Inputs: none.
- Output: never returns during normal operation.
- Authorizes Spotify, initializes DB, refreshes WordPress cache, synchronizes saved library, starts Discord, waits for readiness, starts tracker, and starts the 24-hour saved-library sync loop.

`Service._refresh_saved_library(self)`:

- Refreshes WordPress post cache, then synchronizes saved Spotify library rows.
- Logs saved-library sync errors without stopping the service.

`Service._run_saved_library_sync_loop(self)`:

- Sleeps for 24 hours between saved-library refreshes.

`Service.stop(self)`:

- Inputs: none.
- Output: none.
- Cancels saved-library sync, stops tracker and Discord, closes publisher and database.

`main()`:

- Inputs: none.
- Output: process exit behavior.
- Creates and starts the service, handles top-level errors.

`signal_handler(signum, frame)`:

- Inputs: signal number and frame.
- Output: process exit.
- Schedules shutdown and exits.

### `src/config.py`

`Config.__init__(self)`:

- Loads paths, environment variables, tokens, and validates required config.

`Config._load_env(self)`:

- Loads `.env` with `python-dotenv` if the file exists.

`Config._load_persisted_tokens(self)`:

- Reads Spotify tokens from `data/.spotify_tokens` if available.

`Config.save_tokens(self, access_token, refresh_token)`:

- Writes Spotify access and refresh tokens to `data/.spotify_tokens`.

`Config._validate(self)`:

- Raises `ValueError` if required values are missing.

`Config.database_url(self)`:

- Property returning a SQLite URL string.

### `src/database.py`

`Database.__init__(self, config)`:

- Stores config, database path, and initializes the connection field.

`initialize(self)`:

- Opens the SQLite connection, enables WAL and foreign keys, runs migrations.

`close(self)`:

- Closes the SQLite connection when present.

`_run_migrations(self)`:

- Reads sorted SQL files in `migrations/` and runs versions newer than current schema version.

`_get_schema_version(self)`:

- Returns latest schema version, or `0` if the table does not exist.

`_set_schema_version(self, version)`:

- Inserts or replaces the schema version row.

`get_release(self, spotify_id)`:

- Input: Spotify album/release ID.
- Output: `Release` or `None`.
- Loads lifecycle row, artists, and tracks.

`save_release(self, release)`:

- Input: `Release`.
- Output: SQLite row ID from the lifecycle insert/replace.
- Saves lifecycle, artists, and tracks.

`delete_release(self, spotify_id)`:

- Input: Spotify release ID.
- Output: `True` if a row was deleted, otherwise `False`.
- Deletes Discord prompts and release row.

`delete_published_releases_older_than(self, cutoff)`:

- Input: cutoff `datetime`.
- Output: number of deleted releases.
- Deletes retained `PUBLISHED_RECENTLY` releases with old `published_at`.

`touch_release_last_seen(self, spotify_id, seen_at)`:

- Updates only `last_seen` for a release.

`get_active_releases(self)`:

- Output: list of releases with status `active`, `awaiting_75_decision`, or `publishing`, newest first.

`_get_release_artists(self, release_id)`:

- Loads `Artist` rows for a numeric release row ID.

`_get_release_tracks(self, release_id)`:

- Loads `Track` rows ordered by disc and track number.

`_save_release_artists(self, release_id, artists)`:

- Replaces all artist rows for a release.

`_save_release_tracks(self, release_id, tracks)`:

- Replaces all track rows for a release.

`_row_to_release(self, row, artists, tracks)`:

- Converts a SQLite row plus child objects into `Release`.

`get_wordpress_posts(self)`:

- Output: cached `WordPressPost` list.

`save_wordpress_posts(self, posts)`:

- Replaces the full WordPress post cache.

`get_saved_library_album(self, spotify_id)`:

- Returns one saved-library album row or `None`.

`get_saved_library_album_ids(self)`:

- Returns all saved-library Spotify IDs.

`get_saved_library_albums_by_id(self)`:

- Returns saved-library rows keyed by Spotify ID.

`get_saved_library_snapshot_items(self)`:

- Returns the complete saved-library identity snapshot ordered by Spotify saved-library position.

`replace_saved_library_snapshot(self, items)`:

- Replaces the full saved-library identity snapshot in one database transaction.

`upsert_saved_library_album(self, album)`:

- Inserts or updates one saved-library row.

`delete_saved_library_albums(self, spotify_ids)`:

- Deletes saved-library rows by Spotify ID and returns the deleted count.

`mark_saved_library_album_posted(self, spotify_id, wordpress_post_id)`:

- Marks an existing saved-library row posted/listened.

`mark_saved_library_album_unposted(self, spotify_id)`:

- Clears posted/listened state for an existing saved-library row.

`get_random_unposted_saved_library_album(self)`:

- Returns one random unposted saved-library row or `None`.

`get_saved_library_stats(self)`:

- Returns saved-library total, posted/listened count, and percentage.

`_row_to_saved_library_album(self, row)`:

- Converts a SQLite row into `SavedLibraryAlbum`.

`save_discord_prompt(self, prompt)`:

- Inserts a Discord prompt row.

`get_discord_prompt(self, message_id)`:

- Looks up a prompt by Discord message ID.

`has_discord_prompt(self, release_id, prompt_type)`:

- Returns whether any prompt of that type exists for the release.

`expire_stale_discord_prompts(self, release_id, prompt_type, now=None)`:

- Marks pending prompts expired when `expires_at` has passed.

`get_live_discord_prompt(self, release_id, prompt_type, now=None)`:

- Returns newest pending unexpired prompt for a release/type after expiring stale prompts.

`get_discord_prompt_by_release_and_type(self, release_id, prompt_type)`:

- Returns newest prompt for a release/type regardless of state.

`update_discord_prompt_state(self, message_id, state)`:

- Updates prompt state by Discord message ID.

`log_audit_event(self, event_type, data)`:

- Inserts an audit event with JSON payload and current timestamp.

`save_service_state(self, key, value)`:

- Upserts a key-value service state row.

`get_service_state(self, key)`:

- Returns a service-state value or `None`.

### `src/spotify_client.py`

`SpotifyClient.__init__(self, config)`:

- Creates an authenticated `httpx.AsyncClient` if an access token exists.

`ensure_authorized(self)`:

- Ensures access and refresh tokens exist.

`_authorize(self)`:

- Performs the current browser-plus-pasted-code PKCE authorization flow.

`_wait_for_callback(self)`:

- Prompts the user to paste the authorization code.

`_exchange_code_for_tokens(self, code, code_verifier)`:

- Exchanges authorization code for access and refresh tokens.

`close(self)`:

- Closes the Spotify HTTP client.

`_ensure_token(self)`:

- Refreshes the access token if expired or missing.

`get_playback_state(self)`:

- Calls `GET /me/player`; returns JSON or `None`.

`get_album(self, album_id)`:

- Calls `GET /albums/{album_id}`.

`get_album_tracks(self, album_id)`:

- Calls `GET /albums/{album_id}/tracks?limit=50` and paginates.

`get_recently_played(self, limit=50)`:

- Calls `GET /me/player/recently-played?limit={limit}`.

`get_saved_albums_page(self, limit=50, offset=0, url=None)`:

- Calls one saved-albums page, capped at Spotify's limit of 50.

`get_all_saved_albums(self, first_page=None)`:

- Follows saved-album paging links until no `next` URL remains.

`check_library_contains_album(self, album_id_or_uri)`:

- Checks whether one album URI is saved in the user's Spotify library.

### `src/saved_library.py`

`SavedLibraryService.__init__(self, db, spotify)`:

- Stores database and Spotify collaborators.

`sync(self, force=False)`:

- Fetches the first saved-albums page, validates total and first-page hash, and either skips, performs an incremental snapshot reconciliation, or falls back to a full saved-library scan.

`compute_first_page_hash(self, page)`:

- Hashes stable first-page album IDs plus `added_at` values.

`_run_full_reconcile(self, first_page, current_total, first_page_hash, reason)`:

- Follows all saved-album pages, rebuilds `saved_library_snapshot_item`, reconciles `saved_library_album`, and refreshes full-audit service state.

`_run_incremental_reconcile(self, first_page, current_total, first_page_hash, snapshot_items, existing_by_id)`:

- Handles common changed-library cases without full pagination: head additions, sparse removals, and simple mixed head additions/removals.

`_apply_incremental_changes(self, current_total, first_page_hash, addition_items, removed_ids, new_order, old_by_id, existing_by_id, reason)`:

- Persists an incremental reconciliation by deleting removed listen-to rows, upserting newly included Albums/EPs, replacing the snapshot, and saving validation state.

`_fetch_head_until_known_album(self, first_page, old_by_id, current_total, page_cache)`:

- Reads head pages until a previously known Spotify album ID appears, capped by `SAVED_LIBRARY_MAX_INCREMENTAL_HEAD_PAGES`.

`_remove_missing_ids_with_probes(self, candidate_order, current_total, removals_needed, page_cache)`:

- Finds removed IDs through repeated page probes instead of walking every saved-library page.

`_find_first_missing_id(self, local_order, current_total, page_cache)`:

- Binary-searches Spotify pages to find the first local snapshot ID missing from the current saved-library order.

`_rebuild_snapshot_items(self, new_order, old_by_id, addition_items)`:

- Combines new saved-album response items with preserved existing snapshot metadata and assigns fresh positions.

`_build_snapshot_item(self, item, position)`:

- Converts one Spotify saved-album item into a lightweight `SavedLibrarySnapshotItem`.

`_parse_state_int(self, value)`:

- Parses integer service-state values for validation checks.

`_build_saved_album(self, item, existing_by_id, wordpress_posts)`:

- Converts one Spotify saved-album item into `SavedLibraryAlbum`, or returns `None` when it is not an included Album/EP.

`_get_release_type(self, item, existing_by_id)`:

- Reuses an existing release type when possible, otherwise classifies from `album.total_tracks` and embedded `album.tracks.items`.
- Falls back to fetching full album tracks only when embedded track data is incomplete.

`_parse_total_tracks(self, album)`:

- Parses Spotify's `total_tracks` field into an integer when possible.

`_get_complete_embedded_tracks(self, album, total_tracks)`:

- Returns embedded track items when they are complete enough for release-type classification.

`_find_matching_wordpress_post(self, normalized_title, normalized_artists, wordpress_posts)`:

- Finds a WordPress post with matching normalized title and exact normalized artist set.

`_get_cover_url(self, album)`:

- Returns the first Spotify image URL when present.

`_parse_spotify_datetime(self, value)`:

- Parses Spotify ISO timestamps, including trailing `Z`.

### `src/tracker.py`

`Tracker.__init__(self, config, db, publisher=None, discord_bot=None)`:

- Stores collaborators, creates `SpotifyClient`, initializes intervals and cleanup state.

`set_discord_bot(self, discord_bot)`:

- Sets the Discord bot after both objects have been constructed.

`run(self)`:

- Main infinite tracker loop.

`stop(self)`:

- Stops the loop and closes Spotify HTTP client.

`_poll_once(self)`:

- Runs one full playback polling iteration.

`_parse_playback_state(self, data)`:

- Converts raw Spotify playback JSON to `PlaybackState`.

`_qualifies_for_tracking(self, state)`:

- Returns whether playback should count for automatic album tracking.

`_get_or_create_release(self, album_id)`:

- Used by manual actions. Loads or creates a tracked release.

`_create_tracked_release(self, release, is_relisten=False, duplicate_post_id=None)`:

- Saves a release once tracking is allowed.

`_start_tracking_or_prompt_for_relisten(self, release)`:

- Starts tracking immediately unless duplicate approval is required.

`_build_release_from_spotify(self, album_id)`:

- Builds an unsaved `Release` from Spotify album and track data.

`_match_track_to_release(self, release, item)`:

- Matches current Spotify track ID to a release track.

`_mark_track_listened(self, track, source)`:

- Mutates track listened fields.

`_recompute_release_progress(self, release)`:

- Recomputes track-count progress and saves the release.

`_has_75_prompt(self, release)`:

- Returns whether a 75 percent prompt already exists.

`_send_75_prompt(self, release)`:

- Saves awaiting state, sends Discord prompt, and records prompt state.

`_handle_completion(self, release)`:

- Publishes completed release unless already recently published.

`_initialize_duplicate_state(self, release)`:

- Legacy helper for manual duplicate previews.

`_check_duplicate(self, release)`:

- Compares release title/artist set against cached WordPress posts.

`_get_cached_wordpress_post(self, post_id)`:

- Finds one cached WordPress post by ID.

`_send_relisten_approval_prompt(self, release, duplicate_post)`:

- Sends relisten approval DM and saves prompt.

`approve_relisten_tracking(self, prompt)`:

- Handles relisten approval and returns an outcome string.

`publish_release_now(self, release, as_relisten=False)`:

- Publishes manually with idempotency outcomes:
  - `already_published`
  - `already_publishing`
  - `published`

`_publish_release(self, release, as_relisten=False)`:

- Performs publish workflow, release status updates, saved-library posted/listened marking, saved-library membership warning, and audit logging.

`_warn_if_published_album_not_saved(self, release)`:

- Checks Spotify saved-library membership after publish and sends a Discord warning when the album is not saved.

`_cleanup_published_releases_if_due(self, now=None)`:

- Runs retention cleanup only when the cleanup interval has elapsed.

`_cleanup_published_releases(self, now=None)`:

- Deletes old recently published releases and audits the cleanup.

`_delete_published_release_if_expired(self, release, now=None)`:

- Deletes a single expired recently published release before reusing its album ID.

`_handle_idle(self)`:

- Handles no active Spotify playback and sleeps idle interval.

`_handle_non_qualifying(self, state)`:

- Handles playback that exists but should not count, then sleeps paused interval.

`_update_current_listening(self, state)`:

- Saves current playback state string and updates Discord presence.

### `src/publisher.py`

`format_discord_content_for_wordpress(raw_content)`:

- Converts Discord modal body text to escaped WordPress paragraph HTML.

`_coerce_spotify_release_date(value)`:

- Expands Spotify `YYYY` or `YYYY-MM` partial dates to `YYYY-01-01` / `YYYY-MM-01` (SCF `date_picker` rejects partial dates). Full ISO dates pass through.

`_format_scf_date(value)`:

- Renders an ISO date/datetime (or empty) as `d/m/Y` for SCF `date_picker` fields.

`Publisher.__init__(self, config, db)`:

- Creates `WordPressClient`, `LastFMClient`, category cache, tag cache, and the `_fill_scf_enabled` flag (driven by `Config.fill_scf_enabled`).

`close(self)`:

- Closes WordPress and Last.fm clients.

`publish_release(self, release, as_relisten=False)`:

- Creates WordPress post with categories, artist tags, featured artwork, and (when `fill_scf_enabled`) the SCF `acf` block. Counts matching existing posts BEFORE `create_post` so `listen-count` does not double-count. Returns a `PublishResult`.

`trash_post(self, post_id)`:

- Moves a WordPress post to trash and refreshes cache.

`update_post_content(self, post_id, raw_content)`:

- Formats modal content and updates a WordPress post body.

`_ensure_categories(self)`:

- Finds or creates required WordPress categories.

`_resolve_tags(self, artist_names)`:

- Finds or creates tags for release artists.

`_upload_artwork(self, release)`:

- Downloads Spotify artwork and uploads it to WordPress media.

`refresh_post_cache(self, force=False)`:

- Refreshes cached WordPress posts used for duplicate detection.

`_save_post_cache_validation_state(self, posts_result)`:

- Saves `X-WP-Total` and first-page hash to service state.

`_count_listen_index(self, release)`:

- Counts cached WordPress posts whose normalised title and normalised artist set match the release, returns `matches + 1`. Used to fill SCF `listen-count` and surface in the Discord embed.

`_build_scf_payload(self, release, listen_count, post)`:

- Builds the SCF `acf` payload from in-memory `Release` data plus one Last.fm call, and reports which fields could not be filled (`fetch_status["mood_tags"]` is `None` when Last.fm returned no usable tags).

`_fill_post_scf(self, post_id, acf_payload)`:

- `POST /wp/v2/posts/{post_id}` with `{"acf": acf_payload}` so SCF picks up the new fields while leaving un-supplied meta intact.

### `src/wordpress_client.py`

`WordPressPostsResult`:

- Dataclass returned by `get_posts`.
- Fields: `posts`, `cache_unchanged`, `message`, `x_wp_total`, `first_page_hash`.

`WordPressClient.__init__(self, config)`:

- Builds API URL, Basic Auth header, HTTP client, and tag cache fields.

`close(self)`:

- Closes HTTP client.

`get_posts(self, validate_first_page=False, previous_x_wp_total=None, previous_first_page_hash=None, **params)`:

- Fetches paginated WordPress posts and optionally short-circuits when cache metadata matches.

`_first_page_cache_matches(...)`:

- Returns whether first-page metadata proves the post cache is current.

`create_post(self, data)`:

- Creates a WordPress post.

`update_post(self, post_id, data)`:

- Updates a WordPress post.

`delete_post(self, post_id, force=False)`:

- Deletes or trashes a WordPress post.

`get_categories(self)`:

- Fetches up to 100 categories.

`create_category(self, name)`:

- Creates a category.

`get_tags(self)`:

- Fetches all tags with pagination and in-memory cache validation.

`_parse_total_pages(self, response)`:

- Parses `X-WP-TotalPages`.

`_tag_cache_matches(self, x_wp_total, first_page_hash)`:

- Returns whether the cached full tag list can be reused.

`_cache_tags(self, tags, x_wp_total, first_page_hash)`:

- Stores the full tag list and validation metadata in memory.

`_reconcile_cached_tag(self, tag)`:

- Updates or appends one tag in the in-memory cache and invalidates metadata.

`get_tag_by_id(self, tag_id)`:

- Fetches one tag by ID.

`get_tag_by_name(self, name)`:

- Finds an exact tag name by scanning all tags.

`create_tag(self, name)`:

- Creates a tag and handles "already exists" error cases.

`upload_media(self, file_path, alt_text="")`:

- Uploads a JPEG media file.

`update_media(self, media_id, data)`:

- Updates media metadata.

`delete_media(self, media_id, force=False)`:

- Deletes or trashes media.

### `src/discord_bot.py`

`CurrentPostContext`:

- Dataclass describing current playback publishing context.

`CurrentPostContext.will_publish_as_relisten`:

- Returns whether the current post should be treated as a relisten.

`_clip_discord_text(value, limit)`:

- Truncates text for Discord labels/fields.

`PromptView.__init__(self, discord_bot)`:

- Stores Discord bot and creates persistent view.

`PromptView.interaction_check(self, interaction)`:

- Enforces authorized Discord user.

`SeventyFivePromptView.publish_now(...)`:

- Routes to `handle_prompt_action(..., "publish_now")`.

`SeventyFivePromptView.wait_for_completion(...)`:

- Routes to `handle_prompt_action(..., "wait")`.

`RelistenApprovalPromptView.approve_tracking(...)`:

- Routes to `handle_prompt_action(..., "approve_relisten_tracking")`.

`PostContentModal.__init__(...)`:

- Builds modal for post body input.

`PostContentModal.on_submit(...)`:

- Submits content to `_handle_post_content_submit`.

`PublishedPostActionView.add_content(...)`:

- Routes to add-content handling.

`PublishedPostActionView.undo_post(...)`:

- Routes to undo handling.

`PublishedPostActionView.keep_post(...)`:

- Routes to keep-post handling.

`InProgressView.__init__(self, discord_bot, page_data)`:

- Builds select menu and pagination buttons.

`InProgressView._build_option(self, release, featured=False)`:

- Builds a Discord select option for a release.

`InProgressView.previous_page(...)`:

- Shows previous in-progress page.

`InProgressView.refresh_page(...)`:

- Refreshes current page.

`InProgressView.next_page(...)`:

- Shows next in-progress page.

`ReleaseActionView.__init__(self, discord_bot, release_id, return_page=0)`:

- Stores selected release and return page.

`ReleaseActionView.publish_early(...)`:

- Publishes selected release.

`ReleaseActionView.remove_from_database(...)`:

- Opens removal confirmation.

`ReleaseActionView.show_missing_songs(...)`:

- Shows missing tracks.

`ReleaseActionView.back_to_inprogress(...)`:

- Returns to in-progress list.

`ConfirmRemoveView.__init__(self, discord_bot, release_id)`:

- Stores release to remove.

`ConfirmRemoveView.confirm_remove(...)`:

- Deletes selected release.

`ConfirmRemoveView.cancel_remove(...)`:

- Cancels deletion.

`CurrentPlaybackActionView.__init__(self, discord_bot, playback_state, post_label="Post current content")`:

- Builds current playback action and optionally changes button label.

`CurrentPlaybackActionView.post_content(...)`:

- Opens current playback publish preview.

`ConfirmCurrentPostView.__init__(self, discord_bot, playback_state)`:

- Stores current playback state.

`ConfirmCurrentPostView.confirm_post(...)`:

- Publishes current playback.

`ConfirmCurrentPostView.cancel_post(...)`:

- Cancels current publish.

`DiscordBot.__init__(self, config, db, tracker)`:

- Creates Discord client, command tree, persistent views, and slash commands.

`_setup_commands(self)`:

- Registers `/inprogress`, `/current`, `/random`, `/service`, and `on_ready`.

`_register_views(self)`:

- Registers persistent prompt views.

`start(self)`:

- Starts Discord bot.

`wait_until_ready(self)`:

- Waits for `on_ready`.

`stop(self)`:

- Closes Discord bot.

`_check_authorized(self, user_id)`:

- Checks Discord user ID.

`_get_user(self)`:

- Gets or fetches the configured Discord user.

`_send_dm(self, content, embed=None, view=None)`:

- Sends a DM to the configured user.

`_get_public_wordpress_link(self, raw_link)`:

- Rewrites WordPress links to public base URL.

`update_presence(self, state)`:

- Updates bot presence from playback state.

`send_75_percent_prompt(self, release)`:

- Sends the 75 percent prompt DM.

`send_relisten_tracking_prompt(self, release, duplicate_post, expires_at)`:

- Sends relisten approval prompt DM.

`send_publish_notification(self, release, result)`:

- Sends publish notification and saves undo prompt. `result` is a `PublishResult`; the embed adds a `Listen count` field when `result.listen_count > 1` and a `⚠️ SCF metadata` field when `result.scf_pending_tags` is non-empty. The plain-text line above the embed reflects whether SCF mood tags landed.

`send_library_missing_notification(self, release)`:

- Sends a warning DM when a published album is not saved in Spotify.

`handle_prompt_action(self, interaction, action)`:

- Central dispatcher for prompt-backed button actions.

`_send_prompt_action_response(self, interaction, content)`:

- Sends response through followup or initial response.

`_unknown_prompt_action(self, interaction)`:

- Sends unknown-action message.

`_resolve_wordpress_post_id(self, release, prompt, fallback_wordpress_post_id=None)`:

- Chooses post ID from prompt, release, or fallback.

`_handle_75_publish(self, interaction, release, prompt)`:

- Marks prompt accepted and publishes early.

`_handle_75_wait(self, interaction, release, prompt)`:

- Marks prompt declined and resumes active tracking.

`_handle_relisten_tracking_approval(self, interaction, prompt)`:

- Calls tracker approval flow and reports outcome.

`_handle_add_content(self, interaction, release, prompt)`:

- Opens post-content modal.

`_handle_post_content_submit(...)`:

- Updates WordPress post content from modal text.

`_handle_undo_post(self, interaction, release, prompt)`:

- Trashes WordPress post, clears saved-library posted/listened state, and deletes release state.

`_handle_keep_post(self, interaction, release, prompt)`:

- Marks undo prompt declined and keeps post.

`_handle_inprogress_selection(self, interaction, release_id, return_page=0)`:

- Shows selected release management embed.

`_handle_publish_release(self, interaction, release_id)`:

- Publishes a selected release from `/inprogress`.

`_handle_remove_release_prompt(self, interaction, release_id)`:

- Shows removal confirmation.

`_handle_missing_songs(self, interaction, release_id)`:

- Shows up to 10 missing countable tracks.

`_handle_confirm_remove_release(self, interaction, release_id)`:

- Deletes release after confirmation.

`_build_release_summary_embed(self, release, title="Release Manager")`:

- Builds manage-release embed.

`_handle_current_post_request(self, interaction, playback_state)`:

- Builds preview for publishing current playback.

`_build_current_preview_embed(self, state, context)`:

- Builds current publish confirmation embed.

`_build_current_embed(self, state, context=None)`:

- Builds `/current` status embed.

`_resolve_current_post_context(self, state, check_duplicate=False)`:

- Determines tracked release, candidate release, duplicate post, and relisten state.

`_handle_current_post_confirm(self, interaction, playback_state)`:

- Publishes current playback after confirmation.

`_publish_release_with_feedback(...)`:

- Wraps tracker publish outcomes into Discord messages.

`_handle_random(self, interaction)`:

- Handles `/random` by selecting one unposted saved-library album and attaching a Re-roll button.

`_handle_random_reroll(self, interaction)`:

- Handles the Re-roll button by editing the original `/random` message with another random album or the empty state.

`_build_random_album_embed(self, album)`:

- Builds the `/random` embed from cached saved-library fields.

`_build_inprogress_embed(self, page_data)`:

- Builds `/inprogress` embed.

`_format_inprogress_release(self, release, include_last_seen)`:

- Formats one release entry for `/inprogress`.

`_get_release_progress_parts(self, release)`:

- Returns listened count, countable count, and integer progress percent.

`_handle_inprogress_page(self, interaction, page)`:

- Refreshes or pages the in-progress message.

`_handle_inprogress(self, interaction)`:

- Handles `/inprogress`.

`_handle_current(self, interaction)`:

- Handles `/current`.

`_handle_service(self, interaction)`:

- Handles `/service`, including saved-library stats.

### `src/models.py`

Classes:

- `ReleaseType`
- `LifecycleStatus`
- `PromptType`
- `PromptState`
- `Artist`
- `Track` (carries an `explicit: bool` field captured at tracking time so the SCF `music_tracks` repeater and `music_explicit` field can be filled without an extra Spotify call)
- `Release`
- `PlaybackState`
- `WordPressPost`
- `SavedLibraryAlbum`
- `SavedLibrarySnapshotItem`
- `SavedLibraryStats`
- `SavedLibrarySyncResult`
- `DiscordPrompt`
- `PublishResult` (returned by `Publisher.publish_release`; fields: `post: dict`, `scf_pending_tags: list[str]`, `listen_count: int`)

These are covered in [Data Models And State](#data-models-and-state).

### `src/lastfm_client.py`

- `LFM_TAG_BLOCKLIST` (tuple of regex patterns used by `pick_mood_tags`)
- `LastFMClient(api_key)`
- `LastFMClient.album_getinfo(artist, album)`
- `LastFMClient.close()`
- `pick_mood_tags(album_info, max_n=3)`

These are covered in [Last.fm Integration](#lastfm-integration) and [SCF Auto-Fill](#scf-auto-fill).

### `src/utils.py`

Functions:

- `normalize_text(text)`
- `normalize_artist_name(name)`
- `normalize_artist_list(artists)`
- `compute_release_type(tracks, raw_spotify_type)`

These are covered in [Helpers](#helpers).

### `src/inprogress.py`

Classes/functions:

- `InProgressPage`
- `build_inprogress_page(releases, page, page_size=INPROGRESS_PAGE_SIZE)`
- `get_next_unlistened_track(release)`

These are covered in [Helpers](#helpers).

### `src/logging_config.py`

Functions:

- `_resolve_log_level(level)`
- `configure_logging(project_root, level=None)`

These are covered in [Helpers](#helpers).

### `scripts/migrate.py`

`main()`:

- Creates config/database, runs migrations through `Database.initialize()`, logs success, and closes database.

## Testing

Run tests:

```bash
PYTHONPATH=src python3 -m unittest -v
```

Observed in this environment on 2026-07-07:

```text
Ran 114 tests
OK
```

Install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python3 -m unittest -v
```

### Test Groups

`TestLoggingConfiguration`:

- Verifies log file creation.
- Verifies idempotent logging setup does not duplicate handlers.

`TestNormalization`:

- Verifies casefolding, whitespace collapse, trimming, comma stripping, and artist-list normalization.

`TestReleaseClassification`:

- Verifies compilation, album, EP, and single classification rules.

`TestProgressTracking`:

- Verifies 0, 50, and 100 percent progress.
- Verifies non-countable tracks are ignored.

`TestInProgressPagination`:

- Verifies the most recent release is featured.
- Verifies page size and page clamping.

`TestNextTrackSelection`:

- Verifies first unlistened countable track selection.
- Verifies non-countable/listened tracks are skipped.

`TestSavedLibraryService`:

- Verifies matching total and first-page hash skips full sync.
- Verifies full sync filters to Album/EP entries, matches WordPress posts, saves state, and deletes removed albums.
- Verifies incremental head additions and sparse removals avoid full saved-library pagination.
- Verifies saved-library release typing uses embedded `/me/albums` track data and only falls back to full track fetches for incomplete low-track-count payloads.

`TestDiscordBotEmbeds`:

- Verifies Discord embed formatting and view labels.
- Verifies `/random` embed uses cached Spotify link and cover URL.
- Verifies add-content modal flow.
- Verifies post content update handling.
- Verifies undo behavior.
- Verifies current playback publish/relisten context behavior.
- Verifies in-progress publish uses stored relisten state.

`TestTrackerLastSeen`:

- Verifies replaying an already listened track still refreshes `last_seen`.

`TestTrackerPublishNow`:

- Verifies publish-now idempotency.
- Verifies published retention state.
- Verifies cleanup cutoff and interval behavior.

`TestTrackerRelistenApprovalFlow`:

- Verifies duplicate candidates prompt without being saved.
- Verifies non-duplicates start tracking.
- Verifies relisten approval creates tracked relisten release.
- Verifies completion does not re-check duplicates.

`TestWordPressPostFetch`:

- Verifies first-page post cache validation and pagination.

`TestWordPressTagFetch`:

- Verifies tag pagination, in-memory cache reuse, cache refresh, and tag cache reconciliation.

`TestPublisherPostCacheRefresh`:

- Verifies post-cache refresh short-circuiting.
- Verifies forced refresh rebuilds cache and saves validation state.
- Verifies content formatting and WordPress content update.
- Verifies publishing still succeeds if forced cache refresh after publish fails, and now also exercises the new `PublishResult` contract.

`TestDuplicateDetection`:

- Verifies normalized title and artist-set matching behavior.

`TestLastFMMoodTags`:

- Verifies the Last.fm blocklist filter, the top-3 cap, and that flat-string tag lists work the same as dict-shaped inputs.

`TestLastFMClientAlbumGetInfo`:

- Verifies the client returns an empty dict when the API key is missing or the inputs are blank.
- Verifies `mbid` and tag-name extraction from a real-shaped payload.
- Verifies that HTTP errors and malformed payloads return an empty dict without raising.

`TestSCFDateHelpers`:

- Verifies `_coerce_spotify_release_date` expands `YYYY` to `YYYY-01-01` and `YYYY-MM` to `YYYY-MM-01` while leaving a full ISO date unchanged.
- Verifies `_format_scf_date` renders ISO dates and datetimes as `d/m/Y` and tolerates empty input.

`TestPublisherSCFFill`:

- Verifies `_build_scf_payload` uses only countable tracks, sums their durations, derives totals/averages/explicit-flag, and coerces release dates and post dates.
- Verifies `_fill_post_scf` PATCHes the `acf` block on the new post.
- Verifies `_count_listen_index` returns `matches + 1`.
- Verifies the Real `publish_release` path with SCF enabled returns a `PublishResult`, calls `update_post`, marks mood-tags pending when Last.fm has no tags, marks `scf_error` when the SCF update raises, and is a no-op when the feature flag is off.

`TestPublishNotificationEmbed`:

- Verifies the SCF auto-fill text appears when everything landed, the mood-tags-unavailable text appears when Last.fm had no tags, the corresponding `⚠️ SCF metadata` field is added in that case, and the `Listen count` field appears only when `result.listen_count > 1`.

### Manual Documentation Verification

Useful inventory command:

```bash
rg -n "^(class|def|async def) |^    (class|def|async def) " main.py src scripts tests
```

Useful endpoint/control command:

```bash
rg -n "@self\\.tree\\.command|custom_id=|/me/player|/me/albums|/me/library|/albums|/wp-json" main.py src README.md DEPLOYMENT.md DOCKER_DEPLOYMENT.md
```

## Deployment And Operations

### Local Python

Create environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Configure:

```bash
cp .env.example .env
```

Edit `.env` with Spotify, WordPress, and Discord credentials.

Run migrations:

```bash
PYTHONPATH=src python3 scripts/migrate.py
```

Run service:

```bash
PYTHONPATH=src python3 main.py
```

On first run, if no Spotify tokens exist, the service opens the Spotify authorization URL and asks for the redirected `code` parameter.

### Docker

`Dockerfile` uses:

- base image `python:3.12-slim`
- workdir `/app`
- installs `gcc`
- installs `requirements.txt`
- copies the project
- creates `/app/logs` and `/app/data`
- sets `PYTHONPATH=/app/src`
- runs `python main.py`

Build:

```bash
docker build -t spotify-wordpress-tracker .
```

Run directly:

```bash
docker run --env-file .env spotify-wordpress-tracker
```

### Docker Compose

`docker-compose.yml` defines service `spotify-wordpress-tracker` with:

- local build
- container name `spotify-wordpress-tracker`
- `.env` env file
- `./logs:/app/logs`
- `./data:/app/data`
- `restart: unless-stopped`
- `stdin_open: true`
- `tty: true`

Run:

```bash
docker-compose up --build
```

Detached:

```bash
docker-compose up -d --build
```

Logs:

```bash
docker-compose logs -f
```

Stop:

```bash
docker-compose down
```

### Systemd-Style Deployment

The existing `DEPLOYMENT.md` includes a systemd unit example. The key runtime requirements are:

- Working directory set to the project root.
- `ExecStart` runs the Python interpreter against `main.py`.
- Environment includes the virtualenv path or direct env vars.
- Service restarts on failure.
- Persistent `data/` and `logs/` directories are backed up.

Example shape:

```ini
[Unit]
Description=Spotify WordPress Album Tracker
After=network.target

[Service]
Type=simple
User=tim
WorkingDirectory=/path/to/SpotifyWordpressAlbumTracker
Environment="PATH=/path/to/SpotifyWordpressAlbumTracker/venv/bin"
ExecStart=/path/to/SpotifyWordpressAlbumTracker/venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Logs

Application logs:

```text
logs/album-tracker.log
```

Logs rotate daily with 14 backups.

Audit events:

```sql
SELECT event_type, data_json, timestamp
FROM audit_event
ORDER BY timestamp DESC
LIMIT 20;
```

### Health Checks

Discord:

- `/service`: service status, active release count, saved-library count, and listened percentage.
- `/current`: current playback and trackability.
- `/inprogress`: tracked release progress.
- `/random`: random unposted saved-library Album/EP.

SQLite:

```bash
sqlite3 data/album_tracker.db
```

Check active rows:

```sql
SELECT title, progress, status, last_seen
FROM release_lifecycle
ORDER BY last_seen DESC;
```

### Backups

Back up state:

```bash
tar czf album_tracker_backup.tar.gz data .env logs
```

Minimum critical files/directories:

- `data/album_tracker.db`
- `data/.spotify_tokens`
- `.env`
- `logs/` if operational history matters

### Troubleshooting

No active playback:

- Confirm Spotify is playing, not paused.
- Confirm playback is from an album context, not a playlist.
- Confirm shuffle is off.
- Confirm the track is not local.
- Confirm the Spotify token has `user-read-playback-state`.

Album not being tracked:

- Singles are intentionally skipped.
- Spotify item album type must be `album`.
- Context URI must match album URI.
- Existing WordPress duplicates require relisten approval before tracking.

No relisten prompt:

- Check cached WordPress posts in `wordpress_post_cache`.
- Check pending prompts in `discord_prompt`.
- Check whether an existing prompt is still live and unexpired.

WordPress publishing failure:

- Verify `WORDPRESS_URL`.
- Verify Application Password.
- Verify user can create posts, categories, tags, and media.
- Check WordPress REST API availability.
- Check `logs/album-tracker.log`.

Discord commands not responding:

- Verify `DISCORD_BOT_TOKEN`.
- Verify bot has slash command permissions.
- Verify `DISCORD_USER_ID` matches the operator's numeric Discord ID.
- Check startup logs for `Discord bot logged in as ...`.

Spotify token problems:

- Delete or rotate `data/.spotify_tokens`.
- Restart service to re-run authorization.
- Confirm redirect URI in Spotify developer dashboard matches `SPOTIFY_REDIRECT_URI`.

## Ambiguities And Current Behavior Notes

This section intentionally documents rough edges and current code truth.

- `SpotifyClient.get_recently_played()` exists but the tracker loop does not currently use it.
- Saved-library sync requires the `user-library-read` Spotify scope. Persisted tokens created before this scope was added may need to be regenerated.
- `/service` displays `last_poll` if present, but the tracker does not appear to write `service_state["last_poll"]`.
- `LifecycleStatus.AWAITING_RELISTEN_DECISION` is explicitly marked legacy, and current code should not enter it.
- Spotify OAuth callback handling is incomplete. `_wait_for_callback()` asks the user to paste the code instead of running a callback server.
- `Publisher.publish_release()` sets `release.published_at = None` after WordPress post creation. `Tracker._publish_release()` sets the real `published_at` timestamp afterward.
- Existing docs mention `album_tracker.db` in some examples. The actual configured database path is `data/album_tracker.db`.
- `Database.get_service_state()` contains unreachable commit code after `return`.
- `Database.save_release()` rewrites artist and track child rows on each save.
- `PromptState.USED` is defined but not a prominent state in current prompt handling.
- Current publish posts use empty initial content. Content can later be added through Discord's "Add content" modal.
- Duplicate detection depends on the WordPress post cache and artist tags. If WordPress tags do not reflect album artists, duplicate detection can miss or misclassify a relisten.
- `WORDPRESS_URL` and `WORDPRESS_PUBLIC_URL` serve different roles. The first is for authenticated REST calls, and the second is for links shown to the user.
- The tracker records a track as listened as soon as it observes that track in qualifying playback. It does not currently require listening for a minimum duration or reaching a track-end threshold.

## End-To-End Mental Model

The system is best understood as a state machine around a Spotify album ID:

1. Spotify playback exposes an album ID.
2. The tracker decides whether that playback is eligible.
3. A release row is created only if the album is not a skipped single and is not an unapproved duplicate.
4. Each observed countable track flips from unlistened to listened.
5. Progress is recomputed after every new listened track.
6. Human prompts appear at policy points: 75 percent, duplicate/relisten approval, and post-publish undo/content actions.
7. Publishing writes to WordPress and then marks the local release as recently published.
8. A cleanup window keeps local state briefly available, then deletes it.

That is the overarching ideology of the project: automate the boring observation and publishing mechanics, but keep the operator in control at moments where taste, duplication, or editorial judgment matters.
