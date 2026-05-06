"""
Utility functions.
"""

import unicodedata
import re
from typing import List

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

def compute_release_type(tracks: List[dict], raw_spotify_type: str) -> str:
    """Compute release type from tracks, matching plugin logic."""
    if raw_spotify_type.lower() == 'compilation':
        return 'Compilation'

    track_count = len(tracks)
    total_ms = sum(t.get('duration_ms', 0) for t in tracks)
    max_track_ms = max((t.get('duration_ms', 0) for t in tracks), default=0)

    duration_30m = 1800000  # 30 minutes
    duration_10m = 600000   # 10 minutes

    if track_count >= 7 or total_ms >= duration_30m:
        return 'Album'
    elif (4 <= track_count <= 6 and total_ms < duration_30m) or \
         (1 <= track_count <= 3 and max_track_ms >= duration_10m):
        return 'EP'
    elif 1 <= track_count <= 3 and total_ms < duration_30m and max_track_ms < duration_10m:
        return 'Single'
    else:
        return 'Album'  # fallback