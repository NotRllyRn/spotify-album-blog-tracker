# Spotify WordPress Album Tracker

A standalone Python service that monitors Spotify playback and automatically posts album releases to WordPress via REST API, with Discord bot control.

## Status: ✅ IMPLEMENTATION COMPLETE

The service is fully implemented and tested. It successfully:
- ✅ Monitors Spotify playback in real-time
- ✅ Classifies releases (Album/EP/Single/Compilation)
- ✅ Publishes to WordPress with artwork and metadata
- ✅ Prevents duplicate posts
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
```

## Discord Commands

- `/inprogress` - Show currently tracked releases
- `/current` - Show current playback status
- `/service` - Show service status

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
- **src/publisher.py**: WordPress publishing
- **src/discord_bot.py**: Discord control interface
- **src/models.py**: Data structures
- **src/utils.py**: Classification utilities

## Commands

- `/inprogress`: View active releases
- `/current`: Show current playback and manual publish
- `/service`: Service status

## License

MIT