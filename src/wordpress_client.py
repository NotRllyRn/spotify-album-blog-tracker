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
        self.api_url = f"{self.base_url}/wp-json/wp/v2"
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
        url = f"{self.api_url}/posts"
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
        url = f"{self.api_url}/posts"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def update_post(self, post_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update a post."""
        url = f"{self.api_url}/posts/{post_id}"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def delete_post(self, post_id: int, force: bool = False) -> Dict[str, Any]:
        """Delete a post (move to trash if force=False)."""
        url = f"{self.api_url}/posts/{post_id}"
        params = {"force": "true"} if force else {}
        response = await self.client.delete(url, params=params)
        response.raise_for_status()
        return response.json()

    async def get_categories(self) -> List[Dict[str, Any]]:
        """Get all categories."""
        url = f"{self.api_url}/categories?per_page=100"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def create_category(self, name: str) -> Dict[str, Any]:
        """Create a category."""
        url = f"{self.api_url}/categories"
        response = await self.client.post(url, json={"name": name})
        response.raise_for_status()
        return response.json()

    async def get_tags(self) -> List[Dict[str, Any]]:
        """Get all tags."""
        tags = []
        page = 1
        per_page = 100

        while True:
            response = await self.client.get(
                f"{self.api_url}/tags",
                params={"per_page": per_page, "page": page}
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError("Unexpected response when fetching tags")

            tags.extend(data)

            total_pages = response.headers.get("X-WP-TotalPages")
            if total_pages is not None:
                try:
                    total_pages = int(total_pages)
                except ValueError:
                    total_pages = None

            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(data) < per_page:
                break

            page += 1

        return tags

    async def get_tag_by_id(self, tag_id: int) -> Dict[str, Any]:
        """Get a tag by WordPress ID."""
        url = f"{self.api_url}/tags/{tag_id}"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def get_tag_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a tag by exact name."""
        for tag in await self.get_tags():
            if tag.get("name") == name:
                return tag
        return None

    async def create_tag(self, name: str) -> Dict[str, Any]:
        """Create a tag."""
        url = f"{self.api_url}/tags"
        response = await self.client.post(url, json={"name": name})
        try:
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                try:
                    body = exc.response.json()
                except ValueError:
                    raise

                if body.get("code") in {"term_exists", "existing_term_slug", "term_exists_invalid"}:
                    term_id = body.get("data", {}).get("term_id")
                    if term_id:
                        return await self.get_tag_by_id(term_id)

                    existing_tag = await self.get_tag_by_name(name)
                    if existing_tag:
                        return existing_tag

            raise

    async def upload_media(self, file_path: Path, alt_text: str = "") -> Dict[str, Any]:
        """Upload media file."""
        url = f"{self.api_url}/media"

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
        url = f"{self.api_url}/media/{media_id}"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def delete_media(self, media_id: int, force: bool = False) -> Dict[str, Any]:
        """Delete media."""
        url = f"{self.api_url}/media/{media_id}"
        params = {"force": "true"} if force else {}
        response = await self.client.delete(url, params=params)
        response.raise_for_status()
        return response.json()