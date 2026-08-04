"""Last.fm client, matching, validation, recovery, and genre selection."""

import difflib
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
from typing import Any, Iterable

from album_metadata.common import match_key, raw_query, similarity
from album_metadata.providers import (
    LastFMProviderError, ProviderCircuit, ProviderError, _request_json,
)

log = logging.getLogger("post_to_album")
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
