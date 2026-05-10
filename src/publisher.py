"""
WordPress publishing service.
"""

import httpx
import logging
import re
from typing import Dict, Any, Optional
from pathlib import Path
import tempfile
from html import escape

from config import Config
from database import Database
from wordpress_client import WordPressClient
from models import Release

logger = logging.getLogger(__name__)

POST_CACHE_TOTAL_KEY = "wordpress_post_cache.x_wp_total"
POST_CACHE_FIRST_PAGE_HASH_KEY = "wordpress_post_cache.first_page_hash"


def format_discord_content_for_wordpress(raw_content: str) -> str:
    """Convert Discord modal text into simple WordPress paragraph HTML."""
    normalized = raw_content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n[ \t]*\n+", normalized)
        if paragraph.strip()
    ]

    formatted_paragraphs = []
    for paragraph in paragraphs:
        escaped_paragraph = escape(paragraph).replace("\n", "<br />")
        formatted_paragraphs.append(f"<p>{escaped_paragraph}</p>")

    return "\n\n".join(formatted_paragraphs)


class Publisher:
    """Handles publishing releases to WordPress."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
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

            try:
                await self.refresh_post_cache(force=True)
            except Exception as e:
                logger.error(f"Post cache refresh failed after publish: {e}")

            return post

        except Exception as e:
            logger.error(f"Error publishing release: {e}")
            raise

    async def trash_post(self, post_id: int) -> bool:
        """Move post to trash (undo)."""
        try:
            await self.wordpress.delete_post(post_id, force=False)
            await self.refresh_post_cache(force=True)
            logger.info(f"Post {post_id} moved to trash")
            return True
        except Exception as e:
            logger.error(f"Error trashing post {post_id}: {e}")
            return False

    async def update_post_content(self, post_id: int, raw_content: str) -> Dict[str, Any]:
        """Replace a WordPress post body with formatted Discord-submitted content."""
        formatted_content = format_discord_content_for_wordpress(raw_content)
        post = await self.wordpress.update_post(post_id, {"content": formatted_content})
        logger.info(f"Updated WordPress post content: post_id={post_id}")
        return post

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

    async def refresh_post_cache(self, force: bool = False) -> Optional[str]:
        """Refresh WordPress post cache for duplicate detection."""
        try:
            logger.info("Refreshing WordPress post cache...")
            previous_x_wp_total = None if force else await self.db.get_service_state(POST_CACHE_TOTAL_KEY)
            previous_first_page_hash = None if force else await self.db.get_service_state(POST_CACHE_FIRST_PAGE_HASH_KEY)

            posts_result = await self.wordpress.get_posts(
                validate_first_page=not force,
                previous_x_wp_total=previous_x_wp_total,
                previous_first_page_hash=previous_first_page_hash,
                status="publish",
                _fields="id,title,tags,link",
            )

            if posts_result.cache_unchanged:
                logger.info(posts_result.message)
                return posts_result.message

            # Get all tags
            tags = await self.wordpress.get_tags()
            tag_map = {t["id"]: t["name"] for t in tags}

            # Process posts
            from models import WordPressPost
            from utils import normalize_text, normalize_artist_list

            cache = []
            for post in posts_result.posts:
                # Get tag names from tag IDs
                post_tags = [tag_map.get(t, "") for t in post.get("tags", [])]

                cache_item = WordPressPost(
                    id=post["id"],
                    title=post["title"]["rendered"],
                    normalized_title=normalize_text(post["title"]["rendered"]),
                    artists=post_tags,
                    normalized_artists=normalize_artist_list(post_tags),
                    link=post.get("link", "")
                )
                cache.append(cache_item)

            await self.db.save_wordpress_posts(cache)
            await self._save_post_cache_validation_state(posts_result)

            message = f"Updated post cache: {len(cache)} posts"
            logger.info(message)
            return message

        except Exception as e:
            logger.error(f"Error refreshing post cache: {e}")
            return None

    async def _save_post_cache_validation_state(self, posts_result):
        """Persist page-1 metadata after a successful full post-cache refresh."""
        if not posts_result.x_wp_total:
            logger.info("Skipping WordPress post cache validation state save; response headers were incomplete.")
            return

        await self.db.save_service_state(POST_CACHE_TOTAL_KEY, posts_result.x_wp_total)
        await self.db.save_service_state(POST_CACHE_FIRST_PAGE_HASH_KEY, posts_result.first_page_hash)
