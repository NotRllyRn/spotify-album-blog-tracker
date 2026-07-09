"""
Fuzzy search over cached or live WordPress posts.

Public seam: ``search_for_posts(db, wordpress_client, query, *,
threshold, force_source) -> SearchOutcome``. The Discord bot calls this
to build the picker; the picker View itself stays a thin wrapper.

Token semantics: every query token must score at least ``threshold``
against the post haystack (title + artists, normalised). Substring
matches pass automatically; otherwise ``rapidfuzz.fuzz.WRatio`` is
used and mapped to ``[0.0, 1.0]``. Tiebreakers: highest score first,
newest WP ID first. Capped at ``RESULT_CAP``.

Cache freshness: when the local ``wordpress_post_cache`` table is
empty or older than ``WORDPRESS_CACHE_MAX_AGE_HOURS`` (read from
``service_state[LAST_SYNCED_AT_KEY]``), ``search_for_posts`` falls
back to a live ``GET /wp/v2/posts?search=…`` and reports the swap via
``SearchOutcome.fell_back_to_live``. The operator's Discord pick may
point at a fresh live result that's not in ``wordpress_post_cache``
yet — that's intentional.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional, Protocol

from rapidfuzz import fuzz

from utils import normalize_artist_name, normalize_text


# --- Constants --------------------------------------------------------------

FUZZY_BASE_THRESHOLD: float = 0.55
FUZZY_LOOSE_THRESHOLD: float = 0.30
RESULT_CAP: int = 9
LAST_SYNCED_AT_KEY: str = "wordpress_post_cache.last_synced_at"
WORDPRESS_CACHE_MAX_AGE_HOURS: int = 24


# --- Data classes -----------------------------------------------------------


@dataclass(frozen=True)
class SearchMatch:
    post_id: int
    title: str
    artists: List[str]
    link: str
    score: float


@dataclass(frozen=True)
class SearchOutcome:
    matches: List[SearchMatch]
    source: str  # "cache" | "live"
    fell_back_to_live: bool


# --- Helpers ----------------------------------------------------------------


def _normalize_query(query: str) -> List[str]:
    """Tokenize, normalize per-token, dedupe. Empty input → []."""
    raw = (query or "").strip()
    if not raw:
        return []
    seen: set = set()
    out: List[str] = []
    for tok in (normalize_text(t) for t in raw.split()):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _haystack(post: Any) -> str:
    """title + " " + " ".join(artists), all normalized."""
    title = normalize_text(getattr(post, "title", "") or "")
    artists = " ".join(normalize_artist_name(a) for a in (getattr(post, "artists", None) or []))
    return f"{title} {artists}".strip()


def _score_token(token: str, hay: str) -> float:
    if not hay:
        return 0.0
    if token in hay:
        return 1.0
    return fuzz.WRatio(token, hay) / 100.0


async def _is_cache_stale(db: Any, now: datetime) -> bool:
    """Cache is stale when empty or older than ``WORDPRESS_CACHE_MAX_AGE_HOURS``."""
    raw = await db.get_service_state(LAST_SYNCED_AT_KEY)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return (now - last) > timedelta(hours=WORDPRESS_CACHE_MAX_AGE_HOURS)


# --- Public API -------------------------------------------------------------


def rank_matches(
    posts: List[Any],
    query: str,
    threshold: float = FUZZY_BASE_THRESHOLD,
) -> List[SearchMatch]:
    """AND-token score every post; rank; cap. Pure function."""
    tokens = _normalize_query(query)
    if not tokens:
        return []

    matches: List[SearchMatch] = []
    for post in posts:
        hay = _haystack(post)
        if not hay:
            continue
        scores = [_score_token(t, hay) for t in tokens]
        if scores and all(s >= threshold for s in scores):
            matches.append(
                SearchMatch(
                    post_id=int(post.id),
                    title=post.title,
                    artists=list(getattr(post, "artists", []) or []),
                    link=getattr(post, "link", "") or "",
                    score=max(scores),
                )
            )

    matches.sort(key=lambda m: (-m.score, -m.post_id))
    return matches[:RESULT_CAP]


# --- Live search adapter ----------------------------------------------------


class WordPressClientLike(Protocol):
    """Shape we depend on from WordPressClient."""

    async def get_posts(self, **params: Any) -> Any: ...


class _RawPostProxy:
    """Minimal duck-typed post used by the live path; lets rank_matches reuse _haystack."""

    __slots__ = ("id", "title", "artists", "link")

    def __init__(self, payload: dict):
        title_obj = payload.get("title") or {}
        self.title = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj)
        self.id = int(payload["id"])
        self.artists: List[str] = []
        self.link = str(payload.get("link", "") or "")


async def search_live(
    wordpress_client: WordPressClientLike,
    query: str,
    threshold: float = FUZZY_BASE_THRESHOLD,
) -> List[SearchMatch]:
    """Hit ``GET /wp/v2/posts?search=…&per_page=100`` and rank via the same ladder."""
    result = await wordpress_client.get_posts(search=query, per_page=100)
    raw_posts = list(getattr(result, "posts", None) or result or [])
    return rank_matches([_RawPostProxy(p) for p in raw_posts], query, threshold)


# --- Single deep-module seam ------------------------------------------------


async def search_for_posts(
    db: Any,
    wordpress_client: WordPressClientLike,
    query: str,
    *,
    threshold: float = FUZZY_BASE_THRESHOLD,
    force_source: Optional[str] = None,
) -> SearchOutcome:
    """Cache-first fuzzy search with live-WP fallback. The single public entry point."""
    if not _normalize_query(query):
        return SearchOutcome(matches=[], source="cache", fell_back_to_live=False)

    cache_posts = await db.get_wordpress_posts()
    cache_empty = not cache_posts
    cache_stale = cache_empty or await _is_cache_stale(db, datetime.now())

    if force_source != "live" and not cache_empty and not cache_stale:
        matches = rank_matches(cache_posts, query, threshold)
        return SearchOutcome(matches=matches, source="cache", fell_back_to_live=False)

    matches = await search_live(wordpress_client, query, threshold)
    fell_back = force_source != "live"
    return SearchOutcome(matches=matches, source="live", fell_back_to_live=fell_back)
