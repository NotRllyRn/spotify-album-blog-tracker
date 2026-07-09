# Spotify WordPress Album Tracker

A standalone Python service that monitors Spotify playback and automatically posts album releases to WordPress via REST API, with Discord bot control.

## Status: ✅ IMPLEMENTATION COMPLETE

The service is fully implemented and tested. It successfully:

- ✅ Monitors Spotify playback in real-time
- ✅ Classifies releases (Album/EP/Single/Compilation)
- ✅ Publishes to WordPress with artwork and metadata
- ✅ Prevents duplicate posts
- ✅ Tracks saved Spotify library Albums/EPs as a listen-to list
- ✅ Provides Discord bot control
- ✅ Persists state in SQLite database

## Quick Start

1. **Set up environment:**

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure credentials:**

   ```bash
   cp .env.example .env
   # Edit .env with your Spotify, WordPress, and Discord credentials
   ```

3. **Initialize database:**

   ```bash
   PYTHONPATH=src python scripts/migrate.py
   ```

4. **Run the service:**

   ```bash
   PYTHONPATH=src python main.py
   ```

   The service will prompt for Spotify authorization on first run.

## Docker Deployment

For containerized deployment:

```bash
docker-compose up --build
```

See [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md) for detailed instructions.

## Configuration

Required environment variables in `.env`:

```env
# Spotify API
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=https://your-domain.com/callback

# WordPress REST API
WORDPRESS_URL=https://your-wordpress-site.com
WORDPRESS_USERNAME=albumtracker
WORDPRESS_APP_PASSWORD=your_app_password

# Discord Bot
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_USER_ID=your_user_id

# Last.fm + SCF auto-fill (optional but recommended)
LASTFM_API_KEY=your_lastfm_api_key
# Set to "1" to auto-fill the same SCF `acf` block that Wordpress-PostToAlbum-Script writes.
# When enabled, LASTFM_API_KEY becomes required.
SPOTIFY_BLOG_TRACKER_FILL_SCF=1
```

## Discord Commands

- `/inprogress` - Show currently tracked releases
- `/current` - Show current playback status
- `/random` - Pick a random unposted album from your saved Spotify library, with a re-roll button
- `/service` - Show service status
- `/search query:str` - Fuzzy search cached WordPress posts; pick one to open the persistent metadata editor in your DM
- `/editor post_id:int` - Open the metadata editor against a known WordPress post ID

## SCF Editor

Every Discord-published post can be edited through a persistent editor embed sent to your DM. The editor supports both pre-publish and post-publish modes:

- **Pre-publish**: open from `/inprogress` → select a release → "Edit metadata". Edits land in the SQLite row and ride along with the publish flow (rating, favorite, notes, unreleased, per-track highlight).
- **Post-publish**: open from the publish-confirmation embed's "Edit metadata" button. Edits PATCH the live WordPress `acf` block via `POST /wp/v2/posts/{id}`.

Bool fields (`favorite`, `unreleased`) flip inline with one click. Number and long-text fields open a single-field modal. Per-track highlights live in a paginated sub-view. There is also a "Re-sync from WP" button to re-read SCF after manual WP edits, and a "Body" modal to replace the post body.

## SCF Auto-Fill

When `SPOTIFY_BLOG_TRACKER_FILL_SCF=1`, every Discord-published release is backfilled with the same SCF `acf` block that the `Wordpress-PostToAlbum-Script` writes for the rest of the blog: `music_tracks`, `music_length_ms`, `spotify_album_id`, `spotify_album_url`, `music_release_date`, `music_listened_at`, `lastfm_release_id`, `music_total_tracks`, `music_avg_track_ms`, `music_explicit`, `music_mood_tags`, and `listen-count`. Spotify data is reused from the in-memory `Release`; mood tags and the MusicBrainz release ID come from one Last.fm `album.getinfo` call. The publish notification surfaces a `Listen count` field when the value is greater than one and a `⚠️ SCF metadata` warning if Last.fm returned no mood tags.

## WordPress Setup

1. Enable WordPress REST API (enabled by default in WordPress 4.7+)
2. Create an Application Password for the albumtracker user
3. Optionally install the companion Album Art Picker plugin

## Architecture

- **main.py**: Service orchestration
- **src/config.py**: Configuration management
- **src/database.py**: SQLite persistence
- **src/spotify_client.py**: Spotify API integration
- **src/tracker.py**: Playback monitoring logic
- **src/publisher.py**: WordPress publishing + SCF auto-fill
- **src/wordpress_client.py**: WordPress REST API client
- **src/discord_bot.py**: Discord control interface
- **src/lastfm_client.py**: Last.fm client used for SCF mood tags
- **src/editor_view.py**: Persistent Discord editor (pre/post-publish SCF edits)
- **src/models.py**: Data structures
- **src/utils.py**: Classification utilities

## Commands

- `/inprogress`: View active releases
- `/current`: Show current playback and manual publish
- `/random`: Pick a random unposted saved-library album
- `/service`: Service status
- `/search query`: Fuzzy search cached WordPress posts; pick one to open the persistent metadata editor in your DM
- `/editor post_id`: Open the metadata editor against a known WordPress post ID

## Editor Flow

The SCF editor is the recommended way to add ratings, notes, favorites, unreleased flags, and per-track highlights. Pre-publish edits are persisted to the local database and emitted as part of the SCF auto-fill payload when the release is published. Post-publish edits immediately PATCH the live `acf` block on WordPress; a re-fetch via "Re-sync from WP" reads the canonical values back from the post.

## License

MIT
