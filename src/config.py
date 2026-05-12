"""
Configuration management.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class Config:
    def __init__(self):
        # Paths
        self.project_root = Path(__file__).parent.parent
        self.db_path = self.project_root / "data" / "album_tracker.db"
        self.token_file = self.project_root / "data" / ".spotify_tokens"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs_path = self.project_root / "logs"
        self.env_file = self.project_root / ".env"

        # Load environment
        self._load_env()

        # Spotify
        self.spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.spotify_redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "https://musicblog.callita.day")
        self.spotify_access_token = os.getenv("SPOTIFY_ACCESS_TOKEN")
        self.spotify_refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN")

        # WordPress
        self.wordpress_url = os.getenv("WORDPRESS_URL", "http://10.17.3.3:8085")
        self.wordpress_public_url = os.getenv("WORDPRESS_PUBLIC_URL", "https://musicblog.callita.day")  # Used for links in posts
        self.wordpress_username = os.getenv("WORDPRESS_USERNAME")
        self.wordpress_app_password = os.getenv("WORDPRESS_APP_PASSWORD")

        # Discord
        self.discord_bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.discord_user_id = int(os.getenv("DISCORD_USER_ID"))

        # Load persisted tokens if available
        self._load_persisted_tokens()

        # Validate required config
        self._validate()

    def _load_env(self):
        """Load .env file if it exists."""
        if self.env_file.exists():
            from dotenv import load_dotenv
            load_dotenv(self.env_file)

    def _load_persisted_tokens(self):
        """Load Spotify tokens from persistent file."""
        if self.token_file.exists():
            try:
                import json
                with open(self.token_file, 'r') as f:
                    tokens = json.load(f)
                if not self.spotify_access_token:
                    self.spotify_access_token = tokens.get('access_token')
                if not self.spotify_refresh_token:
                    self.spotify_refresh_token = tokens.get('refresh_token')
            except Exception as e:
                logger.warning("Could not load persisted tokens: %s", e)

    def save_tokens(self, access_token: str, refresh_token: str):
        """Save Spotify tokens to persistent file."""
        import json
        tokens = {
            'access_token': access_token,
            'refresh_token': refresh_token
        }
        with open(self.token_file, 'w') as f:
            json.dump(tokens, f)

    def _validate(self):
        """Validate required configuration."""
        required = [
            ("SPOTIFY_CLIENT_ID", self.spotify_client_id),
            ("SPOTIFY_CLIENT_SECRET", self.spotify_client_secret),
            ("WORDPRESS_URL", self.wordpress_url),
            ("WORDPRESS_USERNAME", self.wordpress_username),
            ("WORDPRESS_APP_PASSWORD", self.wordpress_app_password),
            ("DISCORD_BOT_TOKEN", self.discord_bot_token),
            ("DISCORD_USER_ID", self.discord_user_id),
        ]

        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"
