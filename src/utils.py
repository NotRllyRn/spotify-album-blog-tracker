"""
Utility functions.
"""

import unicodedata
import re
from typing import List
from logging import getLogger

from album_metadata.common import compute_release_type  # shared metadata policy

logger = getLogger(__name__)

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

