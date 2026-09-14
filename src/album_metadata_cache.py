"""Persistent static album metadata preparation and reuse."""

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from album_metadata.enrichment import (
    MetadataResolutionFailure,
    ResolvedAlbumMetadata,
    resolve_known_album_metadata,
)
from album_metadata.lastfm import LastFM
from album_metadata.spotify import Spotify
from models import CachedAlbumMetadata, Release

logger = logging.getLogger(__name__)

METADATA_RESOLVER_VERSION = 1
LASTFM_REQUEST_INTERVAL = 0.25
_PROVIDER_FAILURE_CODES = {"spotify_provider_error", "lastfm_provider_error"}


class MetadataCacheProviderError(RuntimeError):
    """A transient provider failure that must not become a terminal cache row."""


def spotify_evidence_from_release(release: Release) -> tuple[dict, list[dict]]:
    """Adapt persisted tracker state to the shared canonical Spotify evidence shape."""
    tracks = [
        {
            "id": track.spotify_id,
            "name": track.title,
            "duration_ms": track.duration_ms,
            "disc_number": track.disc_number,
            "track_number": track.track_number,
            "explicit": track.explicit,
            "is_playable": track.is_countable,
        }
        for track in release.tracks
    ]
    album = {
        "id": release.spotify_id,
        "name": release.title,
        "artists": [
            {"id": artist.spotify_id, "name": artist.name}
            for artist in release.artists
        ],
        "album_type": release.raw_spotify_type,
        "release_date": release.release_date,
        "total_tracks": len(tracks),
        "external_urls": {
            "spotify": f"https://open.spotify.com/album/{release.spotify_id}"},
    }
    return album, tracks


def resolved_metadata_from_cache(
    metadata: CachedAlbumMetadata, album: dict
) -> ResolvedAlbumMetadata:
    """Rehydrate the static resolution needed by the pure WordPress patch builder."""
    evidence: dict[str, Any] = {
        "title": album["name"],
        "artist": (album.get("artists") or [{}])[0].get("name", ""),
        "score": 1.0,
    }
    if metadata.lastfm_url:
        evidence["url"] = metadata.lastfm_url
    if metadata.lastfm_mbid:
        evidence["mbid"] = metadata.lastfm_mbid
    return ResolvedAlbumMetadata(
        lastfm_url=metadata.lastfm_url,
        lastfm_mbid=metadata.lastfm_mbid,
        genres=list(metadata.genres),
        lastfm_listeners=metadata.lastfm_listeners,
        lastfm_evidence=evidence,
    )


class AlbumMetadataCacheService:
    """Serialize provider work and persist each completed album independently."""

    def __init__(
        self,
        config: Any,
        db: Any,
        provider_factory: Optional[Callable[[], tuple[Any, Any]]] = None,
    ):
        self.config = config
        self.db = db
        self._provider_factory = provider_factory or self._make_providers
        self._resolver_lock = asyncio.Lock()
        self._backfill_task: Optional[asyncio.Task] = None
        self._foreground_tasks: set[asyncio.Task] = set()

    def _make_providers(self) -> tuple[Spotify, LastFM]:
        return (
            Spotify(self.config.spotify_client_id, self.config.spotify_client_secret),
            LastFM(self.config.lastfm_api_key, request_interval=LASTFM_REQUEST_INTERVAL),
        )

    async def get(self, spotify_id: str) -> Optional[CachedAlbumMetadata]:
        return await self.db.get_album_metadata_cache(
            spotify_id, METADATA_RESOLVER_VERSION)

    async def ensure_for_release(self, release: Release) -> CachedAlbumMetadata:
        album, tracks = spotify_evidence_from_release(release)
        return await self._ensure(release.spotify_id, album, tracks, self._provider_factory())

    async def ensure_spotify_id(
        self, spotify_id: str, providers: Optional[tuple[Any, Any]] = None
    ) -> CachedAlbumMetadata:
        return await self._ensure(
            spotify_id, None, None, providers or self._provider_factory())

    async def _ensure(
        self,
        spotify_id: str,
        album: Optional[dict],
        tracks: Optional[list[dict]],
        providers: tuple[Any, Any],
    ) -> CachedAlbumMetadata:
        cached = await self.get(spotify_id)
        if cached:
            return cached
        async with self._resolver_lock:
            cached = await self.get(spotify_id)
            if cached:
                return cached
            spotify, lastfm = providers
            try:
                resolved = await asyncio.to_thread(
                    self._resolve, spotify_id, spotify, lastfm, album, tracks)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                raise MetadataCacheProviderError(str(exc)) from exc
            if (isinstance(resolved, MetadataResolutionFailure) and
                    resolved.code in _PROVIDER_FAILURE_CODES):
                raise MetadataCacheProviderError(resolved.message)
            metadata = self._to_cache(spotify_id, resolved)
            await self.db.save_album_metadata_cache(metadata)
            logger.info(
                "Metadata cached: spotify_id=%s status=%s listeners=%s",
                spotify_id, metadata.status, metadata.lastfm_listeners)
            return metadata

    @staticmethod
    def _resolve(
        spotify_id: str,
        spotify: Any,
        lastfm: Any,
        album: Optional[dict],
        tracks: Optional[list[dict]],
    ) -> ResolvedAlbumMetadata | MetadataResolutionFailure:
        if album is None or tracks is None:
            album, tracks = spotify.album_with_tracks(spotify_id)
        return resolve_known_album_metadata(spotify, lastfm, album, tracks)

    @staticmethod
    def _to_cache(
        spotify_id: str,
        resolved: ResolvedAlbumMetadata | MetadataResolutionFailure,
    ) -> CachedAlbumMetadata:
        if isinstance(resolved, MetadataResolutionFailure):
            return CachedAlbumMetadata(
                spotify_id, "unresolved", None, None, [], None, resolved.code,
                METADATA_RESOLVER_VERSION, datetime.now())
        return CachedAlbumMetadata(
            spotify_id, "ready", resolved.lastfm_url, resolved.lastfm_mbid,
            list(resolved.genres), resolved.lastfm_listeners, None,
            METADATA_RESOLVER_VERSION, datetime.now())

    def schedule_for_release(self, release: Release) -> None:
        """Schedule foreground preparation without delaying the playback loop."""
        task = asyncio.create_task(self.ensure_for_release(release))
        self._foreground_tasks.add(task)
        task.add_done_callback(self._foreground_done)

    def _foreground_done(self, task: asyncio.Task) -> None:
        self._foreground_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.warning("Foreground metadata preparation failed: %s", error)

    def trigger_saved_library_backfill(self) -> None:
        """Start one resumable background pass unless one is already running."""
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.create_task(self._run_saved_library_backfill())

    async def _run_saved_library_backfill(self) -> None:
        spotify_ids = await self.db.get_unposted_saved_library_ids_missing_metadata(
            METADATA_RESOLVER_VERSION)
        logger.info("Metadata backfill started: %s missing unposted saved releases", len(spotify_ids))
        providers = self._provider_factory()
        ready = unresolved = 0
        for spotify_id in spotify_ids:
            try:
                metadata = await self.ensure_spotify_id(spotify_id, providers)
            except MetadataCacheProviderError as exc:
                logger.warning("Metadata backfill aborted at %s: %s", spotify_id, exc)
                break
            ready += metadata.status == "ready"
            unresolved += metadata.status == "unresolved"
            await asyncio.sleep(0)
        logger.info("Metadata backfill completed: ready=%s unresolved=%s", ready, unresolved)

    async def stop(self) -> None:
        tasks = set(self._foreground_tasks)
        if self._backfill_task and not self._backfill_task.done():
            tasks.add(self._backfill_task)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
