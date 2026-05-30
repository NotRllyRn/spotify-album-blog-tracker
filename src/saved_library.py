"""
Saved Spotify library synchronization.
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from models import ReleaseType, SavedLibraryAlbum, SavedLibrarySyncResult, WordPressPost
from utils import compute_release_type, normalize_artist_list, normalize_text

if TYPE_CHECKING:
    from database import Database
    from spotify_client import SpotifyClient

logger = logging.getLogger(__name__)

SAVED_LIBRARY_TOTAL_KEY = "spotify_saved_library.total"
SAVED_LIBRARY_FIRST_PAGE_HASH_KEY = "spotify_saved_library.first_page_hash"
SAVED_LIBRARY_LAST_SYNCED_AT_KEY = "spotify_saved_library.last_synced_at"
SAVED_LIBRARY_INCLUDED_TYPES = {ReleaseType.ALBUM, ReleaseType.EP}


class SavedLibraryService:
    """Keeps a local mirror of saved Spotify library albums."""

    def __init__(self, db: "Database", spotify: "SpotifyClient"):
        self.db = db
        self.spotify = spotify

    async def sync(self, force: bool = False) -> SavedLibrarySyncResult:
        """Synchronize saved Spotify albums into SQLite."""
        logger.info("Synchronizing saved Spotify library albums...")

        previous_total = None if force else await self.db.get_service_state(SAVED_LIBRARY_TOTAL_KEY)
        previous_hash = None if force else await self.db.get_service_state(SAVED_LIBRARY_FIRST_PAGE_HASH_KEY)

        first_page = await self.spotify.get_saved_albums_page(limit=50, offset=0)
        current_total = str(first_page.get("total", 0))
        first_page_hash = self.compute_first_page_hash(first_page)

        if not force and previous_total == current_total and previous_hash == first_page_hash:
            message = "Saved Spotify library is current; total and first-page hash both matched."
            logger.info(message)
            return SavedLibrarySyncResult(
                skipped=True,
                total_seen=int(first_page.get("total", 0) or 0),
                stored_total=(await self.db.get_saved_library_stats()).total,
                message=message,
            )

        all_items = await self.spotify.get_all_saved_albums(first_page=first_page)
        existing_by_id = await self.db.get_saved_library_albums_by_id()
        wordpress_posts = await self.db.get_wordpress_posts()

        incoming_albums = []
        for item in all_items:
            saved_album = await self._build_saved_album(item, existing_by_id, wordpress_posts)
            if saved_album is not None:
                incoming_albums.append(saved_album)

        incoming_ids = {album.spotify_id for album in incoming_albums}
        existing_ids = set(existing_by_id)
        removed_ids = sorted(existing_ids - incoming_ids)

        removed_count = await self.db.delete_saved_library_albums(removed_ids)

        for album in incoming_albums:
            await self.db.upsert_saved_library_album(album)

        await self.db.save_service_state(SAVED_LIBRARY_TOTAL_KEY, current_total)
        await self.db.save_service_state(SAVED_LIBRARY_FIRST_PAGE_HASH_KEY, first_page_hash)
        await self.db.save_service_state(SAVED_LIBRARY_LAST_SYNCED_AT_KEY, datetime.now().isoformat())

        message = (
            f"Synchronized saved Spotify library: {len(incoming_albums)} stored, "
            f"{removed_count} removed."
        )
        logger.info(message)
        return SavedLibrarySyncResult(
            skipped=False,
            total_seen=int(first_page.get("total", 0) or 0),
            stored_total=len(incoming_albums),
            added_or_updated=len(incoming_albums),
            removed=removed_count,
            message=message,
        )

    def compute_first_page_hash(self, page: Dict[str, Any]) -> str:
        """Hash stable first-page identity fields for change detection."""
        stable_items = []
        for item in page.get("items", []):
            album = item.get("album") or {}
            stable_items.append({
                "spotify_id": album.get("id", ""),
                "added_at": item.get("added_at", ""),
            })

        payload = json.dumps(stable_items, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    async def _build_saved_album(
        self,
        item: Dict[str, Any],
        existing_by_id: Dict[str, SavedLibraryAlbum],
        wordpress_posts: List[WordPressPost],
    ) -> Optional[SavedLibraryAlbum]:
        album = item.get("album") or {}
        spotify_id = album.get("id")
        if not spotify_id:
            return None

        album_type = album.get("album_type", "")
        if album_type != "album":
            return None

        release_type = await self._get_release_type(item, existing_by_id)
        if release_type not in SAVED_LIBRARY_INCLUDED_TYPES:
            return None

        artist_names = [
            artist.get("name", "")
            for artist in album.get("artists", [])
            if artist.get("name")
        ]
        normalized_artists = normalize_artist_list(artist_names)
        title = album.get("name", "")
        normalized_title = normalize_text(title)
        wordpress_post = self._find_matching_wordpress_post(
            normalized_title,
            normalized_artists,
            wordpress_posts,
        )

        return SavedLibraryAlbum(
            spotify_id=spotify_id,
            spotify_uri=album.get("uri") or f"spotify:album:{spotify_id}",
            spotify_url=(album.get("external_urls") or {}).get("spotify", ""),
            title=title,
            normalized_title=normalized_title,
            artists=artist_names,
            normalized_artists=normalized_artists,
            album_type=album_type,
            release_type=release_type,
            cover_url=self._get_cover_url(album),
            added_at=self._parse_spotify_datetime(item.get("added_at")),
            is_posted_listened=wordpress_post is not None,
            wordpress_post_id=wordpress_post.id if wordpress_post else None,
        )

    async def _get_release_type(
        self,
        item: Dict[str, Any],
        existing_by_id: Dict[str, SavedLibraryAlbum],
    ) -> ReleaseType:
        album = item["album"]
        spotify_id = album["id"]
        existing = existing_by_id.get(spotify_id)
        if existing and existing.album_type == album.get("album_type"):
            return existing.release_type

        album_type = album.get("album_type", "")
        total_tracks = self._parse_total_tracks(album)
        if total_tracks is not None and total_tracks >= 7:
            return ReleaseType.ALBUM

        tracks = self._get_complete_embedded_tracks(album, total_tracks)
        if tracks is not None:
            return ReleaseType(compute_release_type(tracks, album_type))

        logger.info(
            "Fetching full tracks for saved library album %s because embedded track data was incomplete.",
            spotify_id,
        )
        tracks = await self.spotify.get_album_tracks(spotify_id)
        return ReleaseType(compute_release_type(tracks, album_type))

    def _parse_total_tracks(self, album: Dict[str, Any]) -> Optional[int]:
        total_tracks = album.get("total_tracks")
        if isinstance(total_tracks, int):
            return total_tracks

        try:
            return int(total_tracks)
        except (TypeError, ValueError):
            return None

    def _get_complete_embedded_tracks(
        self,
        album: Dict[str, Any],
        total_tracks: Optional[int],
    ) -> Optional[List[Dict[str, Any]]]:
        tracks = (album.get("tracks") or {}).get("items") or []
        if not tracks:
            return None

        if total_tracks is None:
            return tracks

        if len(tracks) >= total_tracks:
            return tracks[:total_tracks]

        return None

    def _find_matching_wordpress_post(
        self,
        normalized_title: str,
        normalized_artists: List[str],
        wordpress_posts: List[WordPressPost],
    ) -> Optional[WordPressPost]:
        artist_set = set(normalized_artists)
        for post in wordpress_posts:
            if post.normalized_title == normalized_title and set(post.normalized_artists) == artist_set:
                return post
        return None

    def _get_cover_url(self, album: Dict[str, Any]) -> str:
        images = album.get("images") or []
        return images[0].get("url", "") if images else ""

    def _parse_spotify_datetime(self, value: Optional[str]) -> datetime:
        if not value:
            return datetime.now()
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
