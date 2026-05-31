"""
Saved Spotify library synchronization.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from models import (
    ReleaseType,
    SavedLibraryAlbum,
    SavedLibrarySnapshotItem,
    SavedLibrarySyncResult,
    WordPressPost,
)
from utils import compute_release_type, normalize_artist_list, normalize_text

if TYPE_CHECKING:
    from database import Database
    from spotify_client import SpotifyClient

logger = logging.getLogger(__name__)

SAVED_LIBRARY_TOTAL_KEY = "spotify_saved_library.total"
SAVED_LIBRARY_FIRST_PAGE_HASH_KEY = "spotify_saved_library.first_page_hash"
SAVED_LIBRARY_LAST_SYNCED_AT_KEY = "spotify_saved_library.last_synced_at"
SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY = "spotify_saved_library.last_full_audit_at"
SAVED_LIBRARY_INCLUDED_TYPES = {ReleaseType.ALBUM, ReleaseType.EP}
SAVED_LIBRARY_PAGE_LIMIT = 50
SAVED_LIBRARY_FULL_AUDIT_INTERVAL = timedelta(days=7)
SAVED_LIBRARY_MAX_INCREMENTAL_REMOVALS = 10
SAVED_LIBRARY_MAX_INCREMENTAL_HEAD_PAGES = 10


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
        last_full_audit = None if force else await self.db.get_service_state(SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY)

        first_page = await self.spotify.get_saved_albums_page(limit=SAVED_LIBRARY_PAGE_LIMIT, offset=0)
        current_total_value = int(first_page.get("total", 0) or 0)
        current_total = str(current_total_value)
        first_page_hash = self.compute_first_page_hash(first_page)
        snapshot_items = await self.db.get_saved_library_snapshot_items()
        previous_total_value = self._parse_state_int(previous_total)
        snapshot_total_mismatch = (
            previous_total_value is not None
            and previous_total_value != len(snapshot_items)
        )
        needs_full_scan = (
            force
            or previous_total is None
            or previous_hash is None
            or snapshot_total_mismatch
            or (current_total_value > 0 and not snapshot_items)
            or self._full_audit_due(last_full_audit)
        )

        if (
            not needs_full_scan
            and previous_total == current_total
            and previous_hash == first_page_hash
        ):
            message = "Saved Spotify library is current; total and first-page hash both matched."
            logger.info(message)
            return SavedLibrarySyncResult(
                skipped=True,
                total_seen=current_total_value,
                stored_total=(await self.db.get_saved_library_stats()).total,
                message=message,
            )

        if needs_full_scan:
            reason = "force" if force else "missing validation state or due audit"
            return await self._run_full_reconcile(first_page, current_total, first_page_hash, reason=reason)

        existing_by_id = await self.db.get_saved_library_albums_by_id()
        incremental_result = await self._run_incremental_reconcile(
            first_page=first_page,
            current_total=current_total_value,
            first_page_hash=first_page_hash,
            snapshot_items=snapshot_items,
            existing_by_id=existing_by_id,
        )
        if incremental_result is not None:
            return incremental_result

        return await self._run_full_reconcile(
            first_page,
            current_total,
            first_page_hash,
            reason="incremental reconciliation was ambiguous",
        )

    async def _run_full_reconcile(
        self,
        first_page: Dict[str, Any],
        current_total: str,
        first_page_hash: str,
        reason: str,
    ) -> SavedLibrarySyncResult:
        """Fetch every saved-albums page and rebuild both local saved-library views."""
        logger.info("Running full saved-library reconciliation: %s", reason)

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

        snapshot_items = [
            item
            for item in (
                self._build_snapshot_item(item, position)
                for position, item in enumerate(all_items)
            )
            if item is not None
        ]
        await self.db.replace_saved_library_snapshot(snapshot_items)
        await self._save_validation_state(current_total, first_page_hash, full_audit=True)

        message = (
            f"Synchronized saved Spotify library with full scan: {len(incoming_albums)} stored, "
            f"{removed_count} removed, {len(snapshot_items)} snapshot items."
        )
        logger.info(message)
        return SavedLibrarySyncResult(
            skipped=False,
            total_seen=int(current_total),
            stored_total=len(incoming_albums),
            added_or_updated=len(incoming_albums),
            removed=removed_count,
            message=message,
        )

    async def _run_incremental_reconcile(
        self,
        first_page: Dict[str, Any],
        current_total: int,
        first_page_hash: str,
        snapshot_items: List[SavedLibrarySnapshotItem],
        existing_by_id: Dict[str, SavedLibraryAlbum],
    ) -> Optional[SavedLibrarySyncResult]:
        """Reconcile obvious head additions and sparse removals without full pagination."""
        old_order = [item.spotify_id for item in snapshot_items]
        old_by_id = {item.spotify_id: item for item in snapshot_items}
        old_total = len(old_order)
        page_cache = {0: first_page}

        if current_total < old_total:
            removals_needed = old_total - current_total
            if removals_needed > SAVED_LIBRARY_MAX_INCREMENTAL_REMOVALS:
                logger.info(
                    "Saved-library removal count %s is above incremental limit; falling back to full scan.",
                    removals_needed,
                )
                return None

            removal_result = await self._remove_missing_ids_with_probes(
                old_order,
                current_total,
                removals_needed,
                page_cache,
            )
            if removal_result is None:
                return None
            removed_ids, new_order = removal_result
            return await self._apply_incremental_changes(
                current_total=current_total,
                first_page_hash=first_page_hash,
                addition_items=[],
                removed_ids=removed_ids,
                new_order=new_order,
                old_by_id=old_by_id,
                existing_by_id=existing_by_id,
                reason="incremental removals",
            )

        head_items = await self._fetch_head_until_known_album(first_page, old_by_id, current_total, page_cache)
        boundary_index = self._find_first_known_album_index(head_items, old_by_id)
        if boundary_index is None:
            logger.info("Could not find a saved-library boundary item near the head; falling back to full scan.")
            return None

        addition_items = head_items[:boundary_index]
        if not addition_items:
            logger.info("Saved-library change was not a simple head addition or sparse removal.")
            return None

        addition_ids = [self._item_album_id(item) for item in addition_items]
        if any(album_id is None for album_id in addition_ids):
            return None

        candidate_order = [album_id for album_id in addition_ids if album_id is not None] + old_order
        head_ids = [album_id for album_id in (self._item_album_id(item) for item in head_items) if album_id]
        if head_ids != candidate_order[:len(head_ids)]:
            logger.info("Saved-library head page did not align with local snapshot; falling back to full scan.")
            return None

        removals_needed = len(candidate_order) - current_total
        if removals_needed < 0:
            logger.info("Saved-library incremental candidate had too few items; falling back to full scan.")
            return None
        if removals_needed > SAVED_LIBRARY_MAX_INCREMENTAL_REMOVALS:
            logger.info(
                "Saved-library mixed change needs %s removals, above incremental limit; falling back to full scan.",
                removals_needed,
            )
            return None

        removed_ids: List[str] = []
        new_order = candidate_order
        if removals_needed:
            removal_result = await self._remove_missing_ids_with_probes(
                candidate_order,
                current_total,
                removals_needed,
                page_cache,
            )
            if removal_result is None:
                return None
            removed_ids, new_order = removal_result

        return await self._apply_incremental_changes(
            current_total=current_total,
            first_page_hash=first_page_hash,
            addition_items=addition_items,
            removed_ids=removed_ids,
            new_order=new_order,
            old_by_id=old_by_id,
            existing_by_id=existing_by_id,
            reason="incremental additions" if not removed_ids else "incremental additions and removals",
        )

    async def _apply_incremental_changes(
        self,
        current_total: int,
        first_page_hash: str,
        addition_items: List[Dict[str, Any]],
        removed_ids: List[str],
        new_order: List[str],
        old_by_id: Dict[str, SavedLibrarySnapshotItem],
        existing_by_id: Dict[str, SavedLibraryAlbum],
        reason: str,
    ) -> Optional[SavedLibrarySyncResult]:
        """Persist an incremental saved-library reconciliation."""
        snapshot_items = self._rebuild_snapshot_items(new_order, old_by_id, addition_items)
        if snapshot_items is None:
            logger.info("Saved-library snapshot rebuild failed; falling back to full scan.")
            return None

        removed_count = await self.db.delete_saved_library_albums(sorted(removed_ids))
        added_or_updated = 0
        if addition_items:
            wordpress_posts = await self.db.get_wordpress_posts()
            for item in addition_items:
                saved_album = await self._build_saved_album(item, existing_by_id, wordpress_posts)
                if saved_album is not None:
                    await self.db.upsert_saved_library_album(saved_album)
                    added_or_updated += 1

        await self.db.replace_saved_library_snapshot(snapshot_items)
        await self._save_validation_state(str(current_total), first_page_hash, full_audit=False)

        stats = await self.db.get_saved_library_stats()
        message = (
            f"Synchronized saved Spotify library with {reason}: "
            f"{added_or_updated} added/updated, {removed_count} removed, "
            f"{len(snapshot_items)} snapshot items."
        )
        logger.info(message)
        return SavedLibrarySyncResult(
            skipped=False,
            total_seen=current_total,
            stored_total=stats.total,
            added_or_updated=added_or_updated,
            removed=removed_count,
            message=message,
        )

    async def _fetch_head_until_known_album(
        self,
        first_page: Dict[str, Any],
        old_by_id: Dict[str, SavedLibrarySnapshotItem],
        current_total: int,
        page_cache: Dict[int, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Fetch head pages until a previous snapshot item appears."""
        items = list(first_page.get("items", []))
        if self._find_first_known_album_index(items, old_by_id) is not None:
            return items

        next_url = first_page.get("next")
        pages_read = 1
        while next_url and len(items) < current_total and pages_read < SAVED_LIBRARY_MAX_INCREMENTAL_HEAD_PAGES:
            offset = pages_read * SAVED_LIBRARY_PAGE_LIMIT
            page = await self._get_saved_albums_page(offset, page_cache)
            items.extend(page.get("items", []))
            pages_read += 1
            if self._find_first_known_album_index(items, old_by_id) is not None:
                return items
            next_url = page.get("next")

        return items

    async def _remove_missing_ids_with_probes(
        self,
        candidate_order: List[str],
        current_total: int,
        removals_needed: int,
        page_cache: Dict[int, Dict[str, Any]],
    ) -> Optional[Tuple[List[str], List[str]]]:
        """Find sparse removals by binary-searching changed Spotify pages."""
        removed_ids: List[str] = []
        new_order = list(candidate_order)
        for _ in range(removals_needed):
            removed_id = await self._find_first_missing_id(new_order, current_total, page_cache)
            if not removed_id:
                logger.info("Could not locate removed saved-library album with page probes.")
                return None
            removed_ids.append(removed_id)
            new_order.remove(removed_id)
        return removed_ids, new_order

    async def _find_first_missing_id(
        self,
        local_order: List[str],
        current_total: int,
        page_cache: Dict[int, Dict[str, Any]],
    ) -> Optional[str]:
        """Return the first local snapshot ID missing from Spotify's current order."""
        if current_total == 0:
            return local_order[0] if local_order else None

        page_count = (current_total + SAVED_LIBRARY_PAGE_LIMIT - 1) // SAVED_LIBRARY_PAGE_LIMIT
        low = 0
        high = page_count - 1
        changed_page: Optional[int] = None

        while low <= high:
            mid = (low + high) // 2
            offset = mid * SAVED_LIBRARY_PAGE_LIMIT
            page = await self._get_saved_albums_page(offset, page_cache)
            current_ids = self._page_album_ids(page)
            if self._page_matches_local_order(current_ids, local_order, offset, current_total):
                low = mid + 1
            else:
                changed_page = mid
                high = mid - 1

        if changed_page is None:
            return local_order[current_total] if len(local_order) > current_total else None

        offset = changed_page * SAVED_LIBRARY_PAGE_LIMIT
        page = await self._get_saved_albums_page(offset, page_cache)
        current_ids = self._page_album_ids(page)
        local_window = local_order[offset:offset + len(current_ids) + 1]
        current_id_set = set(current_ids)
        missing_ids = [spotify_id for spotify_id in local_window if spotify_id not in current_id_set]
        return missing_ids[0] if len(missing_ids) == 1 else None

    async def _get_saved_albums_page(
        self,
        offset: int,
        page_cache: Dict[int, Dict[str, Any]],
    ) -> Dict[str, Any]:
        if offset not in page_cache:
            page_cache[offset] = await self.spotify.get_saved_albums_page(
                limit=SAVED_LIBRARY_PAGE_LIMIT,
                offset=offset,
            )
        return page_cache[offset]

    def _page_matches_local_order(
        self,
        current_ids: List[str],
        local_order: List[str],
        offset: int,
        current_total: int,
    ) -> bool:
        local_slice = local_order[offset:offset + len(current_ids)]
        if current_ids != local_slice:
            return False
        is_last_current_page = offset + len(current_ids) == current_total
        if is_last_current_page and len(local_order) != current_total:
            return False
        return True

    def _rebuild_snapshot_items(
        self,
        new_order: List[str],
        old_by_id: Dict[str, SavedLibrarySnapshotItem],
        addition_items: List[Dict[str, Any]],
    ) -> Optional[List[SavedLibrarySnapshotItem]]:
        now = datetime.now()
        additions_by_id: Dict[str, Dict[str, Any]] = {}
        for item in addition_items:
            album_id = self._item_album_id(item)
            if album_id:
                additions_by_id[album_id] = item

        snapshot_items: List[SavedLibrarySnapshotItem] = []
        for position, spotify_id in enumerate(new_order):
            if spotify_id in additions_by_id:
                snapshot_item = self._build_snapshot_item(additions_by_id[spotify_id], position)
                if snapshot_item is None:
                    return None
                snapshot_items.append(snapshot_item)
                continue

            old_item = old_by_id.get(spotify_id)
            if old_item is None:
                return None
            snapshot_items.append(SavedLibrarySnapshotItem(
                spotify_id=old_item.spotify_id,
                spotify_uri=old_item.spotify_uri,
                added_at=old_item.added_at,
                position=position,
                last_seen_at=now,
            ))

        return snapshot_items

    def _build_snapshot_item(
        self,
        item: Dict[str, Any],
        position: int,
    ) -> Optional[SavedLibrarySnapshotItem]:
        album = item.get("album") or {}
        spotify_id = album.get("id")
        if not spotify_id:
            return None
        return SavedLibrarySnapshotItem(
            spotify_id=spotify_id,
            spotify_uri=album.get("uri") or f"spotify:album:{spotify_id}",
            added_at=self._parse_spotify_datetime(item.get("added_at")),
            position=position,
            last_seen_at=datetime.now(),
        )

    def _find_first_known_album_index(
        self,
        items: List[Dict[str, Any]],
        old_by_id: Dict[str, SavedLibrarySnapshotItem],
    ) -> Optional[int]:
        for index, item in enumerate(items):
            album_id = self._item_album_id(item)
            if album_id in old_by_id:
                return index
        return None

    def _page_album_ids(self, page: Dict[str, Any]) -> List[str]:
        return [
            album_id
            for album_id in (self._item_album_id(item) for item in page.get("items", []))
            if album_id
        ]

    def _item_album_id(self, item: Dict[str, Any]) -> Optional[str]:
        return (item.get("album") or {}).get("id")

    async def _save_validation_state(
        self,
        current_total: str,
        first_page_hash: str,
        full_audit: bool,
    ):
        now = datetime.now().isoformat()
        await self.db.save_service_state(SAVED_LIBRARY_TOTAL_KEY, current_total)
        await self.db.save_service_state(SAVED_LIBRARY_FIRST_PAGE_HASH_KEY, first_page_hash)
        await self.db.save_service_state(SAVED_LIBRARY_LAST_SYNCED_AT_KEY, now)
        if full_audit:
            await self.db.save_service_state(SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY, now)

    def _full_audit_due(self, last_full_audit: Optional[str]) -> bool:
        if not last_full_audit:
            return True
        try:
            parsed = datetime.fromisoformat(last_full_audit)
            now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            return now - parsed >= SAVED_LIBRARY_FULL_AUDIT_INTERVAL
        except (TypeError, ValueError):
            return True

    def _parse_state_int(self, value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

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
