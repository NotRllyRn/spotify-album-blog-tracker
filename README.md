# Spotify WordPress Album Tracker

A standalone Python service that monitors Spotify playback and automatically posts album releases to WordPress via REST API, with Discord bot control.

## Features

- Continuous Spotify playback monitoring
- Automatic album release classification (Album/EP/Single/Compilation)
- WordPress post creation with artwork, categories, and artist tags
- Duplicate detection to avoid reposting
- Discord slash commands for control and manual publishing
- SQLite database for state persistence

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Configure secrets in `.env` file
3. Run migrations: `python scripts/migrate.py`
4. Start the service: `python main.py`

## Configuration

Create a `.env` file with:

```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8080/callback
SPOTIFY_ACCESS_TOKEN=...
SPOTIFY_REFRESH_TOKEN=...

WORDPRESS_URL=https://your-site.com
WORDPRESS_USERNAME=albumtracker
WORDPRESS_APP_PASSWORD=your_app_password

DISCORD_BOT_TOKEN=your_bot_token
DISCORD_USER_ID=your_user_id
```

## Architecture

- `src/config.py`: Configuration loading
- `src/database.py`: SQLite schema and queries
- `src/spotify_client.py`: Async Spotify API client
- `src/wordpress_client.py`: Async WordPress REST client
- `src/tracker.py`: Main polling loop
- `src/discord_bot.py`: Discord bot commands
- `src/models.py`: Data models
- `src/utils.py`: Helper functions

## Commands

- `/inprogress`: View active releases
- `/current`: Show current playback and manual publish
- `/service`: Service status

## License

MIT