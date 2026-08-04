"""Spotify client, search, matching, and release validation."""

import base64
import logging
import re
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any, cast

from album_metadata.common import match_key, similarity
from album_metadata.providers import (
    ProviderCircuit, ProviderError, SpotifyProviderError, _request_json,
)
from album_metadata.schema import CATEGORY_MAP, RELEASE_TYPES

log = logging.getLogger("post_to_album")
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"

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
