"""Tracker adapter for the shared album metadata engine."""

from datetime import datetime
from typing import Any

from album_metadata.enrichment import build_known_album_patch
from album_metadata_cache import resolved_metadata_from_cache, spotify_evidence_from_release
from models import Release


class MetadataEnrichmentError(RuntimeError):
    """The shared engine could not safely produce a WordPress update."""


class TrackerMetadataAdapter:
    """Adapt a tracked Spotify release to the provider-agnostic metadata engine."""

    def __init__(self, metadata_cache: Any):
        self.metadata_cache = metadata_cache

    async def build_patch(
        self,
        release: Release,
        post: dict,
        tag_ids: list[int],
        category_ids: list[int],
        listen_count: int,
    ) -> dict:
        post_date = post.get("date")
        if not isinstance(post_date, str) or not post_date:
            raise MetadataEnrichmentError("WordPress did not return the new post date.")

        cached = await self.metadata_cache.ensure_for_release(release)
        if cached.status != "ready":
            raise MetadataEnrichmentError(
                f"Album metadata is unresolved ({cached.diagnostic_code or 'unknown'}).")
        album, tracks = spotify_evidence_from_release(release)
        source = {
            "id": post["id"],
            "title": {"rendered": release.title},
            "date": post_date,
            "tags": list(tag_ids),
            "categories": list(category_ids),
            "artist": post.get("artist", []),
            "genre": post.get("genre", []),
            "release_type": post.get("release_type", []),
            "acf": post.get("acf") if isinstance(post.get("acf"), dict) else {},
        }
        if "modified" in post:
            source["modified"] = post["modified"]

        patch = build_known_album_patch(
            source,
            album,
            tracks,
            [artist.name for artist in release.artists],
            resolved_metadata_from_cache(cached, album),
            listen_count=listen_count,
            track_highlights={
                track.spotify_id: track.highlight
                for track in release.tracks if track.is_countable
            },
        )
        if not patch or "write" not in patch:
            diagnostics = (patch or {}).get("diagnostics") or []
            message = diagnostics[0].get("message") if diagnostics else "No metadata update was produced."
            raise MetadataEnrichmentError(message)
        return patch

    async def build_create_patch(
        self,
        release: Release,
        tag_ids: list[int],
        category_ids: list[int],
        listen_count: int,
        post_date: datetime,
    ) -> dict:
        """Build the initial post write without requiring a persisted post."""
        return await self.build_patch(release, {
            "id": 1,
            "title": {"rendered": release.title},
            "date": post_date.isoformat(timespec="seconds"),
            "tags": list(tag_ids),
            "categories": list(category_ids),
            "artist": [],
            "genre": [],
            "release_type": [],
            "acf": {},
        }, tag_ids, category_ids, listen_count)

    @staticmethod
    def editor_acf(release: Release) -> dict:
        """Values owned by the pre-publication editor, not metadata providers."""
        return {
            "music_rating": release.rating if release.rating is not None else "",
            "music_favorite": release.favorite,
            "music_notes": release.notes or "",
        }
