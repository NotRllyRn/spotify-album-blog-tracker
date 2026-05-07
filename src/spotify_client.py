"""
Async Spotify API client.
"""

import httpx
import logging
import webbrowser
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import base64
import secrets
import urllib.parse
import hashlib

from config import Config

logger = logging.getLogger(__name__)

class SpotifyClient:
    def __init__(self, config: Config):
        self.config = config
        self.client_id = config.spotify_client_id
        self.client_secret = config.spotify_client_secret
        self.redirect_uri = config.spotify_redirect_uri
        self.access_token = config.spotify_access_token
        self.refresh_token = config.spotify_refresh_token
        self.token_expires_at: Optional[datetime] = None

        self.client = httpx.AsyncClient(
            base_url="https://api.spotify.com/v1",
            headers={"Authorization": f"Bearer {self.access_token}"} if self.access_token else {},
            timeout=30.0
        )

    async def ensure_authorized(self):
        """Ensure we have valid Spotify authorization."""
        if self.access_token and self.refresh_token:
            return

        logger.info("No Spotify tokens found. Starting authorization flow...")
        await self._authorize()

    async def _authorize(self):
        """Perform Spotify Authorization Code flow."""
        # Generate PKCE code verifier and challenge
        code_verifier = secrets.token_urlsafe(64)[:128]
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).decode().rstrip('=')

        # Build authorization URL
        auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'code_challenge_method': 'S256',
            'code_challenge': code_challenge,
            'scope': 'user-read-playback-state user-read-recently-played'
        })

        print("Opening browser for Spotify authorization...")
        print(f"If browser doesn't open, visit: {auth_url}")
        webbrowser.open(auth_url)

        # Start local server to receive callback
        code = await self._wait_for_callback()

        # Exchange code for tokens
        await self._exchange_code_for_tokens(code, code_verifier)

    async def _wait_for_callback(self) -> str:
        """Wait for OAuth callback and extract authorization code."""
        # For now, prompt user to paste the code
        # TODO: Implement proper callback server
        print("\nAfter authorizing, copy the 'code' parameter from the redirect URL and paste it here:")
        code = input("Authorization code: ").strip()
        return code

    async def _exchange_code_for_tokens(self, code: str, code_verifier: str):
        """Exchange authorization code for access and refresh tokens."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "client_id": self.client_id,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()
            data = response.json()

            self.access_token = data["access_token"]
            self.refresh_token = data["refresh_token"]
            expires_in = data.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            # Save tokens persistently
            self.config.save_tokens(self.access_token, self.refresh_token)

            # Update client headers
            self.client.headers["Authorization"] = f"Bearer {self.access_token}"

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    async def _ensure_token(self):
        """Ensure we have a valid access token."""
        if self.token_expires_at and datetime.now() < self.token_expires_at:
            return

        if not self.refresh_token:
            await self.ensure_authorized()
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

            # Save updated tokens
            self.config.save_tokens(self.access_token, self.refresh_token)

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