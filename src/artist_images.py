"""Shared Spotify artist matching and WordPress image-plan rules."""

import math
import urllib.parse
from collections import OrderedDict
from typing import Any

from album_metadata.common import match_key, similarity
from album_metadata.providers import SpotifyProviderError

ARTIST_IMAGE_FIELD = "image"
ARTIST_IMAGE_MAX_DIMENSION = 256
ARTIST_IMAGE_PLAN_VERSION = 1
ARTIST_MIN_SCORE = 0.90
ARTIST_MAX_TIE_GAP = 0.05
SPOTIFY_IMAGE_HOST = "i.scdn.co"


def _valid_artist(candidate: Any) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("id"), str)
        and bool(candidate["id"])
        and isinstance(candidate.get("name"), str)
        and bool(candidate["name"])
        and isinstance(candidate.get("images"), list)
    )


def validate_spotify_artist(candidate: Any, operation: str = "artist.search") -> dict:
    """Reject malformed Spotify artist objects before matching or image use."""
    if not _valid_artist(candidate):
        raise SpotifyProviderError(
            f"Spotify {operation} response was malformed.", operation=operation)
    for image in candidate["images"]:
        if (
            not isinstance(image, dict)
            or not isinstance(image.get("url"), str)
            or not image["url"]
            or image.get("width") is not None
            and (type(image["width"]) is not int or image["width"] <= 0)
            or image.get("height") is not None
            and (type(image["height"]) is not int or image["height"] <= 0)
        ):
            raise SpotifyProviderError(
                f"Spotify {operation} response was malformed.", operation=operation)
    return candidate


def artist_candidate_score(candidate: dict, artist_name: str) -> dict:
    return {
        "score": similarity(artist_name, candidate.get("name", "")),
        "candidate": candidate,
    }


def choose_artist_candidate(candidates: list[dict], artist_name: str) -> dict:
    """Choose one artist conservatively, preserving ambiguous names for review."""
    scored = [artist_candidate_score(candidate, artist_name) for candidate in candidates]
    passing = [row for row in scored if row["score"] >= ARTIST_MIN_SCORE]
    passing.sort(key=lambda row: (-row["score"], row["candidate"]["id"]))
    if not passing:
        return {
            "candidate": None,
            "reason": "spotify_artist_no_results" if not candidates
            else "spotify_artist_low_confidence",
            "scores": scored,
        }

    exact = [row for row in passing if match_key(row["candidate"]["name"]) == match_key(artist_name)]
    contenders = exact or [
        row for row in passing
        if passing[0]["score"] - row["score"] < ARTIST_MAX_TIE_GAP
    ]
    if len(contenders) == 1:
        evidence = "exact_name" if exact else "unique_similarity"
        return {**contenders[0], "reason": "spotify_artist_match", "selection_evidence": evidence}

    popular = [
        row for row in contenders
        if type(row["candidate"].get("popularity")) is int
        and 0 <= row["candidate"]["popularity"] <= 100
    ]
    if len(popular) == len(contenders):
        top_popularity = max(row["candidate"]["popularity"] for row in popular)
        winners = [
            row for row in popular
            if row["candidate"]["popularity"] == top_popularity
        ]
        if len(winners) == 1:
            return {
                **winners[0],
                "reason": "spotify_artist_match",
                "selection_evidence": "unique_popularity",
            }
    return {
        "candidate": None,
        "reason": "spotify_artist_ambiguous",
        "scores": passing,
    }


def artist_search_ladder(spotify: Any, artist_name: str) -> list[dict]:
    """Search quoted then free-form, deduplicating like album discovery."""
    seen: "OrderedDict[str, dict]" = OrderedDict()
    for query in (f'artist:"{artist_name}"', artist_name):
        if not query.strip():
            continue
        for candidate in spotify.search_artists(query, limit=10):
            candidate_id = candidate.get("id")
            if candidate_id:
                seen.setdefault(candidate_id, candidate)
        found = list(seen.values())
        if choose_artist_candidate(found, artist_name).get("candidate"):
            return found
    return list(seen.values())


def select_artist_image(
    artist: dict, max_dimension: int = ARTIST_IMAGE_MAX_DIMENSION
) -> dict | None:
    """Return the largest native Spotify image within the requested bound."""
    eligible = [
        image for image in artist.get("images", [])
        if type(image.get("width")) is int
        and type(image.get("height")) is int
        and 0 < image["width"] <= max_dimension
        and 0 < image["height"] <= max_dimension
        and is_spotify_image_url(image.get("url"))
    ]
    return max(
        eligible,
        key=lambda image: (max(image["width"], image["height"]), image["width"] * image["height"]),
        default=None,
    )


def is_spotify_image_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname == SPOTIFY_IMAGE_HOST


def image_is_missing(term: dict) -> bool:
    acf = term.get("acf")
    return not bool(acf.get(ARTIST_IMAGE_FIELD)) if isinstance(acf, dict) else True


def artist_image_entry(term_id: int, term_name: str, match: dict) -> dict | None:
    candidate = match.get("candidate")
    if not isinstance(candidate, dict):
        return None
    image = select_artist_image(candidate)
    if image is None:
        return None
    return {
        "term_id": term_id,
        "term_name": term_name,
        "spotify": {
            "id": candidate["id"],
            "name": candidate["name"],
            "score": match["score"],
            "selection_evidence": match["selection_evidence"],
        },
        "image": {
            "url": image["url"],
            "width": image["width"],
            "height": image["height"],
        },
    }


def correction_row(term: dict, reason: str, candidates: list[dict]) -> dict:
    messages = {
        "spotify_artist_no_results": "Spotify returned no artist candidates.",
        "spotify_artist_low_confidence": "No Spotify artist candidate met the confidence threshold.",
        "spotify_artist_ambiguous": "Multiple Spotify artists remained equally plausible.",
        "spotify_artist_image_missing": "The matched Spotify artist has no native image at or below 256px.",
    }
    ranked = sorted(
        (artist_candidate_score(candidate, term["name"]) for candidate in candidates),
        key=lambda row: (-row["score"], row["candidate"]["id"]),
    )
    return {
        "term_id": term["id"],
        "term_name": term["name"],
        "diagnostic": {"code": reason, "message": messages[reason]},
        "candidates": [
            {
                "spotify_id": row["candidate"]["id"],
                "name": row["candidate"]["name"],
                "score": row["score"],
                "popularity": row["candidate"].get("popularity"),
            }
            for row in ranked[:5]
        ],
    }


def validate_artist_image_plan(value: Any) -> dict:
    """Validate a saved plan completely before any network write."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "generated_at", "max_dimension", "images"
    }:
        raise ValueError("Invalid artist image plan root")
    if value["schema_version"] != ARTIST_IMAGE_PLAN_VERSION:
        raise ValueError("Unsupported artist image plan schema version")
    if not isinstance(value["generated_at"], str) or not value["generated_at"]:
        raise ValueError("Invalid artist image plan generated_at")
    if value["max_dimension"] != ARTIST_IMAGE_MAX_DIMENSION:
        raise ValueError("Invalid artist image plan max_dimension")
    if not isinstance(value["images"], list):
        raise ValueError("Invalid artist image plan images")
    seen: set[int] = set()
    for entry in value["images"]:
        if not isinstance(entry, dict) or set(entry) != {
            "term_id", "term_name", "spotify", "image"
        }:
            raise ValueError("Invalid artist image plan entry")
        term_id = entry["term_id"]
        if type(term_id) is not int or term_id <= 0 or term_id in seen:
            raise ValueError("Artist image term IDs must be unique positive integers")
        seen.add(term_id)
        if not isinstance(entry["term_name"], str) or not entry["term_name"].strip():
            raise ValueError("Invalid artist image term name")
        spotify = entry["spotify"]
        if not isinstance(spotify, dict) or set(spotify) != {
            "id", "name", "score", "selection_evidence"
        }:
            raise ValueError("Invalid artist image Spotify match")
        if not all(isinstance(spotify[key], str) and spotify[key] for key in (
            "id", "name", "selection_evidence"
        )):
            raise ValueError("Invalid artist image Spotify identity")
        if len(spotify["id"]) != 22 or not spotify["id"].isalnum():
            raise ValueError("Invalid Spotify artist ID")
        if type(spotify["score"]) not in (int, float) or not math.isfinite(spotify["score"]) \
                or not 0 <= spotify["score"] <= 1:
            raise ValueError("Invalid artist image Spotify score")
        image = entry["image"]
        if not isinstance(image, dict) or set(image) != {"url", "width", "height"}:
            raise ValueError("Invalid artist image selection")
        if not is_spotify_image_url(image["url"]):
            raise ValueError("Artist image URL must use Spotify's image host")
        if any(type(image[key]) is not int or not 0 < image[key] <= ARTIST_IMAGE_MAX_DIMENSION
               for key in ("width", "height")):
            raise ValueError("Artist image dimensions exceed the configured maximum")
    return value
