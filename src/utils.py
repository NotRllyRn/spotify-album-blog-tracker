"""
Utility functions.
"""

import unicodedata
import re
from typing import List
from logging import getLogger
from urllib.parse import unquote, urlencode, urlparse, urlunparse

from album_metadata.common import compute_release_type  # shared metadata policy

logger = getLogger(__name__)


def public_album_link(
    wordpress_link: str,
    public_url: str = "https://music.callita.day",
) -> str:
    """Convert a WordPress post URL to the frontend's album-slug URL."""
    try:
        slug = unquote(urlparse(wordpress_link).path.rstrip("/").rsplit("/", 1)[-1])
        base = urlparse(public_url.rstrip("/"))
        return urlunparse((
            base.scheme, base.netloc, "/", "", urlencode({"album": slug}), ""
        )) if slug else wordpress_link
    except Exception:
        return wordpress_link

def normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    # Unicode NFKC normalize
    text = unicodedata.normalize('NFKC', text)
    # Casefold
    text = text.casefold()
    # Trim outer whitespace
    text = text.strip()
    # Collapse repeated internal whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove zero-width characters
    text = re.sub(r'[\u200B-\u200D\uFEFF]', '', text)
    return text

def normalize_artist_name(name: str) -> str:
    """Normalize artist name, stripping commas first."""
    name = name.replace(',', '')
    return normalize_text(name)

def normalize_artist_list(artists: List[str]) -> List[str]:
    """Normalize list of artist names."""
    return [normalize_artist_name(name) for name in artists]
