"""Tracker adapter for the shared album metadata engine."""

import asyncio
from typing import Any

from album_metadata.enrichment import enrich_known
from album_metadata.lastfm import LastFM
from album_metadata.spotify import Spotify
from models import Release


class MetadataEnrichmentError(RuntimeError):
    """The shared engine could not safely produce a WordPress update."""


class TrackerMetadataAdapter:
    """Adapt a tracked Spotify release to the provider-agnostic metadata engine."""

    def __init__(self, config: Any, spotify: Any = None, lastfm: Any = None):
        self.spotify = spotify or Spotify(
            config.spotify_client_id, config.spotify_client_secret)
        self.lastfm = lastfm or LastFM(config.lastfm_api_key)

    async def build_patch(
        self,
        release: Release,
        post: dict,
        tag_ids: list[int],
        category_ids: list[int],
        listen_count: int,
    ) -> dict:
        return await asyncio.to_thread(
            self._build_patch, release, post, tag_ids, category_ids, listen_count)

    def _build_patch(
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

        album = self.spotify.album(release.spotify_id)
        tracks = self.spotify.all_tracks(release.spotify_id)
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

        patch = enrich_known(
            source,
            self.spotify,
            self.lastfm,
            album,
            tracks,
            [artist.name for artist in release.artists],
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

    @staticmethod
    def editor_acf(release: Release) -> dict:
        """Values owned by the pre-publication editor, not metadata providers."""
        return {
            "music_rating": release.rating if release.rating is not None else "",
            "music_favorite": release.favorite,
            "music_notes": release.notes or "",
        }
