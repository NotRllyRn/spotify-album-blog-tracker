# Deployment Guide

## System Requirements

- Python 3.12+
- pip or uv for package management
- SQLite3 (included with Python)
- macOS, Linux, or WSL (not tested on Windows directly)

## Installation

### 1. Clone the repository

```bash
git clone <repository_url>
cd SpotifyWordpressAlbumTracker
```

### 2. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

#### Spotify Configuration

1. Go to https://developer.spotify.com/dashboard
2. Create an application
3. Note your Client ID and Client Secret
4. Set Authorization Code flow redirect URI to `http://localhost:8080/callback`

```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8080/callback
```

For initial access token and refresh token, run the authorization flow:

```bash
python scripts/spotify_auth.py
```

This will open your browser to authorize the app. After approval, tokens will be saved to `.env`.

#### WordPress Configuration

1. Go to your WordPress admin dashboard: `https://your-site.com/wp-admin`
2. Navigate to Users → your-user → App Passwords
3. Create a new application password named "Album Tracker"
4. Note the generated password (shows once, in format: `XXXX XXXX XXXX XXXX`)

```
WORDPRESS_URL=https://your-site.com  # Or local: http://10.17.3.3:8085
WORDPRESS_USERNAME=albumtracker       # Or your WordPress user
WORDPRESS_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

#### Discord Configuration

1. Go to https://discord.com/developers/applications
2. Create a new application
3. Go to Bot → Add Bot
4. Copy the bot token

```
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_USER_ID=your_discord_user_id  # Your numeric Discord ID
```

### 5. Initialize database

```bash
python scripts/migrate.py
```

This creates the SQLite database and schema.

### 6. Run the service

```bash
python main.py
```

You should see output like:

```
2026-05-05 18:15:22 - __main__ - INFO - Starting Spotify WordPress Album Tracker...
2026-05-05 18:15:22 - src.database - INFO - Initializing database...
2026-05-05 18:15:22 - src.publisher - INFO - Refreshing WordPress post cache...
2026-05-05 18:15:23 - __main__ - INFO - Starting tracker...
2026-05-05 18:15:24 - src.discord_bot - INFO - Discord bot logged in as AlbumTracker#1234
```

## Docker Deployment

### Build the image

```bash
docker build -t album-tracker:latest .
```

### Run the container

```bash
docker run -d \
  --name album-tracker \
  -v $(pwd)/.env:/app/.env:ro \
  -v album-tracker-data:/app/data \
  --restart unless-stopped \
  album-tracker:latest
```

### View logs

```bash
docker logs -f album-tracker
```

## Systemd Deployment (Linux/macOS)

### Create service file

Create `/etc/systemd/system/album-tracker.service`:

```ini
[Unit]
Description=Spotify WordPress Album Tracker
After=network.target

[Service]
Type=simple
User=tim
WorkingDirectory=/Users/tim/Documents/Code/SpotifyWordpressAlbumTracker
Environment="PATH=/Users/tim/Documents/Code/SpotifyWordpressAlbumTracker/venv/bin"
ExecStart=/Users/tim/Documents/Code/SpotifyWordpressAlbumTracker/venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable album-tracker
sudo systemctl start album-tracker
sudo systemctl status album-tracker
```

### View logs

```bash
sudo journalctl -u album-tracker -f
```

## Health Checks

### Discord Commands

Once running, use Discord commands to check status:

- `/service`: Overall health and statistics
- `/current`: Current playback state
- `/inprogress`: Active releases being tracked

### Manual Testing

1. Start listening to an album on Spotify (in album context, not shuffled)
2. Wait 3-5 seconds
3. Run `/current` in Discord to confirm it's being tracked
4. As you complete tracks, watch `/inprogress` to see progress

### Database Inspection

```bash
sqlite3 album_tracker.db

# View active releases
SELECT title, release_type, progress, status FROM release_lifecycle;

# View recent audit events
SELECT event_type, data_json, timestamp FROM audit_event ORDER BY timestamp DESC LIMIT 10;
```

## Troubleshooting

### No active playback detected

- Ensure Spotify is actively playing (not paused)
- Ensure you're playing from an album (not a playlist)
- Ensure shuffle is OFF
- Check that the track is not a local file
- Verify Spotify scopes include `user-read-playback-state`

### WordPress post creation fails

- Verify WordPress site is accessible at the configured URL
- Confirm Application Password is correct and not expired
- Check that the user has publish_posts capability
- Verify categories "Album", "EP", "Single", "Compilation" exist or can be created

### Discord commands not responding

- Verify Discord bot token is correct
- Ensure bot has application.commands and chat_input_command permissions
- Confirm your Discord User ID is set in .env
- Check Discord bot is in the server with send_messages permission

### Duplicate posts when should relisten

- Normalize manually: Go to WordPress, verify artist tags have commas stripped
- Check database: `SELECT * FROM discord_prompt WHERE state='pending'`
- If relisten prompt exists but wasn't acted on, you can manually update: `UPDATE discord_prompt SET state='used'`

## Logs

Logs are stored in:

- **Application logs**: `logs/album-tracker.log` (rotating daily)
- **Database audit events**: `album_tracker.db` → `audit_event` table

Enable debug logging in `config.py`:

```python
logging.basicConfig(level=logging.DEBUG)
```

## Upgrading

### Pull latest code

```bash
git pull origin main
```

### Apply migrations

```bash
python scripts/migrate.py
```

### Restart service

```bash
# If using systemd:
sudo systemctl restart album-tracker

# Or manually restart:
# Stop the current process and run 'python main.py' again
```

## Backups

### Back up database

```bash
cp album_tracker.db album_tracker.db.backup.$(date +%Y%m%d)
```

### Back up to remote

```bash
tar czf album_tracker_backup.tar.gz album_tracker.db .env logs/
scp album_tracker_backup.tar.gz user@remote:/backups/
```

## Security Considerations

1. **Never commit `.env`** - Keep credentials out of version control
2. **Use HTTPS** - WordPress should be HTTPS-only
3. **Rotate app passwords** - Periodically generate new Application Passwords in WordPress
4. **Limit Discord user** - Only your user ID can run commands
5. **Database permissions** - Ensure `album_tracker.db` is readable only by the service user

## Performance Tuning

The tracker uses adaptive polling intervals:

- **3 seconds** while actively playing
- **8 seconds** while paused or non-qualifying playback
- **15 seconds** during idle (no playback)

These can be adjusted in `src/tracker.py` if needed, but defaults balance responsiveness and rate-limit safety.

## Support

For issues, check:

1. This guide's troubleshooting section
2. Application logs in `logs/`
3. Database audit events
4. Discord command responses for error details

