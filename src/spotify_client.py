"""
Async Spotify API client.
"""

import httpx
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

from config import Config

logger = logging.getLogger(__name__)

class SpotifyClient:
    def __init__(self, config: Config):
        self.config = config
        self.client_id = config.spotify_client_id
        self.client_secret = config.spotify_client_secret
        self.access_token = config.spotify_access_token
        self.refresh_token = config.spotify_refresh_token
        self.token_expires_at: Optional[datetime] = None

        self.client = httpx.AsyncClient(
            base_url="https://api.spotify.com/v1",
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=30.0
        )

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    async def _ensure_token(self):
        """Ensure we have a valid access token."""
        if self.token_expires_at and datetime.now() < self.token_expires_at:
            return

        # Refresh token
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()
            data = response.json()

            self.access_token = data["access_token"]
            if "refresh_token" in data:
                self.refresh_token = data["refresh_token"]

            expires_in = data.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            # Update client headers
            self.client.headers["Authorization"] = f"Bearer {self.access_token}"

    async def get_playback_state(self) -> Optional[Dict[str, Any]]:
        """Get current playback state."""
        await self._ensure_token()

        try:
            response = await self.client.get("/me/player")
            if response.status_code == 204:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning("Spotify token expired, will refresh on next call")
                self.token_expires_at = None
            raise

    async def get_album(self, album_id: str) -> Dict[str, Any]:
        """Get album details."""
        await self._ensure_token()

        response = await self.client.get(f"/albums/{album_id}")
        response.raise_for_status()
        return response.json()

    async def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        """Get all tracks for an album."""
        await self._ensure_token()

        tracks = []
        url = f"/albums/{album_id}/tracks?limit=50"

        while url:
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()

            tracks.extend(data["items"])
            url = data.get("next")

        return tracks

    async def get_recently_played(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recently played tracks."""
        await self._ensure_token()

        response = await self.client.get(f"/me/player/recently-played?limit={limit}")
        response.raise_for_status()
        return response.json()["items"]