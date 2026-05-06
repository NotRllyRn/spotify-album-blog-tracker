"""
Async WordPress REST API client.
"""

import httpx
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
import base64

from config import Config

logger = logging.getLogger(__name__)

class WordPressClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.wordpress_url.rstrip("/")
        self.username = config.wordpress_username
        self.app_password = config.wordpress_app_password

        # Basic auth header
        auth_string = f"{self.username}:{self.app_password}"
        auth_b64 = base64.b64encode(auth_string.encode()).decode()
        self.auth_header = f"Basic {auth_b64}"

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0
        )

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    async def get_posts(self, **params) -> List[Dict[str, Any]]:
        """Get posts with pagination."""
        url = f"{self.base_url}/wp/v2/posts"
        all_posts = []

        while url:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            all_posts.extend(data)

            # Check for next page
            next_url = response.headers.get("X-WP-Next")
            url = next_url

        return all_posts

    async def create_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new post."""
        url = f"{self.base_url}/wp/v2/posts"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def update_post(self, post_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update a post."""
        url = f"{self.base_url}/wp/v2/posts/{post_id}"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def delete_post(self, post_id: int, force: bool = False) -> Dict[str, Any]:
        """Delete a post (move to trash if force=False)."""
        url = f"{self.base_url}/wp/v2/posts/{post_id}"
        params = {"force": "true"} if force else {}
        response = await self.client.delete(url, params=params)
        response.raise_for_status()
        return response.json()

    async def get_categories(self) -> List[Dict[str, Any]]:
        """Get all categories."""
        url = f"{self.base_url}/wp/v2/categories?per_page=100"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def create_category(self, name: str) -> Dict[str, Any]:
        """Create a category."""
        url = f"{self.base_url}/wp/v2/categories"
        response = await self.client.post(url, json={"name": name})
        response.raise_for_status()
        return response.json()

    async def get_tags(self) -> List[Dict[str, Any]]:
        """Get all tags."""
        url = f"{self.base_url}/wp/v2/tags?per_page=100"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def create_tag(self, name: str) -> Dict[str, Any]:
        """Create a tag."""
        url = f"{self.base_url}/wp/v2/tags"
        response = await self.client.post(url, json={"name": name})
        response.raise_for_status()
        return response.json()

    async def upload_media(self, file_path: Path, alt_text: str = "") -> Dict[str, Any]:
        """Upload media file."""
        url = f"{self.base_url}/wp/v2/media"

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "image/jpeg")}
            data = {"alt_text": alt_text} if alt_text else {}

            # Remove content-type header for file upload
            headers = self.client.headers.copy()
            headers.pop("Content-Type", None)

            async with httpx.AsyncClient(headers=headers, timeout=60.0) as upload_client:
                response = await upload_client.post(url, files=files, data=data)
                response.raise_for_status()
                return response.json()

    async def update_media(self, media_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update media metadata."""
        url = f"{self.base_url}/wp/v2/media/{media_id}"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def delete_media(self, media_id: int, force: bool = False) -> Dict[str, Any]:
        """Delete media."""
        url = f"{self.base_url}/wp/v2/media/{media_id}"
        params = {"force": "true"} if force else {}
        response = await self.client.delete(url, params=params)
        response.raise_for_status()
        return response.json()