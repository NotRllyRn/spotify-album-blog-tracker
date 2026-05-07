# Spotify WordPress Album Tracker - Docker Deployment

This guide explains how to deploy the Spotify WordPress Album Tracker using Docker.

## Prerequisites

- Docker and Docker Compose installed
- Spotify App credentials
- WordPress site with REST API enabled
- Discord Bot token

## Quick Start

1. **Clone and configure:**
   ```bash
   git clone <repository-url>
   cd SpotifyWordpressAlbumTracker
   cp .env.example .env
   # Edit .env with your credentials
   ```

2. **Build and run:**
   ```bash
   docker-compose up --build
   ```

## Configuration

The application uses environment variables for configuration. Copy `.env.example` to `.env` and fill in your values:

```env
# Spotify Configuration
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REDIRECT_URI=https://your-domain.com/callback

# WordPress Configuration
WORDPRESS_URL=https://your-wordpress-site.com
WORDPRESS_USERNAME=albumtracker
WORDPRESS_APP_PASSWORD=your_application_password

# Discord Configuration
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_USER_ID=your_discord_user_id
```

## WordPress Setup

1. **Enable REST API:** Ensure your WordPress site has the REST API enabled (usually enabled by default in WordPress 4.7+)

2. **Create Application Password:**
   - Go to WordPress Admin → Users → Your Profile
   - Scroll to "Application Passwords" section
   - Create a new password for "albumtracker"
   - Use this password in the `WORDPRESS_APP_PASSWORD` environment variable

3. **Install Album Art Picker Plugin (Optional):**
   - The service works with standard WordPress, but for enhanced functionality, install the companion plugin
   - Upload the `spotify-album-art-picker.php` file to your plugins directory

## Docker Commands

```bash
# Build the image
docker build -t spotify-wordpress-tracker .

# Run with environment file
docker run --env-file .env spotify-wordpress-tracker

# Run with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the service
docker-compose down
```

## Troubleshooting

### WordPress 404 Errors
- Verify the WordPress URL is correct
- Ensure the REST API is enabled
- Check Application Password is valid
- Confirm the WordPress user has publishing permissions

### Spotify Authorization
- The service will prompt for authorization on first run
- Tokens are persisted in `.spotify_tokens`
- Re-authorize if tokens expire

### Discord Bot Issues
- Ensure the bot token is valid
- Check bot has necessary permissions in your server
- Verify the Discord User ID is correct

## Production Deployment

For production deployment:

1. **Use a reverse proxy** (nginx/caddy) for the redirect URI
2. **Set up monitoring** for the service logs
3. **Configure log rotation** for the logs directory
4. **Use Docker secrets** instead of environment variables for sensitive data
5. **Set up automated backups** of the SQLite database

## File Structure

```
/app/
├── main.py                 # Service entry point
├── album_tracker.db       # SQLite database
├── .spotify_tokens        # Persisted Spotify tokens
├── logs/                  # Application logs
└── src/                   # Source code
    ├── config.py
    ├── database.py
    ├── discord_bot.py
    ├── models.py
    ├── publisher.py
    ├── spotify_client.py
    ├── tracker.py
    ├── utils.py
    └── wordpress_client.py
```