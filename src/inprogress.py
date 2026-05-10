"""
Pure helpers for /inprogress pagination.
"""

from dataclasses import dataclass
from typing import List, Optional

from models import Release, Track


INPROGRESS_PAGE_SIZE = 9


@dataclass(frozen=True)
class InProgressPage:
    featured: Release
    items: List[Release]
    page: int
    total_pages: int
    total_releases: int


def build_inprogress_page(
    releases: List[Release],
    page: int,
    page_size: int = INPROGRESS_PAGE_SIZE
) -> Optional[InProgressPage]:
    """Build pinned-feature pagination data for /inprogress."""
    if not releases:
        return None

    ordered = sorted(releases, key=lambda release: release.last_seen, reverse=True)
    featured = ordered[0]
    non_featured = [release for release in ordered[1:] if release.spotify_id != featured.spotify_id]
    total_pages = max(1, (len(non_featured) + page_size - 1) // page_size)
    clamped_page = min(max(page, 0), total_pages - 1)
    start = clamped_page * page_size
    end = start + page_size

    return InProgressPage(
        featured=featured,
        items=non_featured[start:end],
        page=clamped_page,
        total_pages=total_pages,
        total_releases=len(ordered)
    )


def get_next_unlistened_track(release: Release) -> Optional[Track]:
    """Return the next unlistened countable track in stored album order."""
    for track in release.tracks:
        if track.is_countable and not track.listened:
            return track
    return None
