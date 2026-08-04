"""Shared release enrichment and managed WordPress write policy."""

import logging
import re
from typing import Any

from album_metadata.common import (
    compute_release_type, match_key, post_dmy as _post_dmy, raw_query,
)
from album_metadata.lastfm import (
    _UUID_RE, _accept_stale_lastfm_tracks, choose_lastfm_candidate,
    lastfm_candidate_score, lookup_combined_lastfm, pick_top_tags,
    recover_lastfm_candidate, resolve_lastfm_mbid, resolve_lastfm_url,
    search_lastfm_candidates, validate_lastfm_info,
)
from album_metadata.providers import (
    LastFMProviderError, ProviderError, SpotifyProviderError,
)
from album_metadata.schema import (
    AUTO_FILLABLE_FIELDS, CATEGORY_MAP, LFM_BLOCKLIST, WRITE_FILL_ONLY,
    WRITE_OVERWRITE_MANAGED,
)
from album_metadata.spotify import (
    _release_title_base, _spotify_album_and_tracks, _spotify_tracks_complete,
    _spotify_tracks_market_restricted, choose_spotify_candidate,
    expected_release_type, recover_spotify_ambiguity, search_ladder,
    spotify_candidate_score, validate_spotify_album_tracks,
)

log = logging.getLogger("post_to_album")

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
