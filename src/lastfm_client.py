"""
Async Last.fm read-only client for mood tags and MusicBrainz release IDs.
"""

import logging
import re
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

# Blocklist copied from Wordpress-PostToAlbum-Script (lines 261-313) so the
# Discord flow surfaces the same tag set the complement script would.
LFM_TAG_BLOCKLIST: tuple[str, ...] = (
    r"^\d{4}$",
    r"^aoty$",
    r"^best of \d{4}$",
    r"^seen live$",
    r"^favorites?$",
    r"^under \d+$",
)
_LFM_BLOCKLIST_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in LFM_TAG_BLOCKLIST)


class LastFMClient:
    """Minimal Last.fm client. Tolerates being constructed without an API key."""

    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self.client.aclose()

    async def album_getinfo(self, artist: str, album: str) -> dict:
        """Return ``{"mbid": str, "tags": [{"name": str}, ...]}`` or ``{}`` on failure."""
        if not self.api_key or not artist or not album:
            return {}

        try:
            response = await self.client.get(
                LASTFM_API_URL,
                params={
                    "method": "album.getinfo",
                    "artist": artist,
                    "album": album,
                    "api_key": self.api_key,
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload = response.json().get("album", {}) or {}
        except Exception as error:
            logger.warning("Last.fm album.getinfo failed for %s - %s: %s", artist, album, error)
            return {}

        return {
            "mbid": (payload.get("mbid") or "").strip(),
            "tags": [
                tag for tag in (payload.get("tags", {}) or {}).get("tag", []) or []
                if isinstance(tag, dict) and tag.get("name")
            ],
        }


def pick_mood_tags(album_info: dict, max_n: int = 3) -> List[str]:
    """Apply the same blocklist as the complement script and keep the top N tags.

    Accepts either ``album_info["tags"]`` as a list of dicts (``{"name": str}``)
    or pre-flattened name strings.
    """
    raw_tags = album_info.get("tags", []) if isinstance(album_info, dict) else []
    names: List[str] = []
    for tag in raw_tags:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if not name:
            continue
        cleaned = str(name).strip()
        if not cleaned or any(pattern.match(cleaned) for pattern in _LFM_BLOCKLIST_RE):
            continue
        names.append(cleaned)
        if len(names) >= max_n:
            break
    return names
