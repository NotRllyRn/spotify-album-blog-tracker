#!/usr/bin/env python3
"""post_to_album — verbose Python CLI that backfills SCF metadata + taxonomies
on every WordPress post, sourcing data from Spotify (album + tracks) and
Last.fm (genre/mood tags only).

Stdlib only. Single file. See plan.md for the locked-in design.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import html
import json
import logging
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

log = logging.getLogger("post_to_album")

from album_metadata.common import (
    _norm_artist,
    _norm_title,
    compute_release_type,
    match_key,
    now_iso as _now_iso,
    post_dmy as _post_dmy,
    raw_query,
    safe_error as _safe_error,
    similarity,
    write_json_atomic,
)
from album_metadata.schema import (
    APPROVED_ACF_TYPES,
    AUTO_FILLABLE_FIELDS,
    CATEGORY_MAP,
    DIAGNOSTIC_CODES,
    EDITOR_OWNED_ACF_FIELDS,
    LFM_BLOCKLIST,
    PLAN_SCHEMA_VERSION,
    PROVIDER_FAILURE_KINDS,
    RELEASE_TYPES,
    TAG_CACHE_MAX_AGE,
    TAG_CACHE_PATH,
    TAXONOMIES,
    TRACK_KEYS,
    UNRESOLVED_SCHEMA_VERSION,
    WRITE_FILL_ONLY,
    WRITE_OVERWRITE_MANAGED,
)

def expected_release_type(post: dict, release_type_terms: dict[str, int] | None = None) -> str | None:
    """Return one consistent recognized type from legacy and taxonomy evidence."""
    category_types = {name for name, term_id in CATEGORY_MAP.items()
                      if term_id in (post.get("categories") or [])}
    assigned = set(post.get("release_type") or [])
    term_types = {term_id: next((canonical for canonical in RELEASE_TYPES
                                if match_key(name) == match_key(canonical)), None)
                  for name, term_id in (release_type_terms or {}).items()}
    taxonomy_types = {term_types.get(term_id) for term_id in assigned}
    if release_type_terms is not None and (len(assigned) > 1 or None in taxonomy_types):
        return None
    taxonomy_types.discard(None)
    evidence = category_types, taxonomy_types
    combined = category_types | taxonomy_types
    return next(iter(combined)) if len(combined) == 1 and all(len(values) <= 1 for values in evidence) else None


def spotify_release_type_compatible(candidate: dict, expected: str | None) -> bool:
    """Recognize only positive release-type evidence available in search rows."""
    raw_type, total = candidate.get("album_type"), candidate.get("total_tracks")
    if not isinstance(raw_type, str) or not isinstance(total, int) or isinstance(total, bool):
        return False
    raw_type = match_key(raw_type)
    return ((expected == "Album" and raw_type == "album" and total >= 7) or
            (expected == "Single" and raw_type == "single" and 1 <= total <= 3) or
            (expected == "EP" and raw_type in {"album", "single"} and 4 <= total <= 6) or
            (expected == "Compilation" and raw_type == "compilation"))


# --------------------------------------------------------------------------- #
# SPOTIFY  —  Client Credentials Flow
# --------------------------------------------------------------------------- #

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API       = "https://api.spotify.com/v1"


class ProviderError(RuntimeError):
    """A safe, structured provider failure suitable for unresolved diagnostics."""

    def __init__(self, provider: str, operation: str, failure_kind: str,
                 message: str, *, retryable: bool, attempts: int,
                 http_status: int | None = None,
                 circuit_state: str = "closed"):
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.failure_kind = failure_kind
        self.retryable = retryable
        self.attempts = attempts
        self.http_status = http_status
        self.circuit_state = circuit_state

    def diagnostic(self, code: str) -> dict:
        details = {
            "provider": self.provider,
            "operation": self.operation,
            "failure_kind": self.failure_kind,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "circuit_state": self.circuit_state,
        }
        if self.http_status is not None:
            details["http_status"] = self.http_status
        return {"code": code, "message": _safe_error(self), "details": details}


class SpotifyProviderError(ProviderError):
    def __init__(self, message: str, *, operation: str = "response",
                 failure_kind: str = "malformed_response", retryable: bool = False,
                 attempts: int = 1, http_status: int | None = None,
                 circuit_state: str = "closed"):
        super().__init__("spotify", operation, failure_kind, message,
                         retryable=retryable, attempts=attempts,
                         http_status=http_status, circuit_state=circuit_state)


class LastFMProviderError(ProviderError):
    def __init__(self, message: str, *, operation: str = "response",
                 failure_kind: str = "malformed_response", retryable: bool = False,
                 attempts: int = 1, http_status: int | None = None,
                 circuit_state: str = "closed"):
        super().__init__("lastfm", operation, failure_kind, message,
                         retryable=retryable, attempts=attempts,
                         http_status=http_status, circuit_state=circuit_state)


class ProviderCircuit:
    """Stop a finite batch from hammering one persistently failing provider."""

    def __init__(self, provider: str, threshold: int = 3):
        self.provider = provider
        self.threshold = threshold
        self.consecutive_failures = 0
        self.is_open = False
        self.blocked_until = 0.0
        self.request_counts: dict[str, int] = {}

    def before_request(self, operation: str) -> None:
        if self.is_open:
            cls = SpotifyProviderError if self.provider == "spotify" else LastFMProviderError
            raise cls(f"{self.provider.title()} {operation} skipped: provider circuit is open.",
                      operation=operation, failure_kind="circuit_open", retryable=True,
                      attempts=0, circuit_state="open")

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_transient_failure(self) -> None:
        self.consecutive_failures += 1
        self.is_open = self.is_open or self.consecutive_failures >= self.threshold

    def record_request(self, operation: str) -> None:
        self.request_counts[operation] = self.request_counts.get(operation, 0) + 1

    def block_for(self, seconds: int) -> None:
        self.is_open = True
        self.blocked_until = time.time() + seconds


def _retry_delay(exc: urllib.error.HTTPError, fallback: int) -> int:
    if exc.code != 429:
        return fallback
    try:
        value = exc.headers.get("Retry-After", fallback) if exc.headers else fallback
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _http_error_reason(exc: urllib.error.HTTPError) -> str | None:
    try:
        payload = json.loads(exc.read())
        reason = payload.get("error", {}).get("reason")
        return reason if isinstance(reason, str) else None
    except (AttributeError, OSError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _request_json(req: urllib.request.Request, *, provider: str,
                  operation: str, circuit: ProviderCircuit,
                  timeout: int = 30, max_attempts: int = 3) -> dict:
    """Read one JSON object with bounded retries and safe error classification."""
    circuit.before_request(operation)
    error_cls = SpotifyProviderError if provider == "spotify" else LastFMProviderError
    for attempt in range(1, max_attempts + 1):
        try:
            circuit.record_request(operation)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
            circuit.record_success()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise error_cls(
                    f"{provider.title()} {operation} returned malformed JSON.",
                    operation=operation, failure_kind="malformed_response",
                    retryable=False, attempts=attempt) from exc
            if not isinstance(data, dict):
                raise error_cls(
                    f"{provider.title()} {operation} returned a malformed response.",
                    operation=operation, failure_kind="malformed_response",
                    retryable=False, attempts=attempt)
            return data
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            delay = _retry_delay(exc, 2 ** (attempt - 1))
            if (provider == "spotify" and exc.code == 429 and
                    _http_error_reason(exc) == "QUOTA_EXCEEDED"):
                circuit.block_for(delay)
                log.error("%s %s quota exhausted; retry after %ds",
                          provider.title(), operation, delay)
            elif retryable and attempt < max_attempts:
                log.warning("%s %s HTTP %d, retrying in %ds",
                            provider.title(), operation, exc.code, delay)
                time.sleep(delay)
                continue
            if retryable:
                circuit.record_transient_failure()
            state = "open" if circuit.is_open else "closed"
            raise error_cls(
                f"{provider.title()} {operation} failed after {attempt} attempt"
                f"{'s' if attempt != 1 else ''}: HTTP {exc.code}.",
                operation=operation, failure_kind="http_status",
                retryable=retryable, attempts=attempt, http_status=exc.code,
                circuit_state=state) from exc
        except (TimeoutError, socket.timeout) as exc:
            kind, cause = "timeout", exc
        except urllib.error.URLError as exc:
            kind = ("timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout))
                    else "network")
            cause = exc
        except OSError as exc:
            # Some transports raise connection failures directly, including
            # while reading a response, rather than wrapping them in URLError.
            kind, cause = "network", exc
        if attempt < max_attempts:
            delay = 2 ** (attempt - 1)
            log.warning("%s %s %s, retrying in %ds", provider.title(), operation, kind, delay)
            time.sleep(delay)
            continue
        circuit.record_transient_failure()
        state = "open" if circuit.is_open else "closed"
        raise error_cls(
            f"{provider.title()} {operation} failed after {attempt} attempts: {kind} error.",
            operation=operation, failure_kind=kind, retryable=True,
            attempts=attempt, circuit_state=state) from cause
    raise RuntimeError("unreachable")


class Spotify:
    def __init__(self, client_id: str, client_secret: str,
                 circuit: ProviderCircuit | None = None):
        self._id = client_id
        self._secret = client_secret
        self._circuit = circuit or ProviderCircuit("spotify")
        self._tok: str = ""
        self._exp: float = 0.0

    def _ensure_token(self) -> str:
        if self._tok and time.time() < self._exp - 60:
            return self._tok
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            SPOTIFY_TOKEN_URL, data=body, method="POST",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        j = _request_json(req, provider="spotify", operation="token",
                          circuit=self._circuit)
        token = j.get("access_token")
        if not isinstance(token, str) or not token:
            raise SpotifyProviderError(
                "Spotify token response lacked a valid access_token.", operation="token")
        try:
            expires_in = float(j.get("expires_in", 3600))
        except (TypeError, ValueError) as exc:
            raise SpotifyProviderError(
                "Spotify token response had invalid expires_in.", operation="token") from exc
        self._tok = token
        self._exp = time.time() + expires_in
        return self._tok

    def _get(self, url: str, operation: str | None = None) -> Any:
        if operation is None:
            operation = ("album.search" if "/search?" in url else
                         "track.list" if "/tracks?" in url else
                         "album.get" if "/albums/" in url else "request")
        for auth_attempt in (0, 1):
            tok = self._ensure_token()
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            try:
                return _request_json(req, provider="spotify", operation=operation,
                                     circuit=self._circuit)
            except ProviderError as exc:
                if exc.http_status == 401 and auth_attempt == 0:
                    self._exp = 0
                    continue
                raise
        raise RuntimeError("unreachable")

    def search_albums(self, q: str, limit: int = 10) -> list[dict]:
        url = f"{SPOTIFY_API}/search?q={urllib.parse.quote(q)}&type=album&limit={limit}&market=US"
        data = self._get(url)
        albums = data.get("albums")
        if not isinstance(albums, dict) or not isinstance(albums.get("items"), list):
            # A malformed provider payload is not the same as a valid empty result set.
            raise SpotifyProviderError("Spotify search response was malformed")
        items = albums["items"]
        for candidate in items:
            if (not isinstance(candidate, dict) or
                    not isinstance(candidate.get("id"), str) or not candidate["id"] or
                    not isinstance(candidate.get("name"), str) or
                    not isinstance(candidate.get("artists"), list) or
                    any(not isinstance(artist, dict) or
                        not isinstance(artist.get("name"), str)
                        for artist in candidate["artists"])):
                raise SpotifyProviderError("Spotify search candidate was malformed")
        return items

    def album(self, aid: str) -> dict:
        return self._get(f"{SPOTIFY_API}/albums/{urllib.parse.quote(aid)}?market=US")

    def artist(self, aid: str) -> dict:
        data = self._get(
            f"{SPOTIFY_API}/artists/{urllib.parse.quote(aid)}", "artist.get")
        if not isinstance(data.get("genres"), list) or not all(
                isinstance(genre, str) for genre in data["genres"]):
            raise SpotifyProviderError(
                "Spotify artist.get response was malformed.", operation="artist.get")
        return data

    def _track_pages(self, page: Any) -> list[dict]:
        out: list[dict] = []
        while page:
            if (not isinstance(page, dict) or not isinstance(page.get("items"), list) or
                    page.get("next") is not None and not isinstance(page["next"], str)):
                raise SpotifyProviderError(
                    "Spotify track.list response was malformed.", operation="track.list")
            out.extend(page["items"])
            page = self._get(page["next"], "track.list") if page.get("next") else None
        return out

    def album_with_tracks(self, aid: str) -> tuple[dict, list[dict]]:
        album = self.album(aid)
        return album, self._track_pages(album.get("tracks"))

    def all_tracks(self, aid: str) -> list[dict]:
        """Follow tracks.next until exhausted."""
        page = self._get(
            f"{SPOTIFY_API}/albums/{urllib.parse.quote(aid)}/tracks?limit=50&market=US",
            "track.list")
        return self._track_pages(page)


def _spotify_album_and_tracks(spt: Any, aid: str) -> tuple[dict, list[dict]]:
    combined = getattr(spt, "album_with_tracks", None)
    if callable(combined):
        return cast(tuple[dict, list[dict]], combined(aid))
    album = spt.album(aid)
    return album, spt.all_tracks(aid)


# --------------------------------------------------------------------------- #
# Candidate ranking
# --------------------------------------------------------------------------- #

SPOTIFY_MIN_TITLE = 0.80
SPOTIFY_MIN_ARTIST = 0.70
SPOTIFY_MIN_SCORE = 0.82
SPOTIFY_MAX_TIE_GAP = 0.05


def _release_title_base(value: str) -> str:
    """Remove trailing provider annotations for corroborated discovery only."""
    key = match_key(value)
    previous = None
    while key != previous:
        previous = key
        key = re.sub(r"\s*[\[(][^\[\]()]*[\])]\s*$", "", key).strip()
    return key


def _release_title_similarity(a: str, b: str) -> float:
    score = similarity(a, b)
    base_a, base_b = _release_title_base(a), _release_title_base(b)
    return 1.0 if base_a and base_a == base_b else score


def spotify_candidate_score(cand: dict, q_title: str, q_artists: list[str]) -> dict:
    title_score = _release_title_similarity(q_title, cand.get("name", ""))
    candidate_artists = [a.get("name", "") for a in cand.get("artists", [])]
    # Collaborations make "primary artist only" unsafe: compare every supplied
    # artist with every credited candidate artist and retain the best evidence.
    artist_score = max(
        (similarity(wp_artist, candidate_artist)
         for wp_artist in q_artists for candidate_artist in candidate_artists),
        default=0.0,
    )
    return {"score": 0.65 * title_score + 0.35 * artist_score,
            "title_score": title_score, "artist_score": artist_score,
            "candidate": cand}


def _score(cand: dict, q_title: str, q_artists: list[str]) -> float:
    return spotify_candidate_score(cand, q_title, q_artists)["score"]


def search_ladder(spt: Any, q_title: str, q_artists: list[str],
                  expected_type: str | None = None) -> list[dict]:
    """Search until the strongest available query yields a safe match."""
    quoted = f'album:"{q_title}"' + (f' artist:"{q_artists[0]}"' if q_artists else "")
    free = " ".join([q_title] + q_artists)
    seen: "OrderedDict[str, dict]" = OrderedDict()
    for q in (quoted, free, q_title):
        if not q.strip():
            continue
        # A broad title-only query adds no stronger identity evidence once an
        # artist-aware query has candidates and can fail on very common titles.
        if q == q_title and seen:
            break
        for candidate in spt.search_albums(q, limit=10):
            candidate_id = candidate.get("id")
            if candidate_id:
                seen.setdefault(candidate_id, candidate)
        found = list(seen.values())
        if choose_spotify_candidate(found, q_title, q_artists, expected_type).get("candidate"):
            return found
    return list(seen.values())


def choose_spotify_candidate(spt_results: list[dict], q_title: str,
                             q_artists: list[str], expected_type: str | None = None) -> dict:
    if not q_artists:
        # A good title is not identity evidence for common album names.
        return {"candidate": None, "reason": "spotify_missing_artist"}
    passing = []
    for candidate in spt_results:
        row = spotify_candidate_score(candidate, q_title, q_artists)
        if (row["title_score"] >= SPOTIFY_MIN_TITLE and
                row["artist_score"] >= SPOTIFY_MIN_ARTIST and
                row["score"] >= SPOTIFY_MIN_SCORE):
            passing.append(row)
    passing.sort(key=lambda row: row["score"], reverse=True)
    if not passing:
        return {"candidate": None,
                "reason": "spotify_no_results" if not spt_results else "spotify_low_confidence"}
    exact = [row for row in passing
             if match_key(row["candidate"].get("name", "")) == match_key(q_title)]
    if len(exact) == 1:
        return {**exact[0], "reason": "spotify_match"}
    if len(passing) > 1 and passing[0]["score"] - passing[1]["score"] < SPOTIFY_MAX_TIE_GAP:
        contenders = [row for row in passing
                      if passing[0]["score"] - row["score"] < SPOTIFY_MAX_TIE_GAP]
        compatible = [row for row in contenders
                      if spotify_release_type_compatible(row["candidate"], expected_type)]
        if len(compatible) == 1:
            return {**compatible[0], "reason": "spotify_match"}
        return {"candidate": None, "reason": "spotify_ambiguous",
                "scores": passing[:2], "contenders": contenders}
    return {**passing[0], "reason": "spotify_match"}


def stored_spotify_track_ids(post: dict) -> list[str] | None:
    """Return complete ordered stored Spotify IDs, never partial evidence."""
    rows = (post.get("acf") or {}).get("music_tracks")
    if not isinstance(rows, list) or not rows:
        return None
    ids: list[str] = []
    for row in rows:
        track_id = row.get("spotify_id") if isinstance(row, dict) else None
        if (not isinstance(track_id, str) or len(track_id) != 22 or
                not track_id.isalnum()):
            return None
        ids.append(track_id)
    # A release cannot contain the same Spotify track object twice. Treat such
    # rows as damaged stored evidence rather than letting them pin an album.
    return ids if len(ids) == len(set(ids)) else None


def _is_true(value: Any) -> bool:
    return type(value) is bool and value


def _spotify_tracks_complete(album: dict, tracks: list[dict]) -> bool:
    """Require the provider's paginated rows to match its declared total."""
    total = album.get("total_tracks")
    return (isinstance(total, int) and not isinstance(total, bool) and total > 0 and
            len(tracks) == total)


def validate_spotify_album_tracks(album: Any, tracks: Any) -> None:
    """Reject malformed Spotify evidence before matching or building plan rows."""
    malformed = SpotifyProviderError(
        "Spotify track.list response was malformed.", operation="track.list")
    if (not isinstance(album, dict) or
            not isinstance(album.get("id"), str) or not album["id"] or
            not isinstance(album.get("name"), str) or not album["name"] or
            not isinstance(album.get("artists"), list) or
            any(not isinstance(artist, dict) or
                not isinstance(artist.get("name"), str) or not artist["name"]
                for artist in album["artists"]) or
            not isinstance(album.get("total_tracks"), int) or
            isinstance(album.get("total_tracks"), bool) or album["total_tracks"] <= 0 or
            not isinstance(tracks, list)):
        raise malformed
    if not tracks:
        raise SpotifyProviderError(
            "Spotify track.list returned no tracks.", operation="track.list")
    for track in tracks:
        if (not isinstance(track, dict) or
                not isinstance(track.get("id"), str) or not track["id"] or
                not isinstance(track.get("name"), str) or not track["name"] or
                not isinstance(track.get("duration_ms"), int) or
                isinstance(track.get("duration_ms"), bool) or track["duration_ms"] <= 0 or
                not isinstance(track.get("explicit"), bool) or
                not isinstance(track.get("disc_number"), int) or
                isinstance(track.get("disc_number"), bool) or track["disc_number"] <= 0 or
                not isinstance(track.get("track_number"), int) or
                isinstance(track.get("track_number"), bool) or track["track_number"] <= 0):
            raise malformed


def _spotify_tracks_market_restricted(tracks: Any) -> bool:
    if not isinstance(tracks, list) or not tracks:
        return False
    restricted = []
    for track in tracks:
        if (not isinstance(track, dict) or
                not isinstance(track.get("id"), str) or not track["id"] or
                not isinstance(track.get("explicit"), bool) or
                type(track.get("disc_number")) is not int or track["disc_number"] <= 0 or
                type(track.get("track_number")) is not int or track["track_number"] <= 0):
            return False
        valid_content = (isinstance(track.get("name"), str) and bool(track["name"]) and
                         type(track.get("duration_ms")) is int and track["duration_ms"] > 0)
        market = (isinstance(track.get("restrictions"), dict) and
                  track["restrictions"].get("reason") == "market" and
                  (not track.get("name") or not track.get("duration_ms")))
        if not valid_content and not market:
            return False
        restricted.append(market)
    return any(restricted)


def spotify_full_evidence(spt: Any, candidate: dict) -> dict:
    """Fetch one complete album and its fully paginated ordered track list."""
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise SpotifyProviderError("Spotify candidate ID was malformed")
    album, tracks = _spotify_album_and_tracks(spt, candidate_id)
    validate_spotify_album_tracks(album, tracks)
    # Keep contradictory provider data available for diagnostics, but mark it
    # unusable so it can never manufacture a recovery winner.
    valid = album.get("id") == candidate_id and _spotify_tracks_complete(album, tracks)
    return {"album": album, "tracks": tracks, "valid": valid}


def _spotify_identity_row(candidate: dict, q_title: str, q_artists: list[str]) -> dict | None:
    row = spotify_candidate_score(candidate, q_title, q_artists)
    return row if (row["title_score"] >= SPOTIFY_MIN_TITLE and
                   row["artist_score"] >= SPOTIFY_MIN_ARTIST and
                   row["score"] >= SPOTIFY_MIN_SCORE) else None


def corroborate_existing_spotify(post: dict, search_rows: list[dict], q_title: str,
                                  q_artists: list[str], expected_type: str | None,
                                  evidence: dict) -> dict | None:
    """Accept stored identity only with current discovery, identity, type and tracks."""
    album_id = (post.get("acf") or {}).get("spotify_album_id")
    stored_ids = stored_spotify_track_ids(post)
    if (not isinstance(album_id, str) or len(album_id) != 22 or
            not album_id.isalnum() or stored_ids is None or
            album_id not in {row.get("id") for row in search_rows}):
        return None
    full = evidence.get(album_id)
    if not full:
        return None
    album, tracks = full["album"], full["tracks"]
    row = _spotify_identity_row(album, q_title, q_artists)
    if (not _is_true(full.get("valid")) or len(stored_ids) != album["total_tracks"] or
            row is None or (expected_type and
                            not spotify_release_type_compatible(album, expected_type))):
        return None
    live_ids = [track.get("id") if isinstance(track, dict) else None for track in tracks]
    return row if live_ids == stored_ids else None


def spotify_release_fingerprint(album: dict, tracks: list[dict]) -> tuple | None:
    """Build the strict, complete release fingerprint used only for equivalence."""
    def artists(value: Any) -> tuple | None:
        if not isinstance(value, list) or not value:
            return None
        normalized: list[str] = []
        for item in value:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                return None
            normalized.append(match_key(name))
        return tuple(normalized)

    required = ("name", "album_type", "release_date", "release_date_precision",
                "label", "total_tracks")
    if (any(key not in album for key in required) or not tracks or
            not _spotify_tracks_complete(album, tracks)):
        return None
    album_artists = artists(album.get("artists"))
    if (album_artists is None or
            any(not isinstance(album[key], str) or not album[key]
                for key in required[:-1]) or
            not isinstance(album["total_tracks"], int) or
            isinstance(album["total_tracks"], bool)):
        return None
    track_rows = []
    for track in tracks:
        if not isinstance(track, dict):
            return None
        track_artists = artists(track.get("artists"))
        keys = ("disc_number", "track_number", "name", "duration_ms", "explicit", "is_playable")
        if track_artists is None or any(key not in track for key in keys):
            return None
        if (any(not isinstance(track[key], int) or isinstance(track[key], bool)
                for key in ("disc_number", "track_number", "duration_ms")) or
                not isinstance(track["explicit"], bool) or not isinstance(track["is_playable"], bool)):
            return None
        track_rows.append((track["disc_number"], track["track_number"],
                           match_key(track["name"]), track_artists, track["duration_ms"],
                           track["explicit"], track["is_playable"]))
    return (match_key(album["name"]), album_artists, album["album_type"],
            album["release_date"], album["release_date_precision"], album["label"],
            album["total_tracks"], tuple(track_rows))


def recover_spotify_ambiguity(spt: Any, post: dict, scored_contenders: list[dict],
                               q_title: str, q_artists: list[str],
                               expected_type: str | None) -> dict:
    """Resolve an ambiguity without using provider order or incomplete evidence."""
    contenders = [row["candidate"] for row in scored_contenders]
    by_id = {candidate.get("id"): candidate for candidate in contenders}
    cache: dict[str, dict] = {}
    unavailable_ids: set[str] = set()
    raw_existing_id = (post.get("acf") or {}).get("spotify_album_id")
    existing_id = raw_existing_id if isinstance(raw_existing_id, str) else None
    if existing_id is not None and existing_id in by_id and stored_spotify_track_ids(post) is not None:
        try:
            cache[existing_id] = spotify_full_evidence(spt, by_id[existing_id])
        except ProviderError as exc:
            if exc.http_status != 404:
                raise
            unavailable_ids.add(existing_id)
        corroborated = corroborate_existing_spotify(
            post, contenders, q_title, q_artists, expected_type, cache)
        if corroborated:
            return {**corroborated, "reason": "spotify_match",
                    "selection_evidence": "existing_id_tracks",
                    "full_evidence": cache[existing_id]}

    safe = []
    # Fetch every contender before narrowing: one failure must never manufacture uniqueness.
    for candidate_id in sorted(by_id):
        if candidate_id in unavailable_ids:
            continue
        if candidate_id not in cache:
            try:
                cache[candidate_id] = spotify_full_evidence(spt, by_id[candidate_id])
            except ProviderError as exc:
                if exc.http_status != 404:
                    raise
                unavailable_ids.add(candidate_id)
                continue
        full = cache[candidate_id]
        if not _is_true(full.get("valid")):
            unavailable_ids.add(candidate_id)
            continue
        row = _spotify_identity_row(full["album"], q_title, q_artists)
        if row:
            safe.append({**row, "full_evidence": full})
    # A missing or contradictory contender remains ambiguity evidence. Never
    # allow its omission to make another contender uniquely eligible.
    if unavailable_ids:
        return {"candidate": None, "reason": "spotify_ambiguous"}
    if expected_type:
        compatible = [row for row in safe if spotify_release_type_compatible(
            row["full_evidence"]["album"], expected_type)]
        if not compatible:
            return {"candidate": None, "reason": "spotify_ambiguous"}
        safe = compatible
    eligible = [row for row in safe if row["full_evidence"]["tracks"] and
                _is_true(row["full_evidence"]["album"].get("is_playable")) and
                isinstance(row["full_evidence"]["album"].get("external_urls"), dict) and
                isinstance(row["full_evidence"]["album"]["external_urls"].get("spotify"), str) and
                row["full_evidence"]["album"]["external_urls"]["spotify"].startswith(("http://", "https://"))]
    if len(eligible) == 1:
        return {**eligible[0], "reason": "spotify_match", "selection_evidence": "unique_public"}
    if not eligible:
        return {"candidate": None, "reason": "spotify_ambiguous"}
    best_title = max(row["title_score"] for row in eligible)
    safe = [row for row in eligible if row["title_score"] == best_title]
    if len(safe) == 1:
        return {**safe[0], "reason": "spotify_match", "selection_evidence": "best_title"}
    exact = [row for row in safe if match_key(row["full_evidence"]["album"].get("name", "")) == match_key(q_title)]
    if len(exact) == 1:
        return {**exact[0], "reason": "spotify_match", "selection_evidence": "unique_edition"}
    if exact:
        safe = exact
    popular = [row for row in safe if isinstance(
        row["full_evidence"]["album"].get("popularity"), int) and not isinstance(
        row["full_evidence"]["album"].get("popularity"), bool)]
    if len(popular) == len(safe):
        maximum = max(row["full_evidence"]["album"]["popularity"] for row in popular)
        winners = [row for row in popular if row["full_evidence"]["album"]["popularity"] == maximum]
        if len(winners) == 1:
            return {**winners[0], "reason": "spotify_match",
                    "selection_evidence": "unique_popularity",
                    "selection_popularity": maximum, "contender_count": len(safe)}
    fingerprints = [(spotify_release_fingerprint(row["full_evidence"]["album"],
                                                  row["full_evidence"]["tracks"]), row)
                    for row in safe]
    if fingerprints and fingerprints[0][0] is not None and all(
            fingerprint == fingerprints[0][0] for fingerprint, _ in fingerprints):
        winner = min((row for _, row in fingerprints), key=lambda row: row["candidate"]["id"])
        return {**winner, "reason": "spotify_match", "selection_evidence": "equivalent_id"}
    return {"candidate": None, "reason": "spotify_ambiguous"}


def best_candidate(spt_results: list[dict], q_title: str, q_artists: list[str],
                   expected_type: str | None = None) -> dict | None:
    """Compatibility wrapper returning only the accepted Spotify object."""
    return choose_spotify_candidate(
        spt_results, q_title, q_artists, expected_type).get("candidate")


# --------------------------------------------------------------------------- #
# LAST.FM
# --------------------------------------------------------------------------- #

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


class LastFM:
    def __init__(self, api_key: str, circuit: ProviderCircuit | None = None):
        self._key = api_key
        self._circuit = circuit or ProviderCircuit("lastfm")

    def _get(self, method: str, **params) -> dict:
        params.update({"method": method, "api_key": self._key, "format": "json"})
        req = urllib.request.Request(
            f"{LASTFM_BASE}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "wordpress-album-metadata-filler/1.0"},
        )
        data = _request_json(req, provider="lastfm", operation=method.lower(),
                             circuit=self._circuit)
        if data.get("error") is not None:
            raise LastFMProviderError(
                f"Last.fm {method} returned API error {data['error']}.",
                operation=method, failure_kind="api_error")
        return data

    def album_search(self, album: str, limit: int = 10) -> list[dict]:
        data = self._get("album.search", album=album, limit=limit)
        results = data.get("results")
        if not isinstance(results, dict) or not isinstance(results.get("albummatches"), dict):
            raise RuntimeError("Last.fm malformed album.search response")
        matches = results["albummatches"].get("album", [])
        if not matches:
            return []
        if isinstance(matches, dict):
            return [matches]
        if isinstance(matches, list) and all(isinstance(item, dict) for item in matches):
            return matches
        raise RuntimeError("Last.fm malformed album.search matches")

    def album_getinfo(self, artist: str | None = None, album: str | None = None,
                      mbid: str | None = None, autocorrect: int = 0) -> dict:
        if not mbid and not (artist and album):
            raise ValueError("album_getinfo requires mbid or artist and album")
        params = {"mbid": mbid} if mbid else {
            "artist": artist, "album": album, "autocorrect": autocorrect}
        data = self._get("album.getinfo", **params)
        info = data.get("album")
        if not isinstance(info, dict):
            raise RuntimeError("Last.fm malformed album.getinfo response")
        return info

    def album_gettoptags(self, artist: str | None = None, album: str | None = None,
                         mbid: str | None = None, autocorrect: int = 0) -> dict:
        if not mbid and not (artist and album):
            raise ValueError("album_gettoptags requires mbid or artist and album")
        params = {"mbid": mbid} if mbid else {
            "artist": artist, "album": album, "autocorrect": autocorrect}
        data = self._get("album.getTopTags", **params)
        if not isinstance(data.get("toptags"), dict):
            raise RuntimeError("Last.fm malformed album.getTopTags response")
        return data

    def artist_gettoptags(self, artist: str, autocorrect: int = 0) -> dict:
        if not artist:
            raise ValueError("artist_gettoptags requires artist")
        data = self._get("artist.getTopTags", artist=artist, autocorrect=autocorrect)
        if not isinstance(data.get("toptags"), dict):
            raise RuntimeError("Last.fm malformed artist.getTopTags response")
        return data


LASTFM_MIN_TITLE = 0.85
LASTFM_MIN_ARTIST = 0.75
LASTFM_MIN_SCORE = 0.85
LASTFM_MAX_TIE_GAP = 0.03
LASTFM_MIN_TRACK_SIMILARITY = 0.90
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _is_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _lastfm_search_identity(candidate: dict) -> tuple[str, str, str, str]:
    title = _nonempty_string(candidate.get("name")) or ""
    artist = _nonempty_string(candidate.get("artist")) or ""
    mbid = (_nonempty_string(candidate.get("mbid")) or "").casefold()
    url = _nonempty_string(candidate.get("url")) or ""
    return match_key(title), match_key(artist), mbid, url


def search_lastfm_candidates(lfm: Any, spotify_album: dict, limit: int = 10) -> list[dict]:
    """Search by album and credited artists before the title-only fallback."""
    title = raw_query(spotify_album.get("name", ""))
    queries = [
        f"{title} {artist_name}"
        for artist in spotify_album.get("artists", [])
        if (artist_name := raw_query(artist.get("name", "")))
    ]
    queries.append(title)

    candidates: list[dict] = []
    seen_in_earlier_queries: set[tuple[str, str, str, str]] = set()
    for query in queries:
        found = lfm.album_search(query, limit=limit)
        identities = [_lastfm_search_identity(candidate) for candidate in found]
        # Preserve duplicates returned by one query: they remain valid ambiguity
        # evidence. Only repeated rows from later fallback queries are redundant.
        candidates.extend(candidate for candidate, identity in zip(found, identities)
                          if identity not in seen_in_earlier_queries)
        seen_in_earlier_queries.update(identities)
    return candidates


def resolve_lastfm_mbid(info: dict, selected: dict) -> str | None:
    # Detailed validated identity owns precedence; the accepted search row is fallback.
    for value in (info.get("mbid"), selected.get("mbid")):
        candidate = _nonempty_string(value)
        if candidate and _UUID_RE.fullmatch(candidate):
            return candidate
    return None


def resolve_lastfm_url(info: dict, selected: dict) -> str | None:
    for value in (info.get("url"), selected.get("url")):
        candidate = _nonempty_string(value)
        if candidate and _is_http_url(candidate):
            return candidate
    return None


def _lastfm_artist_score(spotify_album: dict, artist: str) -> float:
    expected = [match_key(a.get("name", "")) for a in spotify_album.get("artists", [])
                if match_key(a.get("name", ""))]
    actual = match_key(artist)
    if len(expected) > 1 and all(name in actual for name in expected):
        return 1.0
    return max((similarity(name, actual) for name in expected), default=0.0)


def lastfm_candidate_score(spotify_album: dict, candidate: dict) -> dict:
    title_score = similarity(spotify_album.get("name", ""), candidate.get("name", ""))
    artist_score = _lastfm_artist_score(spotify_album, candidate.get("artist", ""))
    return {"score": 0.70 * title_score + 0.30 * artist_score,
            "title_score": title_score, "artist_score": artist_score,
            "candidate": candidate}


def choose_lastfm_candidate(spotify_album: dict, candidates: list[dict]) -> dict:
    spotify_artists = spotify_album.get("artists", [])
    if not spotify_artists:
        return {"candidate": None, "reason": "lastfm_missing_artist"}
    exact = [c for c in candidates
             if match_key(c.get("name", "")) == match_key(spotify_album.get("name", ""))
             and any(match_key(c.get("artist", "")) == match_key(a.get("name", ""))
                     for a in spotify_artists)]
    if len(exact) == 1:
        return {**lastfm_candidate_score(spotify_album, exact[0]), "reason": "lastfm_exact"}
    if len(exact) > 1:
        raw_title = unicodedata.normalize(
            "NFC", str(spotify_album.get("name", ""))).casefold().strip()
        raw_artist = unicodedata.normalize(
            "NFC", str((spotify_album.get("artists") or [{}])[0].get("name", ""))
        ).casefold().strip()
        raw_exact = [c for c in exact if unicodedata.normalize(
            "NFC", str(c.get("name", ""))).casefold().strip() == raw_title and
            unicodedata.normalize(
                "NFC", str(c.get("artist", ""))).casefold().strip() == raw_artist]
        if len(raw_exact) == 1:
            return {**lastfm_candidate_score(spotify_album, raw_exact[0]),
                    "reason": "lastfm_exact_punctuation"}
        # Only a single syntactically valid, unique MBID can safely distinguish
        # duplicate exact search rows; result order is not identity evidence.
        usable = [c for c in exact if _UUID_RE.fullmatch(str(c.get("mbid", "")))]
        mbids = [c["mbid"].casefold() for c in usable]
        unique = [c for c in usable if mbids.count(c["mbid"].casefold()) == 1]
        if len(unique) == 1:
            return {**lastfm_candidate_score(spotify_album, unique[0]),
                    "reason": "lastfm_exact_mbid"}
        return {"candidate": None, "reason": "lastfm_ambiguous_exact",
                "contenders": exact}
    passing = []
    for candidate in candidates:
        row = lastfm_candidate_score(spotify_album, candidate)
        if (row["title_score"] >= LASTFM_MIN_TITLE and
                row["artist_score"] >= LASTFM_MIN_ARTIST and
                row["score"] >= LASTFM_MIN_SCORE):
            passing.append(row)
    passing.sort(key=lambda row: row["score"], reverse=True)
    if not passing:
        return {"candidate": None,
                "reason": "lastfm_no_results" if not candidates else "lastfm_low_confidence"}
    if len(passing) > 1 and passing[0]["score"] - passing[1]["score"] < LASTFM_MAX_TIE_GAP:
        return {"candidate": None, "reason": "lastfm_ambiguous", "scores": passing[:2],
                "contenders": [row["candidate"] for row in passing]}
    return {**passing[0], "reason": "lastfm_fuzzy"}


def _track_list(root: Any) -> list[dict]:
    if not root:
        return []
    if not isinstance(root, dict):
        raise RuntimeError("Last.fm malformed tracks collection")
    tracks = root.get("track", [])
    if not tracks:
        return []
    if isinstance(tracks, dict):
        return [tracks]
    if isinstance(tracks, list) and all(isinstance(track, dict) for track in tracks):
        return tracks
    raise RuntimeError("Last.fm malformed track entry")


_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
_JAPANESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]")


def _latin_letters_only(value: str) -> bool:
    """Require a Latin letter and reject letters from every other script."""
    letters = [char for char in value if char.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(char, "") for char in letters)


def _punctuation_key(title: str) -> str:
    key = " ".join(re.sub(r"[^\w\s]|_", "", match_key(title)).split())
    return re.sub(r"\bremastered(?:\s+version)?\b", "remaster", key)


def _strip_track_prefix(title: str) -> str:
    stripped = re.sub(r"^\s*(?:track\s+[a-z]|medley:\s*mode\s+[a-z])\s*[-:]\s*",
                      "", title, flags=re.IGNORECASE)
    return re.split(r"/\s*mode\s+[a-z]\s*[-:]", stripped, maxsplit=1,
                    flags=re.IGNORECASE)[0]


def _quoted_base_match(shorter: str, longer: str) -> bool:
    """Accept only a complete, balanced quoted base plus punctuated annotation."""
    longer = match_key(longer).strip()
    if not longer or longer[0] not in _QUOTE_PAIRS:
        return False
    close = _QUOTE_PAIRS[longer[0]]
    end = longer.find(close, 1)
    if end < 0:
        return False
    base, remainder = longer[1:end], longer[end + 1:]
    return (bool(_punctuation_key(shorter)) and
            _punctuation_key(shorter) == _punctuation_key(base) and
            bool(re.match(r"^\s*[^\w\s]", remainder)))


def _track_similarity(a: str, b: str) -> float:
    """Compare full titles, accepting only tightly bounded provider variants."""
    raw_a, raw_b = (_strip_track_prefix(match_key(title)) for title in (a, b))
    # This check intentionally precedes punctuation removal: equal Morse-like
    # titles are evidence, while different punctuation-only sequences are not.
    if raw_a and raw_a == raw_b:
        return 1.0
    pairs = [(title, _punctuation_key(title)) for title in (raw_a, raw_b)]
    (raw_short, key_short), (raw_long, key_long) = sorted(pairs, key=lambda pair: len(pair[1]))
    if not key_short:
        return 0.0
    if key_short == key_long or _quoted_base_match(raw_short, raw_long):
        return 1.0
    if key_long.startswith(key_short):
        punctuated_suffix = (raw_long.startswith(raw_short) and
                             re.match(r"^\s*[^\w\s]", raw_long[len(raw_short):]) is not None)
        return 1.0 if punctuated_suffix else 0.0
    return difflib.SequenceMatcher(a=key_short, b=key_long).ratio()


def _lastfm_track_keys(info: dict) -> list[str]:
    keys = []
    for track in _track_list(info.get("tracks")):
        track_name = track.get("name") or track.get("title") or ""
        if not isinstance(track_name, str):
            raise RuntimeError("Last.fm malformed track name")
        if match_key(track_name):
            keys.append(match_key(track_name))
    informative = [key for key in keys if not re.fullmatch(r"track\s+\d+", key)]
    return informative or keys


def _track_match_count(spotify_titles: list[str], lastfm_titles: list[str]) -> int:
    """Return maximum deterministic one-to-one matches, preserving duplicates."""
    edges = [[j for j, lastfm in enumerate(lastfm_titles)
              if _track_similarity(spotify, lastfm) >= LASTFM_MIN_TRACK_SIMILARITY]
             for spotify in spotify_titles]
    matched: dict[int, int] = {}

    def assign(i: int, seen: set[int]) -> bool:
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in matched or assign(matched[j], seen):
                matched[j] = i
                return True
        return False

    return sum(assign(i, set()) for i in range(len(spotify_titles)))


def _track_overlap(spotify_titles: list[str], lastfm_titles: list[str]) -> float:
    """Compatibility overlap using the smaller provider track-list denominator."""
    count = _track_match_count(spotify_titles, lastfm_titles)
    return count / max(1, min(len(spotify_titles), len(lastfm_titles)))


def _transliteration_alignment(spotify_album: dict, spotify_titles: list[str],
                               candidate: dict, info: dict,
                               lastfm_titles: list[str]) -> dict | None:
    """Recognize one narrowly bounded Latin/Japanese positional sequence."""
    spotify_artists = spotify_album.get("artists") or []
    spotify_artist = (spotify_artists[0].get("name", "")
                      if spotify_artists and isinstance(spotify_artists[0], dict) else "")
    identities = ((spotify_album.get("name", ""), info.get("name", "")),
                  (spotify_album.get("name", ""), candidate.get("name", "")),
                  (spotify_artist, info.get("artist", "")),
                  (spotify_artist, candidate.get("artist", "")))
    if any(not match_key(a) or match_key(a) != match_key(b) for a, b in identities):
        return None
    count = len(spotify_titles)
    if count < 5 or count != len(lastfm_titles):
        return None
    same = [_track_similarity(a, b) >= LASTFM_MIN_TRACK_SIMILARITY
            for a, b in zip(spotify_titles, lastfm_titles)]
    anchors = sum(same)
    if anchors < 3 or anchors / count < 0.30:
        return None
    for i, title in enumerate(spotify_titles):
        if any(i != j and _track_similarity(title, other) >= LASTFM_MIN_TRACK_SIMILARITY
               for j, other in enumerate(lastfm_titles)):
            return None
    for i, anchored in enumerate(same):
        if anchored:
            continue
        left, right = spotify_titles[i], lastfm_titles[i]
        left_latin_only = _latin_letters_only(left)
        right_latin_only = _latin_letters_only(right)
        if not ((left_latin_only and _JAPANESE_RE.search(right)) or
                (right_latin_only and _JAPANESE_RE.search(left))):
            return None
    return {"anchors": anchors, "transliterated_pairs": count - anchors}


def validate_lastfm_info(spotify_album: dict, spotify_tracks: list[dict],
                         candidate: dict, info: dict) -> dict:
    returned = {"name": info.get("name", ""), "artist": info.get("artist", "")}
    spotify_score = lastfm_candidate_score(spotify_album, returned)
    candidate_title = similarity(info.get("name", ""), candidate.get("name", ""))
    candidate_artist = similarity(info.get("artist", ""), candidate.get("artist", ""))
    evidence = {"selected_title": candidate.get("name", ""),
                "selected_artist": candidate.get("artist", ""),
                "returned_title": info.get("name", ""),
                "returned_artist": info.get("artist", ""),
                "title_score": spotify_score["title_score"],
                "artist_score": spotify_score["artist_score"]}
    if (spotify_score["title_score"] < LASTFM_MIN_TITLE or
            spotify_score["artist_score"] < LASTFM_MIN_ARTIST or
            candidate_title < LASTFM_MIN_TITLE or candidate_artist < LASTFM_MIN_ARTIST):
        return {"accepted": False, "reason": "lastfm_identity_changed", **evidence}
    lastfm_keys = _lastfm_track_keys(info)
    if not lastfm_keys:
        return {"accepted": True, "reason": "lastfm_identity_no_tracks", **evidence}
    spotify_keys = [match_key(t.get("name", "")) for t in spotify_tracks if t.get("name")]
    matched = _track_match_count(spotify_keys, lastfm_keys)
    denominator = max(1, min(len(spotify_keys), len(lastfm_keys)))
    overlap = matched / denominator
    track_evidence = {"matched_tracks": matched, "denominator": denominator,
                      "overlap": overlap, "spotify_track_count": len(spotify_keys),
                      "lastfm_track_count": len(lastfm_keys), "gate": 0.60}
    # Tracks are optional, but once supplied a sub-.60 overlap is affirmative
    # contradictory evidence rather than merely missing confirmation.
    if overlap < 0.60:
        alignment = _transliteration_alignment(
            spotify_album, spotify_keys, candidate, info, lastfm_keys)
        if alignment:
            return {"accepted": True, "reason": "lastfm_transliteration_alignment",
                    **evidence, **track_evidence, **alignment}
        return {"accepted": False, "reason": "lastfm_track_contradiction",
                **evidence, **track_evidence}
    return {"accepted": True, "reason": "lastfm_validated", **evidence, **track_evidence}


def lastfm_recovery_validation(spotify_album: dict, spotify_tracks: list[dict],
                               candidate: dict, info: dict) -> dict:
    """Validate an alias detail and require coverage of the full Spotify release."""
    validation = validate_lastfm_info(spotify_album, spotify_tracks, candidate, info)
    if not validation["accepted"]:
        return validation
    lastfm_titles = _lastfm_track_keys(info)
    spotify_titles = [match_key(track.get("name", "")) for track in spotify_tracks
                      if track.get("name")]
    if not lastfm_titles or not spotify_tracks:
        return {"accepted": False, "reason": "lastfm_recovery_no_tracks"}
    matched = _track_match_count(spotify_titles, lastfm_titles)
    coverage = matched / len(spotify_tracks)
    if coverage < 0.60:
        return {"accepted": False, "reason": "lastfm_recovery_insufficient_coverage",
                "matched_tracks": matched, "spotify_track_coverage": coverage}
    return {"accepted": True, "reason": "lastfm_recovered",
            "overlap": _track_overlap(spotify_titles, lastfm_titles),
            "matched_tracks": matched, "spotify_track_coverage": coverage}


def recover_lastfm_candidate(lfm: Any, spotify_album: dict, spotify_tracks: list[dict],
                             candidates: list[dict]) -> dict:
    """Recover a unique ambiguity using every contender's validated detail."""
    safe = []
    for candidate in sorted(candidates, key=_lastfm_search_identity):
        mbid = _nonempty_string(candidate.get("mbid"))
        if mbid and _UUID_RE.fullmatch(mbid):
            info = lfm.album_getinfo(mbid=mbid)
        else:
            info = lfm.album_getinfo(artist=candidate.get("artist"),
                                     album=candidate.get("name"), autocorrect=0)
        validation = lastfm_recovery_validation(
            spotify_album, spotify_tracks, candidate, info)
        if validation["accepted"]:
            safe.append((candidate, info, validation))

    spotify_title = match_key(spotify_album.get("name", ""))
    # Search rows are only locators. Canonical detail identity is the evidence
    # validated above, so exact-title preference must use that returned title.
    exact = [row for row in safe if match_key(row[1].get("name", "")) == spotify_title]
    primary = match_key((spotify_album.get("artists") or [{}])[0].get("name", ""))
    primary_exact = [row for row in exact if match_key(row[1].get("artist", "")) == primary]
    winner = (exact[0] if len(exact) == 1 else
              primary_exact[0] if len(primary_exact) == 1 else
              safe[0] if not exact and len(safe) == 1 else None)
    if winner is None:
        return {"candidate": None, "reason": "lastfm_ambiguous"}
    candidate, info, validation = winner
    return {"candidate": candidate, "info": info, "validation": validation,
            "reason": "lastfm_recovered",
            "score": lastfm_candidate_score(spotify_album, candidate)["score"]}


def lookup_combined_lastfm(lfm: Any, spotify_album: dict,
                           spotify_tracks: list[dict]) -> dict | None:
    """Try Last.fm's canonical combined credit for a collaboration release."""
    artists = [a.get("name", "") for a in spotify_album.get("artists", [])
               if isinstance(a.get("name"), str) and a.get("name")]
    if len(artists) < 2:
        return None
    credit = " & ".join(artists)
    candidate = {"name": spotify_album.get("name", ""), "artist": credit}
    try:
        info = lfm.album_getinfo(artist=credit, album=candidate["name"], autocorrect=0)
    except ProviderError as exc:
        if exc.http_status == 404:
            return None
        raise
    validation = validate_lastfm_info(spotify_album, spotify_tracks, candidate, info)
    if not validation["accepted"]:
        return None
    return {"candidate": candidate, "info": info, "validation": validation,
            "reason": "lastfm_collaboration_lookup",
            "score": lastfm_candidate_score(spotify_album, candidate)["score"]}


def _accept_stale_lastfm_tracks(spotify_album: dict, candidate: dict,
                                info: dict, validation: dict) -> bool:
    """Allow exact aggregate pages whose track cache is stale, but not eponymous editions."""
    if validation.get("reason") != "lastfm_track_contradiction":
        return False
    title = match_key(spotify_album.get("name", ""))
    artist = match_key(candidate.get("artist", ""))
    spotify_artists = {match_key(a.get("name", "")) for a in spotify_album.get("artists", [])}
    return bool(title and title != artist and artist in spotify_artists and
                title == match_key(candidate.get("name", "")) == match_key(info.get("name", "")) and
                artist == match_key(info.get("artist", "")))


def pick_top_tags(album_info: dict, max_n: int, blocklist: Iterable[str],
                  artist_names: Iterable[str] = ()) -> list[str]:
    """Handles four return shapes:
         ''                       (no tags)
         {'tag': []}              (no tags)
         {'tag': [{name:'…'}]}    (multiple tags, list of dicts)
         {'tag': {name:'…'}}      (single tag — dict, not list!)
    Last.fm may also return tags as bare strings.
    """
    tags = ((album_info or {}).get("toptags") or
            (album_info or {}).get("tags") or {})
    raw = tags.get("tag", []) if isinstance(tags, dict) else []
    if isinstance(raw, (dict, str)):
        raw = [raw]
    elif not isinstance(raw, list):
        raw = []
    pats = [re.compile(p, re.IGNORECASE) for p in blocklist]
    artist_keys = {match_key(name) for name in artist_names}
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name") or ""
        else:
            continue
        name = name.strip()
        if not name:
            continue
        key = match_key(name)
        if any(p.match(name) for p in pats) or key in artist_keys or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= max_n:
            break
    return out


# --------------------------------------------------------------------------- #
# WordPress REST client
# --------------------------------------------------------------------------- #

class WordPress:
    def __init__(self, base: str, user: str, app_pw: str):
        self.base      = base.rstrip("/")
        self.api       = f"{self.base}/wp-json/wp/v2"
        self._auth     = "Basic " + base64.b64encode(f"{user}:{app_pw}".encode()).decode()
        self._hdr_json = {"Authorization": self._auth, "Accept": "application/json",
                          "Content-Type": "application/json"}
        self._hdr_get  = {"Authorization": self._auth, "Accept": "application/json"}

    def _url(self, path: str, **qs) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.api}{path}" + (sep + urllib.parse.urlencode(qs) if qs else "")

    def _req_get(self, url: str) -> tuple[Any, dict]:
        req = urllib.request.Request(url, headers=self._hdr_get)
        with urllib.request.urlopen(req, timeout=30) as r:
            try:
                return json.loads(r.read()), dict(r.headers)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("WordPress returned malformed JSON.") from exc

    def _req_post(self, url: str, body: dict) -> Any:
        for attempt in (0, 1):
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers=self._hdr_json, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    if attempt == 0:
                        retry = int(exc.headers.get("Retry-After", "2"))
                        log.warning("WP 429, sleeping %ds", retry)
                        time.sleep(retry)
                        continue
                raise

    # ---- reads ----

    def list_posts(self, per_page: int = 100) -> Iterable[dict]:
        page = 1
        while True:
            url = self._url("/posts", per_page=per_page, page=page, context="edit")
            chunk, hdrs = self._req_get(url)
            if not chunk:
                return
            for p in chunk:
                yield p
            total_pages_hdr = hdrs.get("X-WP-TotalPages", str(page))
            try:
                total_pages = int(total_pages_hdr)
            except (TypeError, ValueError):
                total_pages = page
            if page >= total_pages:
                return
            page += 1

    def total_posts(self) -> int:
        url = self._url("/posts", per_page=1, context="edit")
        req = urllib.request.Request(url, headers=self._hdr_get)
        with urllib.request.urlopen(req, timeout=30) as r:
            try:
                return int(r.headers.get("X-WP-Total", "0"))
            except (TypeError, ValueError):
                return 0

    def list_tax_terms(self, tax: str) -> dict[str, int]:
        """Return every term; only a later out-of-range 400 ends pagination."""
        found: dict[str, int] = {}
        page = 1
        while True:
            try:
                rows, headers = self._req_get(
                    self._url(f"/{tax}", per_page=100, page=page))
            except urllib.error.HTTPError as exc:
                # WordPress uses this specific REST error to end headerless pagination.
                if exc.code == 400:
                    if page > 1:
                        try:
                            error = json.loads(exc.read().decode("utf-8"))
                        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                            raise exc
                        if isinstance(error, dict):
                            if error.get("code") == "rest_post_invalid_page_number":
                                return found
                raise
            if not isinstance(rows, list):
                raise RuntimeError(f"WordPress {tax} response was not a list")
            for row in rows:
                found[row["name"]] = row["id"]
            raw_pages = headers.get("X-WP-TotalPages") or headers.get("x-wp-totalpages")
            if raw_pages is not None:
                try:
                    if page >= int(raw_pages):
                        return found
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Invalid X-WP-TotalPages header") from exc
            elif len(rows) < 100:
                return found
            page += 1

    @staticmethod
    def _header(headers: dict, name: str) -> Any:
        return next((value for key, value in headers.items() if key.lower() == name.lower()), None)

    def _read_tag_cache(self, path: Path) -> tuple[dict, dict[int, str]] | None:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            raw_tags = cached["tags"]
            tags = {int(tag_id): name for tag_id, name in raw_tags.items()}
            now = time.time()
            valid = (cached["api"] == self.api and isinstance(cached["fetched_at"], (int, float))
                     and not isinstance(cached["fetched_at"], bool)
                     and math.isfinite(cached["fetched_at"]) and cached["fetched_at"] <= now
                     and isinstance(cached["total"], int) and not isinstance(cached["total"], bool)
                     and isinstance(cached["highest_id"], int)
                     and not isinstance(cached["highest_id"], bool)
                     and isinstance(raw_tags, dict) and all(
                         str(tag_id) == str(int(tag_id)) and int(tag_id) > 0 and isinstance(name, str)
                         for tag_id, name in raw_tags.items())
                     and cached["total"] == len(tags)
                     and cached["highest_id"] == max(tags, default=0))
            return (cached, tags) if valid else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _tag_probe(self) -> tuple[int, int]:
        rows, headers = self._req_get(
            self._url("/tags", per_page=1, page=1, orderby="id", order="desc"))
        if not isinstance(rows, list):
            raise RuntimeError("WordPress tags response was not a list")
        try:
            total = int(self._header(headers, "X-WP-Total"))
            highest_id = int(rows[0]["id"]) if rows else 0
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise RuntimeError("Invalid WordPress tag metadata") from exc
        return total, highest_id

    def _scan_tags(self) -> tuple[dict[int, str], int, int]:
        tags: dict[int, str] = {}
        page = 1
        while True:
            rows, headers = self._req_get(
                self._url("/tags", per_page=100, page=page, orderby="id", order="asc"))
            if not isinstance(rows, list):
                raise RuntimeError("WordPress tags response was not a list")
            try:
                total = int(self._header(headers, "X-WP-Total"))
                total_pages = int(self._header(headers, "X-WP-TotalPages"))
                tags.update((int(row["id"]), row["name"]) for row in rows)
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError("Invalid WordPress tag response") from exc
            if page >= total_pages:
                break
            page += 1
        if len(tags) != total:
            raise RuntimeError("Incomplete WordPress tag scan")
        return tags, total, max(tags, default=0)

    def _stable_tag_scan(self) -> tuple[dict[int, str], int, int]:
        for _ in range(2):
            before = self._tag_probe()
            result = self._scan_tags()
            after = self._tag_probe()
            if before == result[1:] == after:
                return result
        raise RuntimeError("WordPress tags changed during scan")

    def list_tags(self, name_to_id: dict[int, str], cache_path: str | Path | None = None) -> dict[int, str]:
        path = Path(cache_path) if cache_path is not None else TAG_CACHE_PATH
        cache = self._read_tag_cache(path)
        cached_meta, cached_tags = cache or ({}, {})
        age = time.time() - cached_meta["fetched_at"] if cache else -1
        fresh = bool(cache and 0 <= age < TAG_CACHE_MAX_AGE)
        try:
            if fresh and self._tag_probe() == (cached_meta["total"], cached_meta["highest_id"]):
                tags = cached_tags
            else:
                tags, total, highest_id = self._stable_tag_scan()
                try:
                    write_json_atomic(path, {"api": self.api, "fetched_at": time.time(),
                                             "total": total, "highest_id": highest_id,
                                             "tags": tags})
                except OSError as exc:
                    log.warning("Could not write WordPress tag cache: %s", _safe_error(exc))
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            if not cache:
                raise
            log.warning("WordPress tag refresh failed; using cached tags: %s", _safe_error(exc))
            tags = cached_tags
        name_to_id.clear()
        name_to_id.update(tags)
        return name_to_id

    def create_term(self, tax: str, name: str) -> int | None:
        try:
            t = self._req_post(self._url(f"/{tax}"), {"name": name})
            return t["id"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400:
                if "term_exists" not in body:
                    raise
                # re-fetch by slug
                slug = urllib.parse.quote(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))
                try:
                    rows, _ = self._req_get(self._url(f"/{tax}", slug=slug))
                    if rows:
                        return rows[0]["id"]
                except urllib.error.HTTPError as _slug_err:
                    log.debug("slug re-lookup failed: %s", _slug_err)
                    pass
                # fallback: list all and find by name
                all_rows, _ = self._req_get(self._url(f"/{tax}", per_page=100))
                for r in all_rows:
                    if r["name"] == name:
                        return r["id"]
            log.warning("create_term %s/%s failed: HTTP %d %s",
                        tax, name, exc.code, body[:200])
            return None

    def update_post(self, pid: int, body: dict) -> dict:
        url = self._url(f"/posts/{pid}")
        return self._req_post(url, body)


# --------------------------------------------------------------------------- #
# Field-presence predicate + builder
# --------------------------------------------------------------------------- #

def is_field_present(field: str, v: Any) -> bool:
    """Plan says never overwrite anything currently populated. Treat:
        None, '', 0 (numeric placeholders), False (bool default),
        []  (empty list), {}  as EMPTY → safe to write.
        Music_listened_at 'YYYYMMDD' (no dashes) is treated as PRESENT
        (per Q9=a) — leave alone.
    """
    if v is None:
        return False
    if field in ("music_explicit", "music_favorite"):
        return bool(v)
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        if v == "":
            return False
        if field == "music_listened_at" and re.fullmatch(r"\d{8}", v):
            return True   # honor Q9=a: the legacy YYYYMMDD strings stay
        return True
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return bool(v)


def is_fully_filled(acf: dict) -> bool:
    # False is a complete explicitness result, but remains fillable as an SCF default.
    return all((f == "music_explicit" and isinstance(acf.get(f), bool)) or
               is_field_present(f, acf.get(f))
               for f in AUTO_FILLABLE_FIELDS)


def post_is_complete(post: dict) -> bool:
    acf = post.get("acf") or {}
    return (is_fully_filled(acf) and bool(post.get("artist")) and
            bool(post.get("release_type")))


def _computed_value_valid(key: str, value: Any) -> bool:
    """Provider absence is not a value, but computed false/zero can be."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, dict)):
        return bool(value)
    if isinstance(value, bool):
        return key == "music_explicit"
    return isinstance(value, (int, float))


def _set_managed(acf_in: dict, acf_out: dict, key: str, value: Any,
                 write_policy: str) -> None:
    """Plan a valid managed value according to the selected ownership policy."""
    if key not in AUTO_FILLABLE_FIELDS:
        raise ValueError(f"Unmanaged ACF key: {key}")
    if write_policy not in (WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED):
        raise ValueError(f"Unknown write policy: {write_policy}")
    if not _computed_value_valid(key, value):
        return
    if (write_policy == WRITE_OVERWRITE_MANAGED or
            not is_field_present(key, acf_in.get(key))):
        acf_out[key] = value


# --------------------------------------------------------------------------- #
# Per-post enrichment
# --------------------------------------------------------------------------- #

def _diagnostic_code(reason: str) -> str:
    return {"lastfm_ambiguous_exact": "lastfm_ambiguous",
            "lastfm_identity_changed": "lastfm_identity_mismatch",
            "lastfm_track_contradiction": "lastfm_track_mismatch",
            "provider_error": "lastfm_provider_error"}.get(reason, reason)


def _unresolved(post: dict, code: str, message: str,
                details: dict | None = None) -> dict:
    diagnostic: dict[str, Any] = {
        "code": _diagnostic_code(code), "message": message or code}
    if details is not None:
        diagnostic["details"] = details
    return {"post_id": post["id"], "post_title": post["title"]["rendered"],
            "diagnostics": [diagnostic]}


def _ignored(post: dict, code: str, message: str) -> dict:
    return {**_unresolved(post, code, message), "ignored": True}


def _provider_unresolved(post: dict, code: str, exc: BaseException,
                         operation: str) -> dict:
    if isinstance(exc, ProviderError):
        diagnostic = exc.diagnostic(code)
    else:
        cls = SpotifyProviderError if code.startswith("spotify_") else LastFMProviderError
        provider = "Spotify" if code.startswith("spotify_") else "Last.fm"
        # Legacy/fake clients can raise exceptions containing URLs, response
        # bodies, or credentials. Do not copy that uncontrolled text into an
        # artifact; typed transport errors already carry a safe message.
        diagnostic = cls(
            f"{provider} {operation} failed unexpectedly.", operation=operation,
            failure_kind="unexpected", retryable=False).diagnostic(code)
    return _unresolved(post, diagnostic["code"], diagnostic["message"],
                       diagnostic["details"])


def enrich(post: dict, spt: Any, lfm: Any,
           tag_id_to_name: dict[int, str],
           write_policy: str = WRITE_FILL_ONLY,
           release_type_terms: dict[str, int] | None = None) -> dict | None:
    """Build declarative provider evidence and writes; never resolve or write WP terms."""
    if write_policy not in (WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED):
        raise ValueError(f"Unknown write policy: {write_policy}")
    pid  = post["id"]
    acf_in = post.get("acf") or {}
    title = post["title"]["rendered"]
    post_date = post["date"]
    tag_names = [tag_id_to_name.get(t, "") for t in post.get("tags", []) if t in tag_id_to_name]

    if write_policy == WRITE_FILL_ONLY and post_is_complete(post):
        log.debug("SKIP post %d '%s' (fully filled)", pid, title)
        return None

    q_title = raw_query(title)
    q_artists = [raw_query(a) for a in tag_names if raw_query(a)]
    log.debug("post %d :: title=%r artists=%r date=%s",
              pid, title, tag_names, post_date)

    if not q_artists:
        return _unresolved(post, "spotify_missing_artist", "No artist tags were available.")
    expected_type = expected_release_type(post, release_type_terms)
    try:
        cands = search_ladder(spt, q_title, q_artists, expected_type)
        stored_id = acf_in.get("spotify_album_id")
        title_base = _release_title_base(q_title)
        if (isinstance(stored_id, str) and stored_id and
                stored_id not in {candidate.get("id") for candidate in cands} and
                title_base != match_key(q_title)):
            widened = search_ladder(spt, title_base, q_artists, expected_type)
            cands = list({candidate.get("id"): candidate for candidate in cands + widened
                          if candidate.get("id")}.values())
    except (OSError, RuntimeError, ValueError) as exc:
        return _provider_unresolved(post, "spotify_provider_error", exc, "album.search")
    spotify_match = choose_spotify_candidate(cands, q_title, q_artists, expected_type)
    if spotify_match["reason"] == "spotify_ambiguous":
        try:
            spotify_match = recover_spotify_ambiguity(
                spt, post, spotify_match["contenders"], q_title, q_artists, expected_type)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            return _provider_unresolved(post, "spotify_provider_error", exc,
                                        "album.get/track.list")
    winner = spotify_match.get("candidate")
    if winner is None:
        if spotify_match["reason"] in {"spotify_no_results", "spotify_low_confidence"}:
            return _ignored(
                post, "spotify_catalog_unavailable",
                "Spotify's current catalog has no safe title/artist match for this release.")
        message = ("Spotify remained ambiguous because current full-release evidence did not "
                   "identify exactly one safe candidate."
                   if spotify_match["reason"] == "spotify_ambiguous" else
                   "Spotify did not produce a safe unique match.")
        return _unresolved(post, spotify_match["reason"], message)

    log.info("post %d '%s' -> Spotify %s '%s' (%d tracks)",
             pid, title, winner["id"], winner["name"], winner.get("total_tracks", 0))

    try:
        full_evidence = spotify_match.get("full_evidence")
        if full_evidence:
            album, tracks = full_evidence["album"], full_evidence["tracks"]
        else:
            album, tracks = _spotify_album_and_tracks(spt, winner["id"])
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return _provider_unresolved(post, "spotify_provider_error", exc, "album.get/track.list")
    try:
        validate_spotify_album_tracks(album, tracks)
        if not _spotify_tracks_complete(album, tracks):
            raise SpotifyProviderError(
                "Spotify track.list response was incomplete.", operation="track.list")
    except SpotifyProviderError as exc:
        if _spotify_tracks_market_restricted(tracks):
            return _ignored(
                post, "spotify_catalog_unavailable",
                "Spotify market restrictions hide required track titles and durations.")
        return _provider_unresolved(post, "spotify_provider_error", exc, "track.list")
    try:
        lfm_candidates = search_lastfm_candidates(lfm, album, limit=10)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return _provider_unresolved(post, "lastfm_provider_error", exc, "album.search")
    lastfm_match = choose_lastfm_candidate(album, lfm_candidates)
    selected = lastfm_match.get("candidate")
    info = validation = None
    collaboration_lookup = False
    combined_attempted = False
    if selected is None and lastfm_match["reason"] in {
            "lastfm_ambiguous", "lastfm_ambiguous_exact"}:
        try:
            combined_attempted = True
            combined = lookup_combined_lastfm(lfm, album, tracks)
            if combined:
                lastfm_match = combined
                selected, info, validation = (combined["candidate"], combined["info"],
                                              combined["validation"])
                collaboration_lookup = True
            else:
                contenders = lastfm_match.get("contenders", [])
                primary = match_key((album.get("artists") or [{}])[0].get("name", ""))
                primary_exact = [candidate for candidate in contenders
                                 if match_key(candidate.get("name", "")) ==
                                 match_key(album.get("name", "")) and
                                 match_key(candidate.get("artist", "")) == primary]
                if len(primary_exact) == 1:
                    selected = primary_exact[0]
                    lastfm_match = {**lastfm_candidate_score(album, selected),
                                    "reason": "lastfm_exact_primary"}
                else:
                    lastfm_match = recover_lastfm_candidate(
                        lfm, album, tracks, contenders)
                    selected = lastfm_match.get("candidate")
                    info = lastfm_match.get("info")
                    validation = lastfm_match.get("validation")
        except (OSError, RuntimeError, ValueError) as exc:
            # Missing one contender means uniqueness cannot be established safely.
            return _provider_unresolved(post, "lastfm_provider_error", exc, "album.getinfo")
    if selected is None:
        if lastfm_match["reason"] in {"lastfm_no_results", "lastfm_low_confidence"}:
            return _ignored(
                post, "lastfm_catalog_unavailable",
                "Last.fm's current catalog has no safe title/artist match for this release.")
        return _unresolved(post, lastfm_match["reason"],
                           "Last.fm did not produce a safe unique match.")
    lookup_fallback = None
    lookup_route = "artist/title"
    try:
        raw_mbid = selected.get("mbid")
        mbid = raw_mbid if isinstance(raw_mbid, str) and _UUID_RE.fullmatch(raw_mbid) else None
        if info is None:
            lookup_route = "mbid" if mbid else "artist/title"
            info = (lfm.album_getinfo(mbid=mbid) if mbid else
                    lfm.album_getinfo(artist=selected.get("artist"),
                                      album=selected.get("name"), autocorrect=0))
        if validation is None:
            validation = validate_lastfm_info(album, tracks, selected, info)
    except (OSError, RuntimeError, ValueError) as exc:
        return _provider_unresolved(post, "lastfm_provider_error", exc, "album.getinfo")
    original_validation = validation
    if not validation["accepted"] and mbid and lookup_route == "mbid":
        # A contradictory MBID response is useful evidence. A failed retry must
        # never hide it or be reclassified as an outage.
        try:
            alternate = lfm.album_getinfo(artist=selected.get("artist"),
                                          album=selected.get("name"), autocorrect=0)
            alternate_validation = validate_lastfm_info(album, tracks, selected, alternate)
            if alternate_validation["accepted"]:
                info, validation = alternate, alternate_validation
                lookup_fallback = original_validation
        except (OSError, RuntimeError, ValueError):
            pass
    if not validation["accepted"] and not combined_attempted:
        try:
            combined_attempted = True
            combined = lookup_combined_lastfm(lfm, album, tracks)
            if combined:
                lastfm_match = combined
                selected, info, validation = (combined["candidate"], combined["info"],
                                              combined["validation"])
                collaboration_lookup = True
                lookup_route, mbid = "combined artist/title", None
        except (OSError, RuntimeError, ValueError):
            pass
    stale_tracks = False
    if not validation["accepted"] and _accept_stale_lastfm_tracks(
            album, selected, info, validation):
        selected = {**selected, "mbid": ""}
        mbid = None
        validation = {**validation, "accepted": True, "reason": "lastfm_stale_tracks"}
        stale_tracks = True
    if not validation["accepted"]:
        code = _diagnostic_code(original_validation["reason"])
        routes = "mbid and artist/title" if mbid and lookup_route == "mbid" else lookup_route
        if original_validation["reason"] == "lastfm_identity_changed":
            message = (f"Last.fm identity mismatch after {routes} lookup: "
                       f"selected='{original_validation['selected_title']}' — "
                       f"'{original_validation['selected_artist']}'; "
                       f"returned='{original_validation['returned_title']}' — "
                       f"'{original_validation['returned_artist']}'; "
                       f"title_score={original_validation['title_score']:.3f} "
                       f"artist_score={original_validation['artist_score']:.3f}.")
        else:
            message = (f"Last.fm track mismatch after {routes} lookup: "
                       f"matched={original_validation['matched_tracks']} "
                       f"denominator={original_validation['denominator']} "
                       f"overlap={original_validation['overlap']:.3f}; "
                       f"spotify_tracks={original_validation['spotify_track_count']} "
                       f"lastfm_tracks={original_validation['lastfm_track_count']}; "
                       f"gate={original_validation['gate']:.3f}.")
        return _unresolved(post, code, message)
    resolved_lastfm_mbid = resolve_lastfm_mbid(info, selected)
    resolved_lastfm_url = resolve_lastfm_url(info, selected)
    artist_names = [a.get("name", "") for a in album.get("artists", [])]
    genre_names = pick_top_tags(
        info, max_n=3, blocklist=LFM_BLOCKLIST, artist_names=artist_names)
    if not genre_names:
        try:
            # A successful raw fallback establishes the accepted lookup identity;
            # do not route tag discovery back through the rejected MBID.
            if lookup_fallback:
                album_tags = lfm.album_gettoptags(
                    artist=info.get("artist"), album=info.get("name"), autocorrect=0)
            else:
                album_tags = (lfm.album_gettoptags(mbid=mbid) if mbid else
                              lfm.album_gettoptags(artist=info.get("artist"),
                                                  album=info.get("name"), autocorrect=0))
            genre_names = pick_top_tags(
                album_tags, max_n=3, blocklist=LFM_BLOCKLIST, artist_names=artist_names)
        except (OSError, RuntimeError, ValueError):
            pass
    if not genre_names:
        tag_artists = list(dict.fromkeys(
            name for name in (info.get("artist"), selected.get("artist")) if name))
        for tag_artist in tag_artists:
            for autocorrect in (0, 1):
                try:
                    artist_tags = lfm.artist_gettoptags(
                        tag_artist, autocorrect=autocorrect)
                    genre_names = pick_top_tags(
                        artist_tags, max_n=3, blocklist=LFM_BLOCKLIST,
                        artist_names=artist_names)
                except (OSError, RuntimeError, ValueError):
                    continue
                if genre_names:
                    break
            if genre_names:
                break
    artist_lookup = getattr(spt, "artist", None)
    if not genre_names and callable(artist_lookup):
        spotify_genres = []
        for artist in album.get("artists", []):
            if not isinstance(artist.get("id"), str) or not artist["id"]:
                continue
            try:
                artist_info = artist_lookup(artist["id"])
                if not isinstance(artist_info, dict):
                    continue
                genres = artist_info.get("genres")
                if not isinstance(genres, list) or not all(
                        isinstance(genre, str) for genre in genres):
                    continue
                spotify_genres.extend(genres)
            except (OSError, RuntimeError, ValueError, KeyError):
                continue
        genre_names = pick_top_tags(
            {"tags": {"tag": spotify_genres}}, max_n=3,
            blocklist=LFM_BLOCKLIST, artist_names=artist_names)
    if not genre_names:
        log.warning("post %d — no useful artist or release genres for %s; leaving genre unchanged",
                    pid, album["name"])

    # Rebuilding provider-owned rows must not reset the editor-owned highlight.
    highlights = {row.get("spotify_id"): bool(row.get("highlight"))
                  for row in (acf_in.get("music_tracks") or [])
                  if isinstance(row, dict) and row.get("spotify_id")}
    track_rows = [
        {"disc_number":  t.get("disc_number", 1),
         "track_number": t.get("track_number", 0),
         "title":        t["name"],
         "duration_ms":  t["duration_ms"],
         "spotify_id":   t["id"],
         "highlight":    highlights.get(t["id"], False),
         "explicit":     t["explicit"]}
        for t in tracks
    ]
    length_ms = sum(t["duration_ms"] for t in track_rows)
    total     = album.get("total_tracks") or len(track_rows)
    rt_term_name = compute_release_type(track_rows, album.get("album_type", ""))
    rt_term_slug = rt_term_name.lower()

    acf_out: dict[str, Any] = {}
    managed_values = {
        "spotify_title": album.get("name"),
        "music_tracks": track_rows or None,
        "music_length_ms": length_ms,
        "spotify_album_id": album["id"],
        "spotify_album_url": f"https://open.spotify.com/album/{album['id']}",
        "music_release_date": _post_dmy(album.get("release_date", "")),
        "music_listened_at": _post_dmy(post_date),
        "lastfm_url": resolved_lastfm_url,
        "mbid": resolved_lastfm_mbid,
        "music_total_tracks": total,
        "music_avg_track_ms": (length_ms // total) if total else 0,
        "music_explicit": any(t["explicit"] for t in track_rows),
        "listen_count": 1,
    }
    for key, value in managed_values.items():
        _set_managed(acf_in, acf_out, key, value, write_policy)

    write: dict[str, Any] = {}
    if acf_out:
        write["acf"] = acf_out
    cat_id = CATEGORY_MAP[rt_term_name]
    categories = list(dict.fromkeys(
        [cid for cid in post.get("categories", []) if cid not in CATEGORY_MAP.values()] + [cat_id]))
    if (write_policy == WRITE_OVERWRITE_MANAGED or
            categories != post.get("categories", [])):
        write["categories"] = categories
    taxonomies = {"release_type": [rt_term_name]}
    artist_names = list(dict.fromkeys(name for name in tag_names if name))
    if artist_names and (write_policy == WRITE_OVERWRITE_MANAGED or not post.get("artist")):
        taxonomies["artist"] = artist_names
    if genre_names and (write_policy == WRITE_OVERWRITE_MANAGED or not post.get("genre")):
        taxonomies["genre"] = genre_names
    write["taxonomies"] = taxonomies
    diagnostics = []
    if collaboration_lookup:
        diagnostics.append({
            "code": "lastfm_collaboration_lookup",
            "message": "Accepted Last.fm's exact combined-artist album page."})
    if stale_tracks:
        diagnostics.append({
            "code": "lastfm_stale_tracks",
            "message": ("Accepted exact Last.fm album/artist identity despite a stale track "
                        "cache; no search-result MBID was retained.")})
    if lookup_fallback:
        diagnostics.append({
            "code": "lastfm_lookup_fallback",
            "message": (f"Accepted selected artist/title lookup after MBID validation failed "
                        f"({lookup_fallback['reason']}); alternate overlap="
                        f"{validation.get('overlap', 0.0):.3f}.")})
    if validation.get("reason") == "lastfm_transliteration_alignment":
        diagnostics.append({
            "code": "lastfm_transliteration_alignment",
            "message": (f"Accepted equal {validation['denominator']}-track sequence using "
                        f"{validation['anchors']} same-position lexical anchors and "
                        f"{validation['transliterated_pairs']} Latin↔Japanese pairs.")})
    if not resolved_lastfm_mbid:
        diagnostics.append({"code": "lastfm_no_mbid", "message": "Validated Last.fm album and selected search result have no usable MBID."})
    if not genre_names:
        diagnostics.append({
            "code": "lastfm_no_tags",
            "message": "No acceptable Last.fm tags or Spotify artist genres were returned."})
    spotify_score = spotify_match.get("score", spotify_candidate_score(winner, q_title, q_artists)["score"])
    lastfm_score = lastfm_match.get("score", lastfm_candidate_score(album, selected)["score"])
    lastfm_evidence = {"title": selected["name"], "artist": selected["artist"],
                       "score": lastfm_score}
    if resolved_lastfm_mbid: lastfm_evidence["mbid"] = resolved_lastfm_mbid
    if resolved_lastfm_url: lastfm_evidence["url"] = resolved_lastfm_url
    if "overlap" in validation: lastfm_evidence["track_overlap"] = validation["overlap"]
    patch = {"post_id": pid, "post_title": title,
             "matches": {"spotify": {"id": album["id"], "title": album["name"],
                                        "artists": [a["name"] for a in album.get("artists", [])],
                                        "score": spotify_score},
                         "lastfm": lastfm_evidence},
             "write": write, "diagnostics": diagnostics}
    if "modified" in post: patch["source_modified"] = post["modified"]
    return patch


def _ensure_term(wp: WordPress, cache: dict[str, dict[str, int]],
                 tax: str, name: str) -> int | None:
    if not name:
        return None
    cache.setdefault(tax, {})
    if name in cache[tax]:
        return cache[tax][name]
    # Probing by name: cached by a GET slug=neck won't work name→slug,
    # but we already did the broader pull. Try direct lookup on existing cache.
    if tax not in cache[tax]:
        cache[tax] = wp.list_tax_terms(tax)
    if name in cache[tax]:
        return cache[tax][name]
    new_id = wp.create_term(tax, name)
    if new_id:
        cache[tax][name] = new_id
    return new_id


# --------------------------------------------------------------------------- #
# Plan validation and replay
# --------------------------------------------------------------------------- #

def _plan_error(path: str, message: str) -> None:
    raise ValueError(f"Invalid plan at {path}: {message}")


def _exact(obj: Any, required: set[str], optional: set[str], path: str) -> None:
    if not isinstance(obj, dict) or set(obj) - required - optional or required - set(obj):
        _plan_error(path, "unsupported or missing keys")


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _score_value(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_plan(plan: Any) -> dict:
    """Recursively validate the complete artifact before slicing or writes."""
    # Identify old/unknown artifacts before enforcing the current root shape so
    # callers get an actionable error even when an older root lacks new keys.
    if (isinstance(plan, dict) and type(plan.get("schema_version")) is int and
            plan["schema_version"] != PLAN_SCHEMA_VERSION):
        raise ValueError(
            f"Unsupported plan schema version {plan['schema_version']}. "
            "Regenerate the plan with the current CLI before applying it.")
    _exact(plan, {"schema_version", "generated_at", "write_policy", "patches"}, set(), "root")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != PLAN_SCHEMA_VERSION:
        _plan_error("schema_version", "unsupported version")
    if plan["write_policy"] not in (WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED):
        _plan_error("write_policy", "unsupported policy")
    if not isinstance(plan["generated_at"], str) or not plan["generated_at"]:
        _plan_error("generated_at", "must be nonempty")
    if not isinstance(plan["patches"], list): _plan_error("patches", "must be list")
    seen: set[int] = set()
    for index, patch in enumerate(plan["patches"]):
        path = f"patches[{index}]"
        _exact(patch, {"post_id", "post_title", "matches", "write", "diagnostics"}, {"source_modified"}, path)
        if not _positive_int(patch["post_id"]) or patch["post_id"] in seen:
            _plan_error(path + ".post_id", "must be a unique positive integer")
        seen.add(patch["post_id"])
        if not isinstance(patch["post_title"], str): _plan_error(path + ".post_title", "must be string")
        if "source_modified" in patch and patch["source_modified"] is not None and not isinstance(patch["source_modified"], str): _plan_error(path + ".source_modified", "must be string or null")
        _exact(patch["matches"], {"spotify", "lastfm"}, set(), path + ".matches")
        spotify = patch["matches"]["spotify"]
        _exact(spotify, {"id", "title", "artists", "score"}, set(), path + ".matches.spotify")
        if (not all(isinstance(spotify[k], str) and spotify[k] for k in ("id", "title")) or
                not isinstance(spotify["artists"], list) or not spotify["artists"] or
                not all(isinstance(x, str) and x for x in spotify["artists"]) or
                not _score_value(spotify["score"])): _plan_error(path + ".matches.spotify", "invalid evidence")
        lastfm = patch["matches"]["lastfm"]
        _exact(lastfm, {"title", "artist", "score"}, {"url", "mbid", "track_overlap"}, path + ".matches.lastfm")
        if (not all(isinstance(lastfm[k], str) and lastfm[k] for k in ("title", "artist")) or
                not _score_value(lastfm["score"])): _plan_error(path + ".matches.lastfm", "invalid evidence")
        if "track_overlap" in lastfm and not _score_value(lastfm["track_overlap"]): _plan_error(path + ".matches.lastfm.track_overlap", "invalid score")
        if "url" in lastfm and (not isinstance(lastfm["url"], str) or not _is_http_url(lastfm["url"])): _plan_error(path + ".matches.lastfm.url", "must be an HTTP(S) URL")
        if "mbid" in lastfm and (not isinstance(lastfm["mbid"], str) or not _UUID_RE.fullmatch(lastfm["mbid"])): _plan_error(path + ".matches.lastfm.mbid", "must be a valid MBID")
        write = patch["write"]
        _exact(write, set(), {"acf", "categories", "taxonomies"}, path + ".write")
        if not write: _plan_error(path + ".write", "must be nonempty")
        if "acf" in write:
            if not isinstance(write["acf"], dict) or not write["acf"]: _plan_error(path + ".write.acf", "must be nonempty object")
            for key, value in write["acf"].items():
                if key not in APPROVED_ACF_TYPES or type(value) is not APPROVED_ACF_TYPES[key]: _plan_error(path + ".write.acf." + key, "unknown or wrong type")
                if isinstance(value, str) and not value: _plan_error(path + ".write.acf." + key, "must be nonempty")
                if key == "lastfm_url" and not _is_http_url(value):
                    _plan_error(path + ".write.acf.lastfm_url", "must be an HTTP(S) URL")
                if key == "mbid" and not _UUID_RE.fullmatch(value):
                    _plan_error(path + ".write.acf.mbid", "must be a valid MBID")
                if key in ("music_release_date", "music_listened_at"):
                    try:
                        canonical_date = datetime.strptime(value, "%d/%m/%Y").strftime("%d/%m/%Y")
                    except ValueError:
                        canonical_date = ""
                    if canonical_date != value:
                        _plan_error(path + ".write.acf." + key, "must be a valid dd/mm/YYYY date")
            if "music_tracks" in write["acf"] and not write["acf"]["music_tracks"]:
                # Repeater replacement with an empty list would clear existing tracks.
                _plan_error(path + ".write.acf.music_tracks", "must be nonempty")
            for row in write["acf"].get("music_tracks", []):
                _exact(row, TRACK_KEYS, set(), path + ".write.acf.music_tracks[]")
                if (not all(_positive_int(row[k]) for k in ("disc_number", "track_number", "duration_ms")) or
                    type(row["highlight"]) is not bool or type(row["explicit"]) is not bool or
                    not all(isinstance(row[k], str) and row[k] for k in ("title", "spotify_id"))): _plan_error(path + ".write.acf.music_tracks[]", "invalid row")
        if "categories" in write:
            values = write["categories"]
            if not isinstance(values, list) or not values or not all(_positive_int(x) for x in values) or len(values) != len(set(values)): _plan_error(path + ".write.categories", "invalid replacement")
        if "taxonomies" in write:
            taxes = write["taxonomies"]
            if not isinstance(taxes, dict) or not taxes or set(taxes) - set(TAXONOMIES): _plan_error(path + ".write.taxonomies", "invalid object")
            for tax, names in taxes.items():
                if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names) or len(names) != len({match_key(n) for n in names}): _plan_error(path + ".write.taxonomies." + tax, "invalid names")
            if "release_type" in taxes and (len(taxes["release_type"]) != 1 or taxes["release_type"][0] not in RELEASE_TYPES): _plan_error(path + ".write.taxonomies.release_type", "invalid value")
        if not isinstance(patch["diagnostics"], list): _plan_error(path + ".diagnostics", "must be list")
        for diagnostic in patch["diagnostics"]:
            _exact(diagnostic, {"code", "message"}, set(), path + ".diagnostics[]")
            if diagnostic["code"] not in DIAGNOSTIC_CODES or not isinstance(diagnostic["message"], str) or not diagnostic["message"]: _plan_error(path + ".diagnostics[]", "invalid diagnostic")
    return plan


def _validate_provider_details(details: Any, code: str, path: str) -> None:
    required = {"provider", "operation", "failure_kind", "retryable",
                "attempts", "circuit_state"}
    _exact(details, required, {"http_status"}, path)
    provider = "spotify" if code == "spotify_provider_error" else "lastfm"
    if details["provider"] != provider:
        _plan_error(path + ".provider", "must agree with diagnostic code")
    if not isinstance(details["operation"], str) or not details["operation"]:
        _plan_error(path + ".operation", "must be nonempty")
    if details["failure_kind"] not in PROVIDER_FAILURE_KINDS:
        _plan_error(path + ".failure_kind", "unsupported value")
    if type(details["retryable"]) is not bool:
        _plan_error(path + ".retryable", "must be boolean")
    if type(details["attempts"]) is not int or details["attempts"] < 0:
        _plan_error(path + ".attempts", "must be a nonnegative integer")
    is_circuit = details["failure_kind"] == "circuit_open"
    if is_circuit != (details["attempts"] == 0):
        _plan_error(path + ".attempts", "zero is reserved for an open circuit")
    if details["circuit_state"] not in {"closed", "open"}:
        _plan_error(path + ".circuit_state", "unsupported value")
    if is_circuit and details["circuit_state"] != "open":
        _plan_error(path + ".circuit_state", "open circuit must report open")
    has_status = "http_status" in details
    if has_status != (details["failure_kind"] == "http_status"):
        _plan_error(path + ".http_status", "required only for HTTP status failures")
    if has_status and (type(details["http_status"]) is not int or
                       not 100 <= details["http_status"] <= 599):
        _plan_error(path + ".http_status", "must be an HTTP status integer")
    if has_status:
        expected_retryable = (details["http_status"] == 429 or
                              500 <= details["http_status"] <= 599)
        if details["retryable"] is not expected_retryable:
            _plan_error(path + ".retryable", "must agree with HTTP status")


def validate_ignored(value: Any) -> dict:
    _exact(value, {"schema_version", "ignored"}, set(), "root")
    validate_unresolved({"schema_version": value["schema_version"],
                         "unresolved": value["ignored"]})
    return value


def validate_unresolved(value: Any) -> dict:
    _exact(value, {"schema_version", "unresolved"}, set(), "root")
    if (type(value["schema_version"]) is not int or
            value["schema_version"] != UNRESOLVED_SCHEMA_VERSION):
        _plan_error("schema_version", "unsupported version")
    if not isinstance(value["unresolved"], list):
        _plan_error("unresolved", "must be list")
    seen = set()
    provider_codes = {"spotify_provider_error", "lastfm_provider_error"}
    for index, row in enumerate(value["unresolved"]):
        path = f"unresolved[{index}]"
        _exact(row, {"post_id", "post_title", "diagnostics"}, set(), path)
        if not _positive_int(row["post_id"]) or row["post_id"] in seen:
            _plan_error(path + ".post_id", "must be a unique positive integer")
        seen.add(row["post_id"])
        if not isinstance(row["post_title"], str):
            _plan_error(path + ".post_title", "must be string")
        if not isinstance(row["diagnostics"], list) or not row["diagnostics"]:
            _plan_error(path + ".diagnostics", "must be nonempty list")
        for diagnostic in row["diagnostics"]:
            code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
            required = {"code", "message", "details"} if code in provider_codes else {
                "code", "message"}
            _exact(diagnostic, required, set(), path + ".diagnostics[]")
            if (code not in DIAGNOSTIC_CODES or
                    not isinstance(diagnostic["message"], str) or not diagnostic["message"]):
                _plan_error(path + ".diagnostics[]", "invalid diagnostic")
            if code in provider_codes:
                _validate_provider_details(
                    diagnostic["details"], code, path + ".diagnostics[].details")
    return value


def slice_items(items: list, offset: int, limit: int | None) -> list:
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("offset and limit must be non-negative")
    return items[offset:] if limit is None else items[offset:offset + limit]


def materialize_body(write: dict, term_ids: dict[str, dict[str, int]]) -> dict:
    """Convert reviewed values to WordPress REST storage shapes."""
    body = {}
    if "acf" in write:
        body["acf"] = dict(write["acf"])
        for key in ("music_release_date", "music_listened_at"):
            if key in body["acf"]:
                body["acf"][key] = datetime.strptime(
                    body["acf"][key], "%d/%m/%Y").strftime("%Y%m%d")
    if "categories" in write: body["categories"] = list(write["categories"])
    for tax, names in write.get("taxonomies", {}).items():
        body[tax] = [term_ids[tax][match_key(name)] for name in names]
    return body


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #

def _resolve_terms(wp: Any, patches: list[dict]) -> dict[str, dict[str, int]]:
    wanted = {tax: {} for tax in TAXONOMIES}
    for patch in patches:
        for tax, names in patch["write"].get("taxonomies", {}).items():
            for name in names:
                wanted[tax].setdefault(match_key(name), name)
    resolved: dict[str, dict[str, int]] = {tax: {} for tax in TAXONOMIES}
    for tax in TAXONOMIES:
        existing = wp.list_tax_terms(tax)
        resolved[tax] = {match_key(name): term_id for name, term_id in existing.items()}
        for key, name in wanted[tax].items():
            if key not in resolved[tax]:
                term_id = wp.create_term(tax, name)
                if not term_id:
                    raise RuntimeError(f"Could not resolve taxonomy term {tax}/{name}")
                resolved[tax][key] = term_id
    return resolved


def apply_patches(wp: Any, patches: list[dict]) -> tuple[list[int], list[dict]]:
    term_ids = _resolve_terms(wp, patches)  # all resolution precedes the first post update
    succeeded, failed = [], []
    for patch in patches:
        try:
            wp.update_post(patch["post_id"], materialize_body(patch["write"], term_ids))
            succeeded.append(patch["post_id"])
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            failed.append({"post_id": patch["post_id"], "message": _safe_error(exc)})
    return succeeded, failed


def cmd_run(args, env) -> int:
    write_policy = (WRITE_OVERWRITE_MANAGED if getattr(args, "overwrite_managed", False)
                    else WRITE_FILL_ONLY)
    log.info("Planning write policy: %s", write_policy)
    if write_policy == WRITE_OVERWRITE_MANAGED:
        log.warning("Overwrite-managed mode: reviewed plans may replace existing managed data.")
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
    spt = Spotify(env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_CLIENT_SECRET"])
    lfm = LastFM(env["LASTFM_API_KEY"])
    tag_id_to_name: dict[int, str] = {}
    wp.list_tags(tag_id_to_name)
    release_type_terms = wp.list_tax_terms("release_type")
    planned, unresolved, ignored = [], [], []
    posts = slice_items(list(wp.list_posts(per_page=100)), args.offset, args.limit)
    for post in posts:
        result = enrich(post, spt, lfm, tag_id_to_name, write_policy,
                        release_type_terms=release_type_terms)
        if result is None:
            continue
        if "write" in result:
            planned.append(result)
        elif _is_true(result.get("ignored")):
            ignored.append({key: value for key, value in result.items() if key != "ignored"})
        else:
            unresolved.append(result)
    plan = {"schema_version": PLAN_SCHEMA_VERSION, "generated_at": _now_iso(),
            "write_policy": write_policy, "patches": planned}
    validate_plan(plan)
    out_dir = Path(args.out_dir)
    write_json_atomic(out_dir / "planned.json", plan)
    unresolved_file = validate_unresolved(
        {"schema_version": UNRESOLVED_SCHEMA_VERSION, "unresolved": unresolved})
    write_json_atomic(out_dir / "unresolved.json", unresolved_file)
    ignored_file = validate_ignored(
        {"schema_version": UNRESOLVED_SCHEMA_VERSION, "ignored": ignored})
    write_json_atomic(out_dir / "ignored.json", ignored_file)
    for client in (spt, lfm):
        circuit = getattr(client, "_circuit", None)
        if isinstance(circuit, ProviderCircuit) and circuit.request_counts:
            log.info("%s requests: %s", circuit.provider,
                     ", ".join(f"{name}={count}" for name, count in
                               sorted(circuit.request_counts.items())))
    circuit_states = [getattr(getattr(client, "_circuit", None), "is_open", False)
                      for client in (spt, lfm)]
    circuit_open = any(type(state) is bool and state for state in circuit_states)
    if args.apply and circuit_open:
        log.error("Provider circuit opened; refusing deprecated run --apply.")
        return 1
    if args.apply:
        log.warning("run --apply is deprecated; use apply-plan")
        succeeded, failed = apply_patches(wp, planned)
        write_json_atomic(out_dir / "applied.json", {
            "schema_version": PLAN_SCHEMA_VERSION, "plan": str(out_dir / "planned.json"),
            "applied_at": _now_iso(), "succeeded": succeeded, "failed": failed})
        return 1 if failed else 0
    return 1 if circuit_open else 0


def cmd_apply_plan(args, env) -> int:
    try:
        plan = validate_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read plan: {_safe_error(exc)}") from exc
    selected = slice_items(plan["patches"], args.offset, args.limit)
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
    succeeded, failed = apply_patches(wp, selected)
    out_dir = Path(args.out_dir) if args.out_dir else Path(args.plan).parent
    write_json_atomic(out_dir / "applied.json", {
        "schema_version": PLAN_SCHEMA_VERSION, "plan": str(args.plan), "applied_at": _now_iso(),
        "succeeded": succeeded, "failed": failed})
    return 1 if failed else 0


def cmd_stats(args, env) -> int:
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
    counts = {f: 0 for f in AUTO_FILLABLE_FIELDS}
    total_posts = 0
    fully_filled_posts = 0

    tax_term_present = {"artist": 0, "genre": 0, "release_type": 0}

    for post in wp.list_posts(per_page=100):
        total_posts += 1
        acf = post.get("acf") or {}
        post_filled = True
        for f in AUTO_FILLABLE_FIELDS:
            if is_field_present(f, acf.get(f)):
                counts[f] += 1
            else:
                post_filled = False
        if post_filled:
            fully_filled_posts += 1
        for tax in tax_term_present:
            if post.get(tax):
                tax_term_present[tax] += 1

    print(f"Total posts: {total_posts}")
    print(f"Fully filled: {fully_filled_posts}")
    print("Auto-fillable field fill count:")
    for f in AUTO_FILLABLE_FIELDS:
        print(f"  {f}: {counts[f]}")
    print("Posts with at least one term in each custom taxonomy:")
    for tax, n in tax_term_present.items():
        print(f"  {tax}: {n}")
    return 0


def cmd_fuzzy(args, env) -> int:
    spt = Spotify(env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_CLIENT_SECRET"])
    q_title = raw_query(args.title)
    q_artists = [raw_query(a) for a in args.artists if raw_query(a)]
    print(f"q_title={q_title!r}  q_artists={q_artists!r}")
    cands = search_ladder(spt, q_title, q_artists)
    for candidate in cands:
        row = spotify_candidate_score(candidate, q_title, q_artists)
        print(f"  score={row['score']:.3f} title={row['title_score']:.3f} "
              f"artist={row['artist_score']:.3f}  {candidate['id']}  "
              f"{candidate['name']!r}  by {[a['name'] for a in candidate.get('artists', [])]}")
    result = choose_spotify_candidate(cands, q_title, q_artists)
    print(f"\nResult: {result['reason']}; top pick: {result.get('candidate') or 'no winner'}")
    return 0


# --------------------------------------------------------------------------- #
# .env loader
# --------------------------------------------------------------------------- #

def load_env(path: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if path:
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD",
              "LASTFM_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        env.setdefault(k, os.environ.get(k, ""))
    return env


def require_env(env: dict[str, str], *names: str) -> dict[str, str]:
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    return env


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="post_to_album",
                                 description="Verbose Python CLI to backfill SCF music metadata "
                                             "from Spotify (album + tracks) and Last.fm (genre + mood).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--env", default=".env", help="path to .env (default: .env)")
    base.add_argument("--quiet", action="store_true")
    base.add_argument("--verbose", "-v", action="store_true")

    run = sub.add_parser("run", parents=[base], help="process posts and dry-run or apply")
    run.add_argument("--all", action="store_true", help="(default) process all posts")
    run.add_argument("--limit", type=int, help="process at most N posts")
    run.add_argument("--offset", type=int, default=0, help="skip first M posts")
    run.add_argument("--dry-run", action="store_true", help="dump planned patches to ./out/ (default)")
    run.add_argument("--apply", action="store_true", help="write to WordPress")
    run.add_argument("--out-dir", default="out", help="directory for dry-run JSON")
    run.add_argument(
        "--overwrite-managed", action="store_true",
        help=("recompute and plan replacements for program-managed ACF fields and "
              "taxonomies even when populated; editor-owned rating, favorite, notes, "
              "and track highlights remain protected, and missing provider values "
              "never clear existing data"))

    apply_plan = sub.add_parser("apply-plan", parents=[base], help="validate and apply a saved plan")
    apply_plan.add_argument("plan")
    apply_plan.add_argument("--offset", type=int, default=0)
    apply_plan.add_argument("--limit", type=int)
    apply_plan.add_argument("--out-dir")

    stats = sub.add_parser("stats", parents=[base], help="report fill-rate before/after")

    fuzzy = sub.add_parser("fuzzy", parents=[base], help="debug-search Spotify for a (title, artists…) pair")
    fuzzy.add_argument("title")
    fuzzy.add_argument("artists", nargs="*")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    env = load_env(args.env)
    wp_names = ("WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD")
    if args.cmd == "run":
        if not args.dry_run and not args.apply:
            args.dry_run = True
        if args.dry_run and args.apply:
            ap.error("--dry-run and --apply are mutually exclusive")
        return cmd_run(args, require_env(env, *wp_names, "SPOTIFY_CLIENT_ID",
                                         "SPOTIFY_CLIENT_SECRET", "LASTFM_API_KEY"))
    if args.cmd == "apply-plan":
        return cmd_apply_plan(args, require_env(env, *wp_names))
    if args.cmd == "stats":
        return cmd_stats(args, require_env(env, *wp_names))
    if args.cmd == "fuzzy":
        return cmd_fuzzy(args, require_env(env, "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"))
    ap.error("unknown subcommand")
    return 2


if __name__ == "__main__":
    sys.exit(main())
