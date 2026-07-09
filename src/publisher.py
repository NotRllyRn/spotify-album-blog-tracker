"""
WordPress publishing service.
"""

import httpx
import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
import tempfile
from html import escape

from config import Config
from database import Database
from wordpress_client import WordPressClient
from models import PublishResult, Release
from lastfm_client import LastFMClient, pick_mood_tags
from search import LAST_SYNCED_AT_KEY as POST_CACHE_LAST_SYNCED_AT_KEY

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


def _coerce_spotify_release_date(value: str) -> str:
    """SCF rejects partial dates; expand ``YYYY`` / ``YYYY-MM`` to first-of-month."""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _format_scf_date(value: Optional[str]) -> str:
    """Render an ISO date (or empty) as ``d/m/Y`` for SCF ``date_picker`` fields."""
    if not value:
        return ""
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).strftime("%d/%m/%Y")
    except ValueError:
        return text


class Publisher:
    """Handles publishing releases to WordPress."""

    # Class-level default so test code paths that bypass __init__ via __new__
    # can still construct a publisher with SCF auto-fill disabled.
    _fill_scf_enabled: bool = False

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.wordpress = WordPressClient(config)
        self.lastfm = LastFMClient(getattr(config, "lastfm_api_key", None))
        self.category_cache: Dict[str, int] = {}
        self.tag_cache: Dict[str, int] = {}
        self._fill_scf_enabled = bool(getattr(config, "fill_scf_enabled", False))

    async def close(self):
        """Close WordPress client."""
        await self.wordpress.close()
        await self.lastfm.close()

    async def publish_release(self, release: Release, as_relisten: bool = False) -> PublishResult:
        """Publish a release to WordPress and return a typed publish outcome."""
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

            # Count existing matching posts BEFORE create_post so the count
            # does not double-count the about-to-be-created post.
            listen_count = await self._count_listen_index(release) if self._fill_scf_enabled else 1

            post = await self.wordpress.create_post(post_data)
            logger.info(f"Post created: {post['id']} - {post['title']}")

            release.wordpress_post_id = post["id"]
            release.wordpress_media_id = media_id
            release.published_at = None  # Will be set by tracker

            scf_pending_tags: list[str] = []
            if self._fill_scf_enabled:
                try:
                    acf_payload, fetch_status = await self._build_scf_payload(release, listen_count, post)
                    await self._fill_post_scf(post["id"], acf_payload)
                    if fetch_status.get("mood_tags") is None:
                        scf_pending_tags.append("mood_tags")
                except Exception as scf_error:
                    logger.error(f"SCF auto-fill failed for post {post['id']}: {scf_error}")
                    scf_pending_tags.append("scf_error")

            try:
                await self.refresh_post_cache(force=True)
            except Exception as e:
                logger.error(f"Post cache refresh failed after publish: {e}")

            return PublishResult(
                post=post,
                scf_pending_tags=scf_pending_tags,
                listen_count=listen_count,
            )

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

    async def update_post_scf(self, post_id: int, partial_acf: Dict[str, Any]) -> Dict[str, Any]:
        """Patch a WordPress post's SCF ``acf`` block with the supplied fields."""
        post = await self.wordpress.update_post(post_id, {"acf": partial_acf})
        logger.info("Updated SCF metadata for post %s: %s", post_id, sorted(partial_acf.keys()))
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
        await self.db.save_service_state(
            POST_CACHE_LAST_SYNCED_AT_KEY,
            datetime.now().isoformat(timespec="seconds"),
        )

    async def _count_listen_index(self, release: Release) -> int:
        """Return the listen-count to write to SCF (matches + 1 for the new post)."""
        from utils import normalize_artist_list

        title = release.normalized_title
        artists = set(normalize_artist_list([a.name for a in release.artists]))
        posts = await self.db.get_wordpress_posts()
        matches = sum(
            1 for post in posts
            if post.normalized_title == title
            and set(post.normalized_artists) == artists
        )
        return matches + 1

    async def _build_scf_payload(
        self,
        release: Release,
        listen_count: int,
        post: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Optional[list]]]:
        """Build the SCF ``acf`` block and report which fields could not be filled."""
        countable_tracks = [t for t in release.tracks if t.is_countable]
        length_ms = sum(t.duration_ms for t in countable_tracks)
        total_tracks = len(countable_tracks)
        avg_track_ms = length_ms // total_tracks if total_tracks else 0
        any_explicit = any(t.explicit for t in countable_tracks)

        first_artist = release.artists[0].name if release.artists else ""
        try:
            album_info = await self.lastfm.album_getinfo(first_artist, release.title)
        except Exception as error:
            logger.warning("Last.fm lookup failed for %s - %s: %s", first_artist, release.title, error)
            album_info = {}

        mood_tags = pick_mood_tags(album_info)
        fetch_status: Dict[str, Optional[list]] = {"mood_tags": mood_tags or None}

        track_rows = [
            {
                "disc_number": t.disc_number,
                "track_number": t.track_number,
                "title": t.title,
                "duration_ms": t.duration_ms,
                "spotify_id": t.spotify_id,
                "highlight": t.highlight,
                "explicit": t.explicit,
            }
            for t in countable_tracks
        ]

        acf = {
            "music_tracks": track_rows,
            "music_length_ms": length_ms,
            "spotify_album_id": release.spotify_id,
            "spotify_album_url": f"https://open.spotify.com/album/{release.spotify_id}",
            "music_release_date": _format_scf_date(_coerce_spotify_release_date(release.release_date)),
            "music_listened_at": _format_scf_date(post.get("date")),
            "lastfm_release_id": album_info.get("mbid", "") if isinstance(album_info, dict) else "",
            "music_total_tracks": total_tracks,
            "music_avg_track_ms": avg_track_ms,
            "music_explicit": any_explicit,
            "music_mood_tags": [{"mood": tag} for tag in mood_tags],
            "listen-count": listen_count,
            "music_rating": release.rating if release.rating is not None else "",
            "music_favorite": release.favorite,
            "music_notes": release.notes or "",
            "unreleased": release.unreleased,
        }
        return acf, fetch_status

    async def _fill_post_scf(self, post_id: int, acf_payload: Dict[str, Any]) -> None:
        """PATCH a WordPress post with the SCF ``acf`` block."""
        await self.wordpress.update_post(post_id, {"acf": acf_payload})
        logger.info("Filled SCF metadata for post %s", post_id)
