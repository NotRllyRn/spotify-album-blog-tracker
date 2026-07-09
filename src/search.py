"""
Fuzzy search over cached or live WordPress posts.

Public seam: ``rank_matches(posts, query, threshold)`` returns a ranked
list of ``SearchMatch`` (capped at ``RESULT_CAP``). Used by both the
cache path (slice 1), the live WP path (slice 3), and the picker UI
later in the chain.

Token semantics: every query token must score at least ``threshold``
against the post haystack (title + artists). Substring matches pass
automatically; otherwise ``rapidfuzz.fuzz.WRatio`` is used and mapped
to ``[0.0, 1.0]``. Tiebreakers: highest score first, newest WP ID first.
"""

from dataclasses import dataclass
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
    # rapidfuzz partial_ratio finds the best substring alignment within
    # ``hay``. Better than vanilla ratio or WRatio when the haystack has
    # padding characters (en-dashes, extra spaces) that confuse WRatio's
    # combined heuristic on short vs long inputs.
    return fuzz.partial_ratio(token, hay) / 100.0


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
    """Shape we depend on from WordPressClient; defined to keep tests fast."""

    async def get_posts(self, **params: Any) -> Any: ...


def _wp_post_to_match(payload: dict) -> SearchMatch:
    """Convert one raw WP REST post payload to a SearchMatch-shaped record."""
    title_obj = payload.get("title") or {}
    title = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj)
    # Live WP search doesn't usually include artists as a flat array;
    # the haystack falls back to title-only for live posts. Tags are not
    # available from WP's `search` query, so artists stays empty.
    return SearchMatch(
        post_id=int(payload["id"]),
        title=title,
        artists=[],
        link=str(payload.get("link", "") or ""),
        score=0.0,
    )


async def search_live(
    wordpress_client: WordPressClientLike,
    query: str,
    threshold: float = FUZZY_BASE_THRESHOLD,
) -> List[SearchMatch]:
    """Hit ``GET /wp/v2/posts?search=…&per_page=100`` and rank via the cache ladder."""
    result = await wordpress_client.get_posts(search=query, per_page=100)
    raw_posts = list(getattr(result, "posts", None) or result or [])
    # Live WP returns dicts; convert to lightweight objects so rank_matches works.
    proxies = [_RawPostProxy(p) for p in raw_posts]
    return rank_matches(proxies, query, threshold)


class _RawPostProxy:
    """Minimal duck-typed post used by the live path; lets rank_matches reuse _haystack."""

    __slots__ = ("id", "title", "artists", "link")

    def __init__(self, payload: dict):
        title_obj = payload.get("title") or {}
        self.title = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj)
        self.id = int(payload["id"])
        self.artists: List[str] = []
        self.link = str(payload.get("link", "") or "")
