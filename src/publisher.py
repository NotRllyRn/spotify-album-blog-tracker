"""
WordPress publishing service.
"""

import httpx
import logging
from typing import Dict, Any, Optional
from pathlib import Path
import tempfile

from config import Config
from wordpress_client import WordPressClient
from models import Release

logger = logging.getLogger(__name__)

class Publisher:
    """Handles publishing releases to WordPress."""

    def __init__(self, config: Config):
        self.config = config
        self.wordpress = WordPressClient(config)
        self.category_cache: Dict[str, int] = {}
        self.tag_cache: Dict[str, int] = {}

    async def close(self):
        """Close WordPress client."""
        await self.wordpress.close()

    async def publish_release(self, release: Release, as_relisten: bool = False) -> Dict[str, Any]:
        """Publish a release to WordPress."""
        logger.info(f"Publishing {release.title} to WordPress")

        try:
            # Ensure categories exist
            await self._ensure_categories()

            # Download and upload media
            media_id = await self._upload_artwork(release)

            # Resolve or create artist tags
            tag_ids = await self._resolve_tags([a.name for a in release.artists])

            # Determine categories
            category_ids = [self.category_cache[release.release_type.value]]
            if as_relisten:
                if "Relisten" in self.category_cache:
                    category_ids.append(self.category_cache["Relisten"])

            # Create post
            post_data = {
                "title": release.title,
                "content": "",  # Empty or minimal placeholder
                "status": "publish",
                "categories": category_ids,
                "tags": tag_ids,
                "featured_media": media_id if media_id else 0,
            }

            post = await self.wordpress.create_post(post_data)
            logger.info(f"Post created: {post['id']} - {post['title']}")

            release.wordpress_post_id = post["id"]
            release.wordpress_media_id = media_id
            release.published_at = None  # Will be set by tracker

            return post

        except Exception as e:
            logger.error(f"Error publishing release: {e}")
            raise

    async def trash_post(self, post_id: int) -> bool:
        """Move post to trash (undo)."""
        try:
            await self.wordpress.delete_post(post_id, force=False)
            logger.info(f"Post {post_id} moved to trash")
            return True
        except Exception as e:
            logger.error(f"Error trashing post {post_id}: {e}")
            return False

    async def _ensure_categories(self):
        """Ensure required categories exist."""
        required_categories = ["Album", "EP", "Single", "Compilation", "Relisten"]

        for category_name in required_categories:
            if category_name not in self.category_cache:
                # Try to get existing
                categories = await self.wordpress.get_categories()
                found = False
                for cat in categories:
                    if cat["name"] == category_name:
                        self.category_cache[category_name] = cat["id"]
                        found = True
                        break

                # Create if not found
                if not found:
                    new_cat = await self.wordpress.create_category(category_name)
                    self.category_cache[category_name] = new_cat["id"]
                    logger.info(f"Created category: {category_name}")

    async def _resolve_tags(self, artist_names: list) -> list:
        """Resolve or create artist tags."""
        tag_ids = []

        # Get all existing tags
        existing_tags = await self.wordpress.get_tags()
        existing_tag_map = {tag["name"]: tag["id"] for tag in existing_tags}

        for artist_name in artist_names:
            print(artist_name)
            if artist_name in self.tag_cache:
                tag_ids.append(self.tag_cache[artist_name])
            elif artist_name in existing_tag_map:
                tag_id = existing_tag_map[artist_name]
                self.tag_cache[artist_name] = tag_id
                tag_ids.append(tag_id)
            else:
                # Create new tag
                new_tag = await self.wordpress.create_tag(artist_name)
                self.tag_cache[artist_name] = new_tag["id"]
                tag_ids.append(new_tag["id"])
                logger.info(f"Created tag: {artist_name}")

        return tag_ids

    async def _upload_artwork(self, release: Release) -> Optional[int]:
        """Download Spotify artwork and upload to WordPress."""
        try:
            # Download image from Spotify
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(release.cover_url)
                response.raise_for_status()
                image_bytes = response.content

            # Save to temp file
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(image_bytes)
                tmp_path = Path(tmp.name)

            try:
                # Upload to WordPress
                alt_text = f"{release.title} album art"
                media = await self.wordpress.upload_media(tmp_path, alt_text=alt_text)
                logger.info(f"Artwork uploaded: media_id={media['id']}")
                return media["id"]
            finally:
                # Clean up temp file
                tmp_path.unlink()

        except Exception as e:
            logger.error(f"Error uploading artwork: {e}")
            return None

    async def refresh_post_cache(self):
        """Refresh WordPress post cache for duplicate detection."""
        try:
            logger.info("Refreshing WordPress post cache...")
            posts = await self.wordpress.get_posts(status="publish", _fields="id,title,tags,link")

            # Get all tags
            tags = await self.wordpress.get_tags()
            tag_map = {t["id"]: t["name"] for t in tags}

            # Process posts
            from models import WordPressPost
            from utils import normalize_text, normalize_artist_list

            cache = []
            for post in posts:
                # Get tag names from tag IDs
                post_tags = [tag_map.get(t, "") for t in post.get("tags", [])]

                cache_item = WordPressPost(
                    id=post["id"],
                    title=post["title"],
                    normalized_title=normalize_text(post["title"]),
                    artists=post_tags,
                    normalized_artists=normalize_artist_list(post_tags),
                    link=post.get("link", "")
                )
                cache.append(cache_item)

            # Save to database
            from database import Database
            db = Database(self.config)
            await db.initialize()
            await db.save_wordpress_posts(cache)
            await db.close()

            logger.info(f"Updated post cache: {len(cache)} posts")

        except Exception as e:
            logger.error(f"Error refreshing post cache: {e}")
