import io
import json
import unittest
from email.message import Message
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch
from typing import Any, cast

import post_to_album as mod


SPOTIFY = {"name": "Blue - Remastered", "artists": [{"name": "Beyoncé"}]}
CANDIDATE = {"name": "Blue - Remastered", "artist": "Beyoncé"}


class SearchAndMatchingTests(unittest.TestCase):
    def test_raw_and_comparison_preserve_edition_and_accents(self):
        self.assertEqual(mod.raw_query(" A &amp; B - Deluxe "), "A & B - Deluxe")
        self.assertEqual(mod.match_key("  CAFÉ\u0301  X "), mod.match_key("CAFÉ́ x"))
        self.assertNotEqual(mod.match_key("Blue"), mod.match_key("Blue - Remastered"))
        self.assertNotEqual(mod.match_key("Beyonce"), mod.match_key("Beyoncé"))
        self.assertEqual(mod.match_key("X’s — Live"), mod.match_key("X's - Live"))
        self.assertEqual(mod._release_title_similarity(
            "ĐỢI (Prod. RIO)", "ĐỢI (feat. WEAN) [SPECIAL VERSION]"), 1.0)
        self.assertEqual(mod._release_title_similarity(
            "Walking On A Dream", "Walking On A Dream (Special Edition)"), 1.0)

    def test_spotify_ladder_order_dedup_and_raw_values(self):
        class Fake:
            def __init__(self): self.queries = []
            def search_albums(self, q, limit=10):
                self.queries.append((q, limit))
                return [{"id": "same"}]
        fake = Fake()
        found = mod.search_ladder(fake, "A & B - EP", ["One", "Two"])
        self.assertEqual([q for q, _ in fake.queries], [
            'album:"A & B - EP" artist:"One"', "A & B - EP One Two"])
        self.assertEqual(len(found), 1)

    def test_spotify_ladder_stops_after_a_safe_match_and_falls_back_when_needed(self):
        candidate = {"id": "sid", "name": "Album", "artists": [{"name": "Artist"}]}

        class Fake:
            def __init__(self, first): self.first, self.queries = first, []
            def search_albums(self, q, limit=10):
                self.queries.append(q)
                return self.first if len(self.queries) == 1 else [candidate]

        immediate = Fake([candidate])
        self.assertEqual(mod.search_ladder(immediate, "Album", ["Artist"]), [candidate])
        self.assertEqual(len(immediate.queries), 1)

        fallback = Fake([])
        self.assertEqual(mod.search_ladder(fallback, "Album", ["Artist"]), [candidate])
        self.assertEqual(len(fallback.queries), 2)

        class BroadFailure:
            def __init__(self): self.queries = []
            def search_albums(self, q, limit=10):
                self.queries.append(q)
                return [
                    {"id": "one", "name": "Time", "artists": [{"name": "Artist"}]},
                    {"id": "two", "name": "Time", "artists": [{"name": "Artist"}]},
                ]

        ambiguous = BroadFailure()
        found = mod.search_ladder(ambiguous, "Time", ["Artist"])
        self.assertEqual(len(found), 2)
        self.assertEqual(len(ambiguous.queries), 2)
        self.assertNotIn("Time", ambiguous.queries)

    def test_spotify_provider_error_propagates(self):
        class Fake:
            def search_albums(self, q, limit=10): raise urllib.error.URLError("down")
        with self.assertRaises(urllib.error.URLError):
            mod.search_ladder(Fake(), "Album", ["Artist"])

    def test_spotify_rejects_malformed_token_and_api_shapes(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.value).encode()

        for payload in ([], {}, {"access_token": ""}, {"access_token": 7}):
            with self.subTest(payload=payload), patch(
                    "urllib.request.urlopen", return_value=Response(payload)):
                with self.assertRaises(mod.SpotifyProviderError):
                    mod.Spotify("id", "secret")._ensure_token()

        spotify = mod.Spotify("id", "secret")
        spotify._tok, spotify._exp = "token", float("inf")
        with patch("urllib.request.urlopen", return_value=Response([])):
            with self.assertRaisesRegex(mod.SpotifyProviderError, "malformed"):
                spotify._get("https://example.test")

    def test_spotify_search_distinguishes_empty_from_malformed_shapes(self):
        spotify = mod.Spotify("id", "secret")
        spotify._get = lambda url, operation=None: {"albums": {"items": []}}
        self.assertEqual(spotify.search_albums("Album"), [])

        malformed = [
            {}, {"albums": []}, {"albums": {}},
            {"albums": {"items": {}}},
            {"albums": {"items": ["bad"]}},
            {"albums": {"items": [{"id": "x", "name": "Album", "artists": ["bad"]}]}},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                spotify._get = lambda url, operation=None, value=payload: value
                with self.assertRaises(mod.SpotifyProviderError):
                    spotify.search_albums("Album")

    def test_enrich_maps_malformed_spotify_search_to_provider_error(self):
        spotify = mod.Spotify("id", "secret")
        spotify._get = lambda url, operation=None: {"albums": {"items": ["bad"]}}
        post = {"id": 1, "title": {"rendered": "Album"}, "date": "2020-01-01",
                "tags": [7], "acf": {}}
        result = cast(dict, mod.enrich(post, spotify, object(), {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_provider_error")

    def test_inclusive_provider_score_gates(self):
        spotify_candidate = {"id": "s", "name": "x", "artists": [{"name": "y"}]}
        lastfm_candidate = {"name": "x", "artist": "y"}
        cases = [
            (mod.choose_spotify_candidate, [spotify_candidate], "spotify_candidate_score",
             {"score": mod.SPOTIFY_MIN_SCORE, "title_score": mod.SPOTIFY_MIN_TITLE,
              "artist_score": mod.SPOTIFY_MIN_ARTIST, "candidate": spotify_candidate}),
            (mod.choose_lastfm_candidate, [lastfm_candidate], "lastfm_candidate_score",
             {"score": mod.LASTFM_MIN_SCORE, "title_score": mod.LASTFM_MIN_TITLE,
              "artist_score": mod.LASTFM_MIN_ARTIST, "candidate": lastfm_candidate}),
        ]
        for chooser, candidates, scorer, boundary in cases:
            with self.subTest(scorer=scorer), patch.object(mod, scorer, return_value=boundary):
                if chooser is mod.choose_spotify_candidate:
                    result = chooser(candidates, "not exact", ["artist"])
                else:
                    result = chooser(SPOTIFY, candidates)
                self.assertIs(result["candidate"], boundary["candidate"])

        # Each gate independently rejects a value immediately below its inclusive boundary.
        for scorer, chooser_value, base, field in [
            ("spotify_candidate_score", mod.choose_spotify_candidate,
             {"score": 1, "title_score": 1, "artist_score": 1,
              "candidate": spotify_candidate}, field)
            for field in ("title_score", "artist_score", "score")
        ] + [
            ("lastfm_candidate_score", mod.choose_lastfm_candidate,
             {"score": 1, "title_score": 1, "artist_score": 1,
              "candidate": lastfm_candidate}, field)
            for field in ("title_score", "artist_score", "score")
        ]:
            threshold = getattr(mod, ("SPOTIFY" if scorer.startswith("spotify") else "LASTFM") +
                                "_MIN_" + {"title_score": "TITLE", "artist_score": "ARTIST",
                                            "score": "SCORE"}[field])
            row = {**base, field: threshold - .001}
            chooser: Any = chooser_value
            with self.subTest(scorer=scorer, field=field), patch.object(mod, scorer, return_value=row):
                result = (chooser([base["candidate"]], "not exact", ["artist"])
                          if scorer.startswith("spotify") else chooser(SPOTIFY, [base["candidate"]]))
                self.assertIsNone(result["candidate"])

    def test_spotify_gates_missing_artist_and_ambiguity_boundary(self):
        c = {"id": "1", "name": "Album", "artists": [{"name": "Artist"}]}
        self.assertEqual(mod.choose_spotify_candidate([c], "Album", [])["reason"],
                         "spotify_missing_artist")
        c2 = {**c, "id": "2"}
        self.assertEqual(mod.choose_spotify_candidate([c, c2], "Album", ["Artist"])["reason"],
                         "spotify_ambiguous")
        edition = {**c, "id": "3", "name": "Album (Deluxe Edition)"}
        exact = mod.choose_spotify_candidate([edition, c], "Album", ["Artist"])
        self.assertEqual(exact["candidate"], c)
        with patch.object(mod, "spotify_candidate_score", side_effect=[
            {"score": .87, "title_score": .9, "artist_score": .8, "candidate": c},
            {"score": .82, "title_score": .9, "artist_score": .8, "candidate": c2},
        ]):
            # Exactly .05 is allowed: ambiguity is strictly less than the gap.
            self.assertEqual(mod.choose_spotify_candidate([c, c2], "x", ["y"])["candidate"], c)

    def test_release_type_evidence_requires_one_consistent_recognized_type(self):
        terms = {"Album": 30, "Single": 31, "Unknown": 32}
        cases = [
            ({"categories": [6]}, "Album"),
            ({"release_type": [31]}, "Single"),
            ({"categories": [6], "release_type": [30]}, "Album"),
            ({"categories": [6], "release_type": [31]}, None),
            ({"categories": [6], "release_type": [32]}, None),
            ({"categories": [6, 7]}, None),
            ({"release_type": [30, 31]}, None),
            ({"release_type": [30, 32]}, None),
            ({"categories": [999], "release_type": [32]}, None),
            ({"categories": None, "release_type": None}, None),
        ]
        for post, expected in cases:
            with self.subTest(post=post):
                self.assertEqual(mod.expected_release_type(post, terms), expected)

    def test_spotify_release_type_compatibility_boundaries(self):
        compatible = [
            ({"album_type": "album", "total_tracks": 7}, "Album"),
            ({"album_type": "single", "total_tracks": 1}, "Single"),
            ({"album_type": "single", "total_tracks": 3}, "Single"),
            ({"album_type": "album", "total_tracks": 4}, "EP"),
            ({"album_type": "single", "total_tracks": 6}, "EP"),
            ({"album_type": "compilation", "total_tracks": 12}, "Compilation"),
        ]
        incompatible = [
            ({"album_type": "album", "total_tracks": 6}, "Album"),
            ({"album_type": "single", "total_tracks": 4}, "Single"),
            ({"album_type": "album", "total_tracks": 3}, "EP"),
            ({"album_type": "compilation", "total_tracks": 12}, "Album"),
            ({"album_type": "album", "total_tracks": 12}, None),
            ({"album_type": None, "total_tracks": 12}, "Album"),
            ({"album_type": ["album"], "total_tracks": 12}, "Album"),
            ({"album_type": "album", "total_tracks": None}, "Album"),
            ({"album_type": "album", "total_tracks": "12"}, "Album"),
            ({"album_type": "album", "total_tracks": 12.0}, "Album"),
            ({"album_type": "album", "total_tracks": True}, "Album"),
            ({"album_type": "compilation"}, "Compilation"),
        ]
        for candidate, expected in compatible:
            with self.subTest(candidate=candidate, expected=expected):
                self.assertTrue(mod.spotify_release_type_compatible(candidate, expected))
        for candidate, expected in incompatible:
            with self.subTest(candidate=candidate, expected=expected):
                self.assertFalse(mod.spotify_release_type_compatible(candidate, expected))

    def test_what_aloha_means_ambiguity_uses_unique_album_type(self):
        album = {"id": "7qy8taDfUOJtA5fNE7BdbJ", "name": "What Aloha Means",
                 "artists": [{"name": "Kolohe Kai"}], "album_type": "album",
                 "total_tracks": 15}
        single = {"id": "4iwq22kCYJDHN8HB57KZ6f", "name": "What Aloha Means",
                  "artists": [{"name": "Kolohe Kai"}], "album_type": "single",
                  "total_tracks": 1}
        self.assertIs(mod.choose_spotify_candidate(
            [single, album], "What Aloha Means", ["Kolohe Kai"], "Album")["candidate"], album)
        self.assertEqual(mod.choose_spotify_candidate(
            [single, album], "What Aloha Means", ["Kolohe Kai"])["reason"], "spotify_ambiguous")

    def test_release_type_never_overrides_safe_text_or_non_unique_type(self):
        album = {"id": "album", "album_type": "album", "total_tracks": 10}
        single = {"id": "single", "album_type": "single", "total_tracks": 1}
        rows = [
            {"score": .90, "title_score": .9, "artist_score": .9, "candidate": album},
            {"score": .84, "title_score": .9, "artist_score": .9, "candidate": single},
        ]
        with patch.object(mod, "spotify_candidate_score", side_effect=rows):
            self.assertIs(mod.choose_spotify_candidate(
                [album, single], "x", ["y"], "Single")["candidate"], album)
        second_album = {"id": "album-2", "album_type": "album", "total_tracks": 12}
        with patch.object(mod, "spotify_candidate_score", side_effect=[
            {**rows[0], "candidate": album}, {**rows[0], "candidate": second_album},
        ]):
            self.assertEqual(mod.choose_spotify_candidate(
                [album, second_album], "x", ["y"], "Album")["reason"], "spotify_ambiguous")

    def test_spotify_ambiguity_recovery_is_corroborated_and_order_independent(self):
        def album(album_id, popularity=10, tracks=2, **changes):
            value = {"id": album_id, "name": "Album (Deluxe)",
                     "artists": [{"name": "Artist"}], "album_type": "album",
                     "total_tracks": tracks, "release_date": "2020-01-01",
                     "release_date_precision": "day", "label": "Label",
                     "popularity": popularity, "is_playable": True,
                     "external_urls": {"spotify": "https://open.spotify.com/album/" + album_id}}
            value.update(changes)
            return value

        def tracks(album_id, count=2):
            return [{"id": (album_id[:20] + f"{number:02d}")[-22:], "name": f"Track {number}",
                     "artists": [{"name": "Artist"}], "disc_number": 1,
                     "track_number": number, "duration_ms": 1000 + number,
                     "explicit": False, "is_playable": True}
                    for number in range(1, count + 1)]

        class Spotify:
            def __init__(self, albums):
                self.albums = {item["id"]: item for item in albums}
                self.calls = []
            def album(self, album_id):
                self.calls.append(("album", album_id))
                return self.albums[album_id]
            def all_tracks(self, album_id):
                self.calls.append(("tracks", album_id))
                return tracks(album_id, self.albums[album_id]["total_tracks"])

        old_id, new_id = "A" * 22, "B" * 22
        old, new = album(old_id, 1), album(new_id, 99)
        rows = [mod.spotify_candidate_score(item, "Album (Deluxe)", ["Artist"])
                for item in (new, old)]
        stored = tracks(old_id)
        post = {"acf": {"spotify_album_id": old_id, "music_tracks": [
            {"spotify_id": track["id"]} for track in stored]}}
        result = mod.recover_spotify_ambiguity(
            Spotify([old, new]), post, rows, "Album (Deluxe)", ["Artist"], None)
        self.assertEqual(result["candidate"]["id"], old_id)
        self.assertEqual(result["selection_evidence"], "existing_id_tracks")

        # Without corroboration, popularity is late and independent of input order.
        for ordered in (rows, list(reversed(rows))):
            result = mod.recover_spotify_ambiguity(
                Spotify([old, new]), {"acf": {}}, ordered,
                "Album (Deluxe)", ["Artist"], None)
            self.assertEqual(result["candidate"]["id"], new_id)
            self.assertEqual(result["selection_evidence"], "unique_popularity")

        # Stored corroboration is exact, complete, ordered, and supports albums
        # whose track list required more than one Spotify page.
        large = album(old_id, tracks=51)
        large_tracks = tracks(old_id, 51)
        large_post = {"acf": {"spotify_album_id": old_id, "music_tracks": [
            {"spotify_id": track["id"]} for track in large_tracks]}}
        large_rows = [mod.spotify_candidate_score(item, "Album (Deluxe)", ["Artist"])
                      for item in (large, new)]
        self.assertEqual(mod.recover_spotify_ambiguity(
            Spotify([large, new]), large_post, large_rows,
            "Album (Deluxe)", ["Artist"], None)["selection_evidence"],
            "existing_id_tracks")
        for damaged in (list(reversed(large_tracks)), large_tracks[:-1],
                        large_tracks[:-1] + [large_tracks[0]]):
            with self.subTest(stored_track_damage=len(damaged)):
                bad_post = {"acf": {"spotify_album_id": old_id, "music_tracks": [
                    {"spotify_id": track["id"]} for track in damaged]}}
                result = mod.recover_spotify_ambiguity(
                    Spotify([large, new]), bad_post, large_rows,
                    "Album (Deluxe)", ["Artist"], None)
                self.assertNotEqual(result.get("selection_evidence"), "existing_id_tracks")

    def test_spotify_full_evidence_requires_matching_id_and_complete_pagination(self):
        album_id = "A" * 22
        candidate = {"id": album_id}

        class Spotify:
            def __init__(self, album, tracks): self.value = album, tracks
            def album(self, album_id): return self.value[0]
            def all_tracks(self, album_id): return self.value[1]

        track = {"id": "T" * 22, "name": "Track", "duration_ms": 1000,
                 "disc_number": 1, "track_number": 1, "explicit": False}
        base = {"id": album_id, "name": "Album", "artists": [{"name": "Artist"}],
                "total_tracks": 1}
        for changes, rows, valid in (({}, [track], True),
                                     ({"id": "B" * 22}, [track], False),
                                     ({"total_tracks": 2}, [track], False)):
            with self.subTest(changes=changes):
                evidence = mod.spotify_full_evidence(
                    Spotify({**base, **changes}, rows), candidate)
                self.assertIs(evidence["valid"], valid)
        for total in (True, "1"):
            with self.subTest(total_tracks=total), self.assertRaises(mod.SpotifyProviderError):
                mod.spotify_full_evidence(
                    Spotify({**base, "total_tracks": total}, [track]), candidate)

    def test_spotify_recovery_keeps_404_and_malformed_contenders_ambiguous(self):
        def album(album_id):
            return {"id": album_id, "name": "Album", "artists": [{"name": "Artist"}],
                    "album_type": "album", "total_tracks": 1, "is_playable": True,
                    "external_urls": {"spotify": "https://spotify.test/" + album_id},
                    "popularity": 9}
        a, b = "A" * 22, "B" * 22
        rows = [mod.spotify_candidate_score(album(key), "Album", ["Artist"])
                for key in (a, b)]

        class Spotify:
            def __init__(self, failure): self.failure = failure
            def album(self, album_id):
                if album_id == a and self.failure == "404":
                    raise mod.SpotifyProviderError(
                        "gone", failure_kind="http_status", http_status=404)
                value = album(album_id)
                if album_id == a and self.failure == "mismatch": value["id"] = b
                return value
            def all_tracks(self, album_id):
                return [{"id": album_id, "name": "Song", "artists": [{"name": "Artist"}],
                         "disc_number": 1, "track_number": 1, "duration_ms": 1,
                         "explicit": False, "is_playable": True}]

        stored = {"acf": {"spotify_album_id": a, "music_tracks": [{"spotify_id": a}]}}
        for failure in ("404", "mismatch"):
            with self.subTest(failure=failure):
                result = mod.recover_spotify_ambiguity(
                    Spotify(failure), stored, rows, "Album", ["Artist"], None)
                self.assertEqual(result, {"candidate": None, "reason": "spotify_ambiguous"})

    def test_spotify_recovery_rejects_bad_stored_rows_and_unsafe_popularity(self):
        self.assertIsNone(mod.stored_spotify_track_ids({"acf": {"music_tracks": []}}))
        self.assertIsNone(mod.stored_spotify_track_ids({"acf": {"music_tracks": [
            {"spotify_id": "short"}]}}))
        self.assertIsNone(mod.stored_spotify_track_ids({"acf": {"music_tracks": [
            {"spotify_id": "A" * 22}, {}]}}))
        self.assertIsNone(mod.stored_spotify_track_ids({"acf": {"music_tracks": [
            {"spotify_id": "A" * 22}, {"spotify_id": "A" * 22}]}}))

        def full(album_id, popularity, count=1):
            album = {"id": album_id, "name": "Album", "artists": [{"name": "Artist"}],
                     "album_type": "album", "total_tracks": count,
                     "release_date": "2020", "release_date_precision": "year",
                     "label": "Label", "popularity": popularity, "is_playable": True,
                     "external_urls": {"spotify": "https://spotify.test/" + album_id}}
            tracks = [{"id": album_id[:-2] + f"{number:02d}",
                       "name": f"Song {number}", "artists": [{"name": "Artist"}],
                       "disc_number": 1, "track_number": number, "duration_ms": number,
                       "explicit": False, "is_playable": True}
                      for number in range(1, count + 1)]
            return album, tracks

        class Spotify:
            def __init__(self, values): self.values = values
            def album(self, album_id): return self.values[album_id][0]
            def all_tracks(self, album_id): return self.values[album_id][1]

        a, b = "A" * 22, "B" * 22
        for popularities in ((7, 7), (7, "bad"), (True, 7)):
            values = {key: full(key, popularity) for key, popularity in zip((a, b), popularities)}
            rows = [mod.spotify_candidate_score(values[key][0], "Album", ["Artist"])
                    for key in (a, b)]
            result = mod.recover_spotify_ambiguity(
                Spotify(values), {"acf": {}}, rows, "Album", ["Artist"], None)
            # Complete identical fingerprints (popularity excluded) use lexical ID only.
            self.assertEqual(result["candidate"]["id"], a)
            self.assertEqual(result["selection_evidence"], "equivalent_id")

        values = {a: full(a, 7, 1), b: full(b, 7, 2)}
        rows = [mod.spotify_candidate_score(values[key][0], "Album", ["Artist"])
                for key in (a, b)]
        result = mod.recover_spotify_ambiguity(
            Spotify(values), {"acf": {}}, rows, "Album", ["Artist"], None)
        self.assertEqual(result["reason"], "spotify_ambiguous")
        self.assertIsNone(result["candidate"])

        # Unique type evidence cannot bypass the later public eligibility gate.
        values = {a: full(a, 7, 7), b: full(b, 7, 1)}
        values[a][0]["is_playable"] = False
        values[b][0]["album_type"] = "single"
        rows = [mod.spotify_candidate_score(values[key][0], "Album", ["Artist"])
                for key in (a, b)]
        result = mod.recover_spotify_ambiguity(
            Spotify(values), {"acf": {}}, rows, "Album", ["Artist"], "Album")
        self.assertEqual(result["reason"], "spotify_ambiguous")

    def test_enrich_only_orchestrates_recovery_for_ambiguity(self):
        post = {"id": 1, "title": {"rendered": "Album"}, "date": "2020-01-01",
                "tags": [7], "acf": {}}
        low = {"id": "A" * 22, "name": "Wrong", "artists": [{"name": "Other"}]}
        with patch.object(mod, "search_ladder", return_value=[low]), \
                patch.object(mod, "recover_spotify_ambiguity") as recover:
            result = cast(dict, mod.enrich(post, object(), object(), {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_catalog_unavailable")
        self.assertTrue(result["ignored"])
        recover.assert_not_called()

        tied = [{"id": value * 22, "name": "Album", "artists": [{"name": "Artist"}]}
                for value in ("A", "B")]
        unresolved = {"candidate": None, "reason": "spotify_ambiguous"}
        with patch.object(mod, "search_ladder", return_value=tied), \
                patch.object(mod, "recover_spotify_ambiguity", return_value=unresolved) as recover:
            result = cast(dict, mod.enrich(post, object(), object(), {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_ambiguous")
        recover.assert_called_once()

    def test_spotify_recovery_fetch_failure_and_low_confidence_are_safe(self):
        album_id = "A" * 22
        candidate = {"id": album_id, "name": "Album", "artists": [{"name": "Artist"}]}
        row = mod.spotify_candidate_score(candidate, "Album", ["Artist"])
        class Broken:
            def album(self, album_id):
                raise mod.SpotifyProviderError("down", failure_kind="network", retryable=True)
            def all_tracks(self, album_id): raise AssertionError("not reached")
        with self.assertRaises(mod.SpotifyProviderError):
            mod.recover_spotify_ambiguity(
                Broken(), {"acf": {}}, [row], "Album", ["Artist"], None)
        for post_id, title, artist in ((2157, "Night of the Living Junkies", "Kendrick Lamar"),
                                       (1885, "Loveless", "my bloody valentine"),
                                       (1494, "Acid Mt. Fuji", "Susumu Yokota"),
                                       (1419, "Walking On A Dream", "Empire of the Sun")):
            with self.subTest(post_id=post_id):
                bad = {"id": str(post_id) * 6, "name": title,
                       "artists": [{"name": "Wrong Artist"}]}
                self.assertEqual(mod.choose_spotify_candidate(
                    [bad], title, [artist])["reason"], "spotify_low_confidence")

    def test_spotify_album_reuses_embedded_tracks_and_follows_next_page(self):
        first_track = {"id": "one"}
        second_track = {"id": "two"}
        album = {"id": "aid", "tracks": {"items": [first_track],
                                             "next": "https://next.test"}}
        spotify = mod.Spotify("id", "secret")
        with patch.object(spotify, "_get", side_effect=[
                album, {"items": [second_track], "next": None}]) as get:
            found_album, tracks = spotify.album_with_tracks("aid")
        self.assertIs(found_album, album)
        self.assertEqual(tracks, [first_track, second_track])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].args, ("https://next.test", "track.list"))

        album["tracks"] = {"items": [first_track], "next": None}
        with patch.object(spotify, "_get", return_value=album) as get:
            _, tracks = spotify.album_with_tracks("aid")
        self.assertEqual(tracks, [first_track])
        get.assert_called_once()

    def test_lastfm_get_user_agent_api_error_and_malformed(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.value
        seen = []
        def open_api(req, timeout):
            seen.append(req)
            return Response(b'{"error": 6, "message": "bad"}')
        with patch("urllib.request.urlopen", open_api):
            with self.assertRaisesRegex(mod.LastFMProviderError, "API error 6"):
                mod.LastFM("key")._get("album.search", album="x")
        self.assertIn("wordpress-album-metadata-filler", seen[0].get_header("User-agent"))
        with patch("urllib.request.urlopen", return_value=Response(b"not json")):
            with self.assertRaisesRegex(mod.LastFMProviderError, "malformed JSON"):
                mod.LastFM("key")._get("album.search", album="x")

    def test_provider_retry_recovers_502_and_timeouts(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.value).encode()

        errors = [urllib.error.HTTPError("secret-url", 502, "body", Message(), None)
                  for _ in range(2)]
        spotify = mod.Spotify("id", "secret")
        spotify._tok, spotify._exp = "token", float("inf")
        with patch("urllib.request.urlopen", side_effect=[*errors, Response({"ok": True})]) as opened, \
             patch("time.sleep") as sleep:
            self.assertEqual(spotify._get("https://example.test"), {"ok": True})
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertFalse(spotify._circuit.is_open)

        spotify = mod.Spotify("id", "secret")
        with patch("urllib.request.urlopen", side_effect=[TimeoutError(),
                                                          Response({"access_token": "token"})]), \
             patch("time.sleep") as sleep:
            self.assertEqual(spotify._ensure_token(), "token")
        sleep.assert_called_once_with(1)

        lfm = mod.LastFM("key")
        with patch("urllib.request.urlopen", side_effect=[TimeoutError(), Response({"ok": True})]), \
             patch("time.sleep") as sleep:
            self.assertEqual(lfm._get("album.search", album="x"), {"ok": True})
        sleep.assert_called_once_with(1)

    def test_direct_oserror_retries_and_counts_toward_circuit(self):
        class BrokenResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): raise ConnectionResetError("connection lost")

        circuit = mod.ProviderCircuit("lastfm")
        lfm = mod.LastFM("key", circuit)
        with patch("urllib.request.urlopen", return_value=BrokenResponse()) as opened, \
             patch("time.sleep") as sleep:
            with self.assertRaises(mod.LastFMProviderError) as caught:
                lfm._get("album.search", album="x")
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(caught.exception.failure_kind, "network")
        self.assertEqual(caught.exception.attempts, 3)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(circuit.consecutive_failures, 1)

        for expected_failures in (2, 3):
            with patch("urllib.request.urlopen",
                       side_effect=ConnectionAbortedError("connection lost")), \
                 patch("time.sleep"):
                with self.assertRaises(mod.LastFMProviderError):
                    lfm._get("album.search", album="x")
            self.assertEqual(circuit.consecutive_failures, expected_failures)
        self.assertTrue(circuit.is_open)

    def test_provider_retry_exhaustion_and_nonretryable_failures(self):
        errors = [urllib.error.HTTPError("url", 503, "bad", Message(), None)
                  for _ in range(3)]
        with patch("urllib.request.urlopen", side_effect=errors) as opened, \
             patch("time.sleep") as sleep:
            with self.assertRaises(mod.LastFMProviderError) as caught:
                mod.LastFM("key")._get("album.search", album="x")
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertTrue(caught.exception.retryable)

        error = urllib.error.HTTPError("url", 400, "bad", Message(), None)
        with patch("urllib.request.urlopen", side_effect=error) as opened, \
             patch("time.sleep") as sleep:
            with self.assertRaises(mod.LastFMProviderError) as caught:
                mod.LastFM("key")._get("album.search", album="x")
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(caught.exception.retryable)

    def test_provider_retry_after_401_and_circuit_are_deterministic(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.value).encode()

        headers = Message(); headers["Retry-After"] = "99"
        error = urllib.error.HTTPError("url", 429, "bad", headers, None)
        spotify = mod.Spotify("id", "secret")
        spotify._tok, spotify._exp = "token", float("inf")
        with patch("urllib.request.urlopen", side_effect=[error, Response({"ok": True})]), \
             patch("time.sleep") as sleep:
            self.assertEqual(spotify._get("https://example.test"), {"ok": True})
        sleep.assert_called_once_with(99)

        unauthorized = urllib.error.HTTPError("url", 401, "bad", Message(), None)
        spotify = mod.Spotify("id", "secret")
        spotify._tok, spotify._exp = "old", float("inf")
        with patch("urllib.request.urlopen", side_effect=[
                unauthorized, Response({"access_token": "new"}), Response({"ok": True})]) as opened, \
             patch("time.sleep") as sleep:
            self.assertEqual(spotify._get("https://example.test"), {"ok": True})
        self.assertEqual(opened.call_count, 3)
        sleep.assert_not_called()

        circuit = mod.ProviderCircuit("lastfm")
        lfm = mod.LastFM("key", circuit)
        errors = [urllib.error.HTTPError("secret-key-url", 502, "secret-body", Message(), None)
                  for _ in range(9)]
        with patch("urllib.request.urlopen", side_effect=errors) as opened, \
             patch("time.sleep"):
            for _ in range(3):
                with self.assertRaises(mod.LastFMProviderError):
                    lfm._get("album.search", album="x")
            with self.assertRaises(mod.LastFMProviderError) as caught:
                lfm._get("album.search", album="x")
        self.assertEqual(opened.call_count, 9)
        self.assertEqual(caught.exception.failure_kind, "circuit_open")
        self.assertEqual(caught.exception.attempts, 0)
        diagnostic = caught.exception.diagnostic("lastfm_provider_error")
        self.assertNotIn("secret-key-url", json.dumps(diagnostic))
        self.assertNotIn("secret-body", json.dumps(diagnostic))

    def test_provider_circuits_reset_independently_and_diagnostics_redact(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"ok": true}'

        spotify_circuit = mod.ProviderCircuit("spotify")
        lastfm_circuit = mod.ProviderCircuit("lastfm")
        spotify_circuit.consecutive_failures = 2
        spotify = mod.Spotify("id", "secret", spotify_circuit)
        spotify._tok, spotify._exp = "token", float("inf")
        with patch("urllib.request.urlopen", return_value=Response()):
            self.assertEqual(spotify._get("https://example.test"), {"ok": True})
        self.assertEqual(spotify_circuit.consecutive_failures, 0)
        self.assertEqual(lastfm_circuit.consecutive_failures, 0)

        lastfm_circuit.is_open = True
        with patch("urllib.request.urlopen") as opened, self.assertRaises(
                mod.LastFMProviderError) as caught:
            mod.LastFM("key", lastfm_circuit)._get("album.search", album="x")
        opened.assert_not_called()
        self.assertEqual(caught.exception.attempts, 0)
        self.assertFalse(spotify_circuit.is_open)

        post = {"id": 1, "title": {"rendered": "Album"}}
        secret = "https://user:password@example.test/?api_key=secret"
        row = mod._provider_unresolved(
            post, "lastfm_provider_error", RuntimeError(secret), "album.search")
        self.assertNotIn(secret, json.dumps(row))
        self.assertEqual(row["diagnostics"][0]["details"]["failure_kind"], "unexpected")

    def test_quota_exceeded_opens_circuit_without_sleep_or_retry(self):
        headers = Message(); headers["Retry-After"] = "39851"
        body = io.BytesIO(b'{"error":{"status":429,"reason":"QUOTA_EXCEEDED"}}')
        error = urllib.error.HTTPError("secret-url", 429, "bad", headers, body)
        spotify = mod.Spotify("id", "secret")
        spotify._tok, spotify._exp = "token", float("inf")
        with patch("urllib.request.urlopen", side_effect=error) as opened, \
             patch("time.sleep") as sleep, patch("time.time", return_value=100.0):
            with self.assertRaises(mod.SpotifyProviderError) as caught:
                spotify._get("https://example.test", "album.get")
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.circuit_state, "open")
        self.assertTrue(spotify._circuit.is_open)
        self.assertEqual(spotify._circuit.blocked_until, 39951.0)
        self.assertEqual(spotify._circuit.request_counts, {"album.get": 1})

        with patch("urllib.request.urlopen") as opened, self.assertRaises(
                mod.SpotifyProviderError) as blocked:
            spotify._get("https://example.test", "album.get")
        opened.assert_not_called()
        self.assertEqual(blocked.exception.failure_kind, "circuit_open")

    def test_retry_after_malformed_uses_normal_delay(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"ok": true}'

        headers = Message(); headers["Retry-After"] = "not-an-integer"
        error = urllib.error.HTTPError("url", 429, "bad", headers, None)
        lfm = mod.LastFM("key")
        with patch("urllib.request.urlopen", side_effect=[error, Response()]), \
             patch("time.sleep") as sleep:
            self.assertEqual(lfm._get("album.search", album="x"), {"ok": True})
        sleep.assert_called_once_with(1)

    def test_lastfm_search_empty_singleton_and_bad_shape(self):
        lfm = mod.LastFM("key")
        lfm._get = lambda *a, **k: {"results": {"albummatches": {"album": []}}}
        self.assertEqual(lfm.album_search("x"), [])
        lfm._get = lambda *a, **k: {"results": {"albummatches": {}}}
        self.assertEqual(lfm.album_search("x"), [])
        lfm._get = lambda *a, **k: {"results": {"albummatches": {"album": {"name": "x"}}}}
        self.assertEqual(lfm.album_search("x"), [{"name": "x"}])
        lfm._get = lambda *a, **k: {}
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            lfm.album_search("x")

    def test_lastfm_candidate_search_dedupes_only_across_queries(self):
        same = {"name": "Palette", "artist": "Didier Armeni", "mbid": "ABC",
                "url": "https://last.fm/palette"}
        different_mbid = {**same, "mbid": "DEF"}
        different_url = {**same, "url": "https://last.fm/palette-two"}
        repeated = {"name": " palette ", "artist": "DIDIER ARMENI", "mbid": "abc",
                    "url": " https://last.fm/palette "}

        class Fake:
            def __init__(self): self.queries = []
            def album_search(self, query, limit=10):
                self.queries.append((query, limit))
                if query == "Palette Didier Armeni":
                    return [same, same]
                return [repeated, different_mbid, different_url]

        fake = Fake()
        found = mod.search_lastfm_candidates(
            fake, {"name": "Palette", "artists": [{"name": "Didier Armeni"}]})

        self.assertEqual(fake.queries, [("Palette Didier Armeni", 10), ("Palette", 10)])
        self.assertEqual(found, [same, same, different_mbid, different_url])

    def test_getinfo_unwraps_and_uses_autocorrect_zero(self):
        lfm = mod.LastFM("key")
        calls = []
        lfm._get = lambda method, **kw: calls.append((method, kw)) or {"album": {"name": "x"}}
        self.assertEqual(lfm.album_getinfo(artist="a", album="x"), {"name": "x"})
        self.assertEqual(calls[0][1]["autocorrect"], 0)
        lfm.album_getinfo(mbid="id")
        self.assertEqual(calls[1][1], {"mbid": "id"})

    def test_top_tag_clients_use_provider_methods_and_unwrap_toptags(self):
        lfm = mod.LastFM("key")
        calls = []
        lfm._get = lambda method, **kw: calls.append((method, kw)) or {
            "toptags": {"tag": [{"name": "Rock"}]}}

        expected = {"toptags": {"tag": [{"name": "Rock"}]}}
        self.assertEqual(lfm.album_gettoptags(artist="a", album="x"), expected)
        self.assertEqual(lfm.album_gettoptags(mbid="id"), expected)
        self.assertEqual(lfm.artist_gettoptags("a"), expected)
        self.assertEqual(calls, [
            ("album.getTopTags", {"artist": "a", "album": "x", "autocorrect": 0}),
            ("album.getTopTags", {"mbid": "id"}),
            ("artist.getTopTags", {"artist": "a", "autocorrect": 0}),
        ])

    def test_lastfm_identity_resolvers_precedence_fallback_and_invalid_values(self):
        info_mbid = "123e4567-e89b-12d3-a456-426614174000"
        selected_mbid = "123e4567-e89b-12d3-a456-426614174001"
        mbid_cases = (
            ({"mbid": f" {info_mbid} "}, {"mbid": selected_mbid}, info_mbid),
            ({"mbid": ""}, {"mbid": selected_mbid}, selected_mbid),
            ({}, {"mbid": selected_mbid}, selected_mbid),
            ({"mbid": "invalid"}, {"mbid": selected_mbid}, selected_mbid),
            ({}, {}, None),
            ({"mbid": "invalid"}, {"mbid": "also-invalid"}, None),
        )
        for info, selected, expected in mbid_cases:
            with self.subTest(info=info, selected=selected):
                self.assertEqual(mod.resolve_lastfm_mbid(info, selected), expected)

        selected_url = "http://last.fm/selected"
        url_cases = (
            ({"url": " https://last.fm/info "}, {"url": selected_url},
             "https://last.fm/info"),
            ({}, {"url": selected_url}, selected_url),
            ({"url": "ftp://last.fm/info"}, {"url": selected_url}, selected_url),
            ({"url": "//last.fm/info"}, {"url": 1}, None),
        )
        for info, selected, expected in url_cases:
            with self.subTest(info=info, selected=selected):
                self.assertEqual(mod.resolve_lastfm_url(info, selected), expected)

    def test_lastfm_exact_and_mbid_ambiguity(self):
        self.assertEqual(mod.choose_lastfm_candidate(SPOTIFY, [CANDIDATE])["reason"], "lastfm_exact")
        duplicate = dict(CANDIDATE)
        self.assertEqual(mod.choose_lastfm_candidate(SPOTIFY, [CANDIDATE, duplicate])["reason"],
                         "lastfm_ambiguous_exact")
        mbid = "123e4567-e89b-12d3-a456-426614174000"
        pinned = {**CANDIDATE, "mbid": mbid}
        self.assertEqual(mod.choose_lastfm_candidate(SPOTIFY, [pinned, duplicate])["candidate"], pinned)

        spotify = {"name": "Album", "artists": [{"name": "alt-J"}]}
        hyphen_alias = {"name": "Album", "artist": "alt‐J", "mbid": "a" * 36}
        canonical = {"name": "Album", "artist": "alt-J", "mbid": "b" * 36}
        chosen = mod.choose_lastfm_candidate(spotify, [hyphen_alias, canonical])
        self.assertEqual(chosen["candidate"], canonical)
        self.assertEqual(chosen["reason"], "lastfm_exact_punctuation")

    def test_lastfm_fuzzy_ambiguity_exact_boundary(self):
        candidates = [{"name": "a"}, {"name": "b"}]
        for gap, reason in ((mod.LASTFM_MAX_TIE_GAP - .001, "lastfm_ambiguous"),
                            (mod.LASTFM_MAX_TIE_GAP, "lastfm_fuzzy")):
            with self.subTest(gap=gap), patch.object(mod, "lastfm_candidate_score", side_effect=[
                {"score": .90, "title_score": .9, "artist_score": .9, "candidate": candidates[0]},
                {"score": .90 - gap, "title_score": .9, "artist_score": .9,
                 "candidate": candidates[1]},
            ]):
                result = mod.choose_lastfm_candidate(SPOTIFY, candidates)
            self.assertEqual(result["reason"], reason)

    def test_validation_identity_tracks_and_boundaries(self):
        tracks = [{"name": "One"}, {"name": "Two"}, {"name": "Three"}, {"name": "Four"}, {"name": "Five"}]
        info = {**CANDIDATE, "tracks": {"track": []}}
        self.assertTrue(mod.validate_lastfm_info(SPOTIFY, tracks, CANDIDATE, info)["accepted"])
        info["tracks"] = {"track": [{"name": x} for x in ["One", "Two", "Three", "x", "y"]]}
        self.assertTrue(mod.validate_lastfm_info(SPOTIFY, tracks, CANDIDATE, info)["accepted"])
        info["tracks"] = {"track": [{"name": x} for x in ["One", "Two", "x", "y", "z"]]}
        self.assertEqual(mod.validate_lastfm_info(SPOTIFY, tracks, CANDIDATE, info)["reason"],
                         "lastfm_track_contradiction")
        wrong = {**info, "artist": "Someone Else", "tracks": {}}
        self.assertEqual(mod.validate_lastfm_info(SPOTIFY, tracks, CANDIDATE, wrong)["reason"],
                         "lastfm_identity_changed")

    def test_track_overlap_matches_provider_suffixes_without_stripping_them(self):
        base = ["Hotel California", "Heartache Tonight", "I Can't Tell You Why",
                "The Long Run", "New Kid in Town"]
        spotify = [{"name": f"{title} - Live; 1999 Remaster"} for title in base]
        info = {**CANDIDATE, "tracks": {"track": [{"name": title} for title in base]}}
        result = mod.validate_lastfm_info(SPOTIFY, spotify, CANDIDATE, info)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["overlap"], 1.0)

    def test_track_similarity_accepts_punctuation_not_word_containment(self):
        accepted = (("Hotel California", "Hotel California - Live; 1999 Remaster"),
                    ("Hotel California", "Hotel California (Remastered)"),
                    ("Don't", "Dont"))
        rejected = (("Love", "Love Me"), ("One", "One More"),
                    ("Part I", "Part II"), ("Chapter 1", "Chapter 10"))
        for titles in accepted:
            with self.subTest(titles=titles):
                self.assertGreaterEqual(mod._track_similarity(*titles),
                                        mod.LASTFM_MIN_TRACK_SIMILARITY)
        for titles in rejected:
            with self.subTest(titles=titles):
                self.assertLess(mod._track_similarity(*titles),
                                mod.LASTFM_MIN_TRACK_SIMILARITY)

    def test_track_similarity_handles_morse_and_balanced_quotes_only(self):
        self.assertEqual(mod._track_similarity("･･－－－", "･･－－－"), 1.0)
        self.assertEqual(mod._track_similarity("･･－－－", "･・－－－"), 0.0)
        self.assertEqual(mod._track_similarity(
            "How It’s Done", '"How It\'s Done" (Huntr/x: EJAE, Audrey Nuna, REI AMI)'), 1.0)
        for longer in ('"How It\'s Done (Huntr/x: EJAE)',
                       '"How It\'s Done" continuing',
                       '"Part II" (version)'):
            with self.subTest(longer=longer):
                self.assertLess(mod._track_similarity("How It’s Done" if "Part" not in longer else "Part I",
                                                      longer), .90)

    def test_kpop_quoted_annotation_fixture_clears_only_release_gate(self):
        bases = ["How It's Done", "Golden", "Soda Pop", "Your Idol", "Free", "What It Sounds Like",
                 "Strategy", "Takedown", "Score Suite", "Love, Maybe", "Path", "Finale"]
        annotated = [f'"{title}" (Huntr/x: performer)' for title in bases[:9]] + [
            "Maybe Love", "길 Path", "Finale Korean"]
        self.assertGreaterEqual(mod._track_overlap(bases, annotated), .60)
        self.assertEqual(mod._track_match_count(bases, annotated), 9)

    def test_transliteration_alignment_requires_every_boundary(self):
        album = {"name": "WINK", "artists": [{"name": "Miki Matsubara"}]}
        candidate = {"name": "WINK", "artist": "Miki Matsubara"}
        latin = ["Anchor One", "Anchor Two", "Anchor Three", "Anchor Four",
                 "Blue!", "Dréam (2024)", "Wind - 2", "Night…", "Moon?", "Sky #1"]
        native = latin[:4] + ["青！", "夢 (2024)", "風 - 2", "夜…", "月？", "空 #1"]
        tracks = [{"name": title} for title in latin]
        info = {**candidate, "tracks": {"track": [{"name": title} for title in native]}}
        result = mod.validate_lastfm_info(album, tracks, candidate, info)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "lastfm_transliteration_alignment")
        self.assertEqual(result["anchors"], 4)
        negatives = [
            (latin, native, {**candidate, "name": "WINK II"}),
            (latin, latin[:2] + native[4:8], candidate),
            (latin, latin[:3] + ["ordinary words"] + native[4:], candidate),
            (latin, [latin[1], latin[0]] + native[2:], candidate),
            (latin[:4] + ["LatinЖ"] + latin[5:], native, candidate),
            (latin[:4] + ["LatinΔ"] + latin[5:], native, candidate),
        ]
        for left, right, row in negatives:
            with self.subTest(right=right, row=row):
                detail = {**row, "tracks": {"track": [{"name": title} for title in right]}}
                self.assertFalse(mod.validate_lastfm_info(
                    album, [{"name": title} for title in left], candidate, detail)["accepted"])

    def test_known_track_contradictions_remain_rejected(self):
        album = {"name": "Toxicity", "artists": [{"name": "System of a Down"}]}
        candidate = {"name": "Toxicity", "artist": "System of a Down"}
        spotify = [{"name": name} for name in ["Toxicity"] + [f"Album {i}" for i in range(14)]]
        info = {**candidate, "tracks": {"track": [{"name": name} for name in
                                                    ["Toxicity", "Marmalade", "Metro"]]}}
        result = mod.validate_lastfm_info(album, spotify, candidate, info)
        self.assertFalse(result["accepted"])
        self.assertEqual((result["matched_tracks"], result["denominator"]), (1, 3))
        music = {"name": "MUSIC", "artists": [{"name": "Playboi Carti"}]}
        music_candidate = {"name": "MUSIC", "artist": "Playboi Carti"}
        stale = {**music_candidate, "tracks": {"track": [{"name": f"Stale {i}"}
                                                            for i in range(21)]}}
        self.assertEqual(mod.validate_lastfm_info(
            music, [{"name": f"Released {i}"} for i in range(30)], music_candidate,
            stale)["matched_tracks"], 0)

    def test_track_overlap_is_fuzzy_bounded_and_one_to_one(self):
        self.assertEqual(mod._track_overlap(["One"], ["Someone"]), 0.0)
        self.assertEqual(mod._track_overlap(["Blue Sky"], ["Red Moon"]), 0.0)
        self.assertEqual(mod._track_overlap(["Song", "Song"], ["Song", "Other"]), 0.5)
        for score, expected in ((mod.LASTFM_MIN_TRACK_SIMILARITY, 1.0),
                                (mod.LASTFM_MIN_TRACK_SIMILARITY - .001, 0.0)):
            with self.subTest(score=score), patch.object(
                    mod, "_track_similarity", return_value=score):
                self.assertEqual(mod._track_overlap(["a"], ["b"]), expected)
        edges = {("a", "x"), ("a", "y"), ("b", "x")}
        with patch.object(mod, "_track_similarity",
                          side_effect=lambda a, b: float((a, b) in edges)):
            self.assertEqual(mod._track_overlap(["a", "b"], ["x", "y"]), 1.0)

    def test_track_provider_labels_and_placeholders_are_non_contradictory(self):
        variants = (
            ("The Great Curve - 2005 Remaster",
             "The Great Curve - 2005 Remastered Version"),
            ("Track A- Solo Dancer",
             "Solo Dancer (Stop! And Listen, Sinner Jim Whitney!)"),
            ("Medley: Mode D-Trio and Group Dancers",
             "Trio and Group Dancers (Stop! Look! And Sing Songs Of Revolutions!)"),
        )
        for left, right in variants:
            with self.subTest(left=left):
                self.assertGreaterEqual(mod._track_similarity(left, right),
                                        mod.LASTFM_MIN_TRACK_SIMILARITY)

        spotify = {"name": "Album", "artists": [{"name": "Artist"}]}
        tracks = [{"name": "Known"}, {"name": "Second"}, {"name": "Third"}]
        info = {"name": "Album", "artist": "Artist", "tracks": {"track": [
            {"name": "Known"}, {"name": "Track 02"}, {"name": "Track 03"}]}}
        validation = mod.validate_lastfm_info(spotify, tracks,
                                              {"name": "Album", "artist": "Artist"}, info)
        self.assertTrue(validation["accepted"])
        self.assertEqual((validation["matched_tracks"], validation["denominator"]), (1, 1))

        restricted = {"id": "track", "name": "", "duration_ms": 0,
                      "explicit": False, "disc_number": 1, "track_number": 1,
                      "restrictions": {"reason": "market"}}
        self.assertTrue(mod._spotify_tracks_market_restricted([restricted]))
        self.assertFalse(mod._spotify_tracks_market_restricted([
            {**restricted, "explicit": "false"}]))

    def test_combined_artist_lookup_and_primary_recovery(self):
        spotify = {"name": "Album", "artists": [{"name": "One"}, {"name": "Two"}]}
        tracks = [{"name": "Song"}]

        class Fake:
            def album_getinfo(self, **kwargs):
                artist = kwargs["artist"]
                return {"name": "Album", "artist": artist,
                        "tracks": {"track": [{"name": "Song"}]}}

        combined = mod.lookup_combined_lastfm(Fake(), spotify, tracks)
        if combined is None:
            self.fail("combined artist lookup should validate")
        self.assertEqual(combined["candidate"]["artist"], "One & Two")
        self.assertEqual(combined["reason"], "lastfm_collaboration_lookup")

        candidates = [{"name": "Album", "artist": "Two"},
                      {"name": "Album", "artist": "One"}]
        recovered = mod.recover_lastfm_candidate(Fake(), spotify, tracks, candidates)
        self.assertEqual(recovered["candidate"]["artist"], "One")

    def test_stale_track_acceptance_requires_exact_non_eponymous_identity(self):
        album = {"name": "Toxicity", "artists": [{"name": "System Of A Down"}]}
        candidate = {"name": "Toxicity", "artist": "System Of A Down"}
        info = dict(candidate)
        contradiction = {"reason": "lastfm_track_contradiction"}
        self.assertTrue(mod._accept_stale_lastfm_tracks(
            album, candidate, info, contradiction))
        self.assertFalse(mod._accept_stale_lastfm_tracks(
            {"name": "Rita Lee", "artists": [{"name": "Rita Lee"}]},
            {"name": "Rita Lee", "artist": "Rita Lee"},
            {"name": "Rita Lee", "artist": "Rita Lee"}, contradiction))
        self.assertFalse(mod._accept_stale_lastfm_tracks(
            album, candidate, {**info, "artist": "Other"}, contradiction))

    def test_track_match_count_is_duplicate_aware_at_boundary(self):
        self.assertEqual(mod._track_match_count(["Song", "Song"], ["Song", "Other"]), 1)
        edges = {("a", "x"), ("a", "y"), ("b", "x")}
        with patch.object(mod, "_track_similarity",
                          side_effect=lambda a, b: float((a, b) in edges)):
            self.assertEqual(mod._track_match_count(["a", "b"], ["x", "y"]), 2)
        for score, expected in ((mod.LASTFM_MIN_TRACK_SIMILARITY, 1),
                                (mod.LASTFM_MIN_TRACK_SIMILARITY - .001, 0)):
            with patch.object(mod, "_track_similarity", return_value=score):
                self.assertEqual(mod._track_match_count(["a"], ["b"]), expected)

    def test_lastfm_recovery_uses_full_spotify_coverage_and_exact_title(self):
        spotify = {"name": "White Pony (20th Anniversary Deluxe Edition)",
                   "artists": [{"name": "Deftones"}]}
        tracks = [{"name": f"Track {i}"} for i in range(22)]
        exact = {"name": spotify["name"], "artist": "Deftones"}
        explicit = {"name": spotify["name"] + " [Explicit]", "artist": "Deftones"}

        class Fake:
            def album_getinfo(self, **kwargs):
                candidate = exact if kwargs["album"] == exact["name"] else explicit
                names = range(22) if candidate is exact else range(2)
                return {**candidate, "tracks": {"track": [
                    {"name": f"Track {i}"} for i in names]}}

        recovered = mod.recover_lastfm_candidate(Fake(), spotify, tracks, [explicit, exact])
        self.assertIs(recovered["candidate"], exact)
        self.assertEqual(recovered["validation"]["matched_tracks"], 22)
        subset = Fake().album_getinfo(artist="Deftones", album=explicit["name"], autocorrect=0)
        self.assertEqual(mod._track_overlap(
            [t["name"] for t in tracks], [t["name"] for t in subset["tracks"]["track"]]), 1.0)
        self.assertAlmostEqual(mod.lastfm_recovery_validation(
            spotify, tracks, explicit, subset)["spotify_track_coverage"], 2 / 22)

    def test_lastfm_recovery_rejects_partial_discs_and_identity_drift(self):
        spotify = {"name": "Collector Edition", "artists": [{"name": "Artist"}]}
        tracks = [{"name": f"Track {i}"} for i in range(22)]
        candidates = [{"name": "Collector Edition (Disc 1)", "artist": "Artist"},
                      {"name": "Collector Edition [Disc 2]", "artist": "Artist"}]

        class Fake:
            def album_getinfo(self, **kwargs):
                start = 0 if "Disc 1" in kwargs["album"] else 10
                return {"name": kwargs["album"], "artist": "Artist",
                        "tracks": {"track": [{"name": f"Track {i}"}
                                               for i in range(start, start + 10)]}}

        self.assertIsNone(mod.recover_lastfm_candidate(
            Fake(), spotify, tracks, candidates)["candidate"])
        drift = {"name": "Collector Edition", "artist": "Someone Else",
                 "tracks": {"track": [{"name": f"Track {i}"} for i in range(22)]}}
        self.assertEqual(mod.lastfm_recovery_validation(
            spotify, tracks, candidates[0], drift)["reason"], "lastfm_identity_changed")

    def test_lastfm_recovery_prefers_unique_exact_not_coverage_or_order(self):
        spotify = {"name": "Dream (Deluxe Edition)", "artists": [{"name": "Artist"}]}
        tracks = [{"name": f"Track {i}"} for i in range(10)]
        exact = {"name": spotify["name"], "artist": "Artist", "listeners": "1"}
        alias = {"name": "Dream (Deluxe)", "artist": "Artist", "listeners": "999999"}
        infos = {
            exact["name"]: {**exact, "tracks": {"track": [{"name": f"Track {i}"}
                                                              for i in range(6)]}},
            alias["name"]: {**alias, "tracks": {"track": [{"name": f"Track {i}"}
                                                              for i in range(10)]}},
        }

        class Fake:
            def __init__(self): self.calls = []
            def album_getinfo(self, **kwargs):
                self.calls.append(kwargs["album"])
                return infos[kwargs["album"]]

        outcomes = []
        for candidates in ([exact, alias], [alias, exact]):
            fake = Fake()
            outcomes.append(mod.recover_lastfm_candidate(
                fake, spotify, tracks, list(candidates))["candidate"]["name"])
            self.assertEqual(fake.calls, sorted(fake.calls, key=mod.match_key))
        self.assertEqual(outcomes, [exact["name"], exact["name"]])

    def test_lastfm_recovery_exact_preference_uses_validated_detail_title(self):
        spotify = {"name": "Dream (Deluxe Edition)", "artists": [{"name": "Artist"}]}
        tracks = [{"name": f"Track {i}"} for i in range(5)]
        exact_search = {"name": spotify["name"], "artist": "Artist"}
        fuzzy_search = {"name": "Dream (Deluxe Ed.)", "artist": "Artist"}
        infos = {
            exact_search["name"]: {"name": fuzzy_search["name"], "artist": "Artist"},
            fuzzy_search["name"]: {"name": spotify["name"], "artist": "Artist"},
        }
        for info in infos.values():
            info["tracks"] = {"track": list(tracks)}

        class Fake:
            def album_getinfo(self, **kwargs): return infos[kwargs["album"]]

        recovered = mod.recover_lastfm_candidate(
            Fake(), spotify, tracks, [exact_search, fuzzy_search])
        self.assertIs(recovered["candidate"], fuzzy_search)
        self.assertEqual(recovered["info"]["name"], spotify["name"])

    def test_lastfm_research_fixture_predictions(self):
        # Minimal summaries of the observed snapshot: post, title, matched tracks,
        # Spotify tracks, and whether the unique exact detail should recover.
        fixtures = (
            (2874, "In A Perfect World (Expanded Edition)", 15, 15, True),
            (2811, "It’s About Time", 9, 10, True),
            (2504, "Led Zeppelin IV (Deluxe Edition)", 10, 10, True),
            (2492, "Californication (Deluxe Edition)", 6, 6, True),
            (2398, "All Things Must Pass (2014 Remaster)", 10, 10, True),
            (2257, "DONDA 2", 6, 7, True),
            (2177, "Paramore (Deluxe Edition)", 8, 9, True),
            (2068, "Humanz (Deluxe)", 10, 10, True),
            (2017, "Escape (2022 Remaster)", 10, 10, True),
            (1560, "White Pony (20th Anniversary Deluxe Edition)", 22, 22, True),
            (1485, "PACIFIC", 8, 8, True),
            (1443, "Nek (spanish version)", 10, 11, True),
            (1277, "More Than Just a Dream (Deluxe Edition)", 13, 14, True),
            (2786, "Remain in Light (Deluxe Version)", 5, 10, False),
            (2769, "Super Tecmo Bo", 0, 10, False),
            (2752, "Life Is Beautiful (Deluxe)", 0, 10, False),
            (2365, "Unknown Pleasures (Collector’s Edition)", 6, 11, False),
            (1801, "DUMB", 0, 10, False),
            (1539, "samurai champloo music record impression", 0, 10, False),
            (1497, "samurai champloo music record departure", 0, 10, False),
        )
        recovered_ids, residual_ids = set(), set()
        for post_id, title, matched, total, should_recover in fixtures:
            with self.subTest(post_id=post_id):
                spotify = {"name": title, "artists": [{"name": "Artist"}]}
                tracks = [{"name": f"Track {i}"} for i in range(total)]
                candidate = {"name": title, "artist": "Artist"}
                info = {**candidate, "tracks": {"track": tracks[:matched]}}

                class Fake:
                    def album_getinfo(self, **kwargs): return info

                result = mod.recover_lastfm_candidate(Fake(), spotify, tracks, [candidate])
                self.assertEqual(result["candidate"] is not None, should_recover)
                (recovered_ids if result["candidate"] else residual_ids).add(post_id)
        self.assertEqual(recovered_ids, {2874, 2811, 2504, 2492, 2398, 2257, 2177,
                                         2068, 2017, 1560, 1485, 1443, 1277})
        self.assertEqual(residual_ids, {2786, 2769, 2752, 2365, 1801, 1539, 1497})

        low = mod.choose_lastfm_candidate(
            {"name": "Purple Friday", "artists": [{"name": "Artist"}]},
            [{"name": "Unrelated", "artist": "Someone Else"}])
        self.assertEqual(low["reason"], "lastfm_low_confidence")  # Post 1226 stays out.
        self.assertNotIn("contenders", low)

    def test_lastfm_recovery_multiple_exact_and_provider_failure_are_not_unique(self):
        spotify = {"name": "Album", "artists": [{"name": "Artist"}]}
        tracks = [{"name": "Song"}]
        one = {"name": "Album", "artist": "Artist", "listeners": "1"}
        two = {"name": "Album", "artist": "Artist", "listeners": "999"}

        class Fake:
            def __init__(self, fail=False): self.calls = 0; self.fail = fail
            def album_getinfo(self, **kwargs):
                self.calls += 1
                if self.fail and self.calls == 2:
                    raise mod.LastFMProviderError("down", operation="album.getinfo")
                return {"name": "Album", "artist": "Artist",
                        "tracks": {"track": [{"name": "Song"}]}}

        self.assertIsNone(mod.recover_lastfm_candidate(
            Fake(), spotify, tracks, [one, two])["candidate"])
        with self.assertRaises(mod.LastFMProviderError):
            mod.recover_lastfm_candidate(Fake(True), spotify, tracks, [one, two])

        malformed = {**one, "name": "Album Alias", "mbid": "not-a-uuid"}
        calls = []
        class LocatorFake:
            def album_getinfo(self, **kwargs):
                calls.append(kwargs)
                return {**malformed, "tracks": {"track": [{"name": "Song"}]}}
        mod.recover_lastfm_candidate(LocatorFake(), spotify, tracks, [malformed])
        self.assertEqual(calls, [{"artist": "Artist", "album": "Album Alias",
                                  "autocorrect": 0}])

    def test_malformed_nonempty_lastfm_tracks_are_provider_errors(self):
        for tracks in ("bad", {"track": "bad"}, {"track": [{"name": "One"}, "bad"]}):
            with self.subTest(tracks=tracks), self.assertRaisesRegex(RuntimeError, "malformed"):
                mod.validate_lastfm_info(SPOTIFY, [], CANDIDATE,
                                         {**CANDIDATE, "tracks": tracks})
        for tracks in (None, {}, {"track": []}):
            with self.subTest(tracks=tracks):
                self.assertTrue(mod.validate_lastfm_info(
                    SPOTIFY, [], CANDIDATE, {**CANDIDATE, "tracks": tracks})["accepted"])

    def test_tags_reads_toptags_and_tags(self):
        self.assertEqual(mod.pick_top_tags({"toptags": {"tag": {"name": "rock"}}}, 3, []), ["rock"])
        self.assertEqual(mod.pick_top_tags({"tags": {"tag": "pop"}}, 3, []), ["pop"])

    def test_enrich_distinguishes_no_artist_provider_error_and_no_results(self):
        post = {"id": 1, "title": {"rendered": "Album"}, "date": "2020-01-01",
                "tags": [], "acf": {}}
        result = cast(dict, mod.enrich(post, object(), object(), {}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_missing_artist")

        class Search:
            def __init__(self, error=False): self.error = error
            def search_albums(self, *args, **kwargs):
                if self.error: raise urllib.error.URLError("down")
                return []
        post["tags"] = [7]
        result = cast(dict, mod.enrich(post, Search(error=True), object(), {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_provider_error")
        result = cast(dict, mod.enrich(post, Search(), object(), {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_catalog_unavailable")
        self.assertTrue(result["ignored"])

    def test_enrich_explains_failed_release_type_tiebreaker(self):
        def candidate(candidate_id, album_type):
            return {"id": candidate_id, "name": "Album", "artists": [{"name": "Artist"}],
                    "album_type": album_type, "total_tracks": 10}

        post = {"id": 1, "title": {"rendered": "Album"}, "date": "2020-01-01",
                "tags": [7], "categories": [6], "acf": {}}
        multiple = [candidate("one", "album"), candidate("two", "album")]
        zero = [candidate("one", "single"), candidate("two", "single")]
        unresolved = {"candidate": None, "reason": "spotify_ambiguous"}
        for rows in (multiple, zero):
            with self.subTest(rows=rows), patch.object(mod, "search_ladder", return_value=rows), \
                    patch.object(mod, "recover_spotify_ambiguity", return_value=unresolved):
                result = cast(dict, mod.enrich(post, object(), object(), {7: "Artist"},
                                               release_type_terms={}))
                self.assertEqual(result["diagnostics"][0]["code"], "spotify_ambiguous")
                self.assertIn("full-release evidence", result["diagnostics"][0]["message"])

        with patch.object(mod, "search_ladder", return_value=multiple), \
                patch.object(mod, "recover_spotify_ambiguity", return_value=unresolved):
            result = cast(dict, mod.enrich(
                {**post, "categories": []}, object(), object(), {7: "Artist"},
                release_type_terms={}))
        self.assertEqual(result["diagnostics"][0]["code"], "spotify_ambiguous")

    def test_enrich_finds_lastfm_release_with_artist_aware_search(self):
        album = {"id": "sid", "name": "Palette", "artists": [{"name": "Didier Armeni"}],
                 "total_tracks": 1, "album_type": "album", "release_date": "2020-01-01"}
        tracks = [{"id": "tid", "name": "Song", "duration_ms": 1000,
                   "disc_number": 1, "track_number": 1, "explicit": False}]
        post = {"id": 1, "title": {"rendered": "Palette"}, "date": "2020-01-02",
                "tags": [7], "acf": {}}

        class SpotifyFake:
            def search_albums(self, *args, **kwargs):
                return [{"id": "sid", "name": "Palette",
                         "artists": [{"name": "Didier Armeni"}]}]
            def album(self, album_id): return album
            def all_tracks(self, album_id): return tracks

        class LastFmFake:
            def __init__(self): self.queries = []
            def album_search(self, album_name, limit=10):
                self.queries.append(album_name)
                if album_name == "Palette Didier Armeni":
                    return [{"name": "Palette", "artist": "Didier Armeni"}]
                return [{"name": "Palette", "artist": "Someone Else"}]
            def album_getinfo(self, **kwargs):
                return {"name": "Palette", "artist": "Didier Armeni", "tracks": {}}
            def album_gettoptags(self, **kwargs):
                return {"toptags": {"tag": []}}
            def artist_gettoptags(self, *args, **kwargs):
                return {"toptags": {"tag": []}}

        lastfm = LastFmFake()
        result = cast(dict, mod.enrich(post, SpotifyFake(), lastfm, {7: "Didier Armeni"}))

        self.assertIn("write", result)
        self.assertEqual(result["matches"]["lastfm"]["artist"], "Didier Armeni")
        self.assertEqual(lastfm.queries, ["Palette Didier Armeni", "Palette"])

    def test_enrich_reuses_recovered_lastfm_detail_and_skips_low_confidence(self):
        album = {"id": "sid", "name": "Album", "artists": [{"name": "Artist"}],
                 "total_tracks": 1, "album_type": "album", "release_date": "2020-01-01"}
        tracks = [{"id": "tid", "name": "Song", "duration_ms": 1000,
                   "disc_number": 1, "track_number": 1, "explicit": False}]
        post = {"id": 1, "title": {"rendered": "Album"}, "date": "2020-01-02",
                "tags": [7], "acf": {}}
        candidates = [{"name": "Album", "artist": "Artist"},
                      {"name": "Album  ", "artist": "Artist"}]

        class SpotifyFake:
            def search_albums(self, *args, **kwargs):
                return [{"id": "sid", "name": "Album", "artists": [{"name": "Artist"}]}]
            def album(self, album_id): return album
            def all_tracks(self, album_id): return tracks

        class LastFmFake:
            def __init__(self): self.calls = []
            def album_search(self, *args, **kwargs): return candidates
            def album_getinfo(self, **kwargs):
                self.calls.append(kwargs)
                has_tracks = kwargs["album"] == "Album"
                return {"name": kwargs["album"], "artist": "Artist",
                        "tracks": {"track": [{"name": "Song"}] if has_tracks else []},
                        "toptags": {"tag": [{"name": "rock"}]}}
            def album_gettoptags(self, **kwargs): return {}
            def artist_gettoptags(self, *args, **kwargs): return {}

        fake = LastFmFake()
        result = cast(dict, mod.enrich(post, SpotifyFake(), fake, {7: "Artist"}))
        self.assertIn("write", result)
        self.assertEqual(len(fake.calls), 2)  # One GET per contender; no winner refetch.
        self.assertEqual(result["matches"]["lastfm"]["track_overlap"], 1.0)

        with patch.object(mod, "recover_lastfm_candidate") as recover:
            with patch.object(mod, "choose_lastfm_candidate", return_value={
                    "candidate": None, "reason": "lastfm_low_confidence"}):
                low = cast(dict, mod.enrich(post, SpotifyFake(), LastFmFake(), {7: "Artist"}))
            self.assertEqual(low["diagnostics"][0]["code"], "lastfm_catalog_unavailable")
            self.assertTrue(low["ignored"])
            recover.assert_not_called()

    def test_enrich_lastfm_accepted_path_and_failures(self):
        candidate = {"name": "Album", "artist": "Artist"}
        album = {"id": "sid", "name": "Album", "artists": [{"name": "Artist"}],
                 "total_tracks": 1, "album_type": "album", "release_date": "2020-01-01"}
        tracks = [{"id": "tid", "name": "Song", "duration_ms": 1000,
                   "disc_number": 1, "track_number": 1, "explicit": False}]
        post = {"id": 1, "title": {"rendered": "Album"}, "date": "2020-01-02",
                "tags": [7], "acf": {}}

        class SpotifyFake:
            def search_albums(self, *args, **kwargs):
                return [{"id": "sid", "name": "Album", "artists": [{"name": "Artist"}]}]
            def album(self, album_id): return album
            def all_tracks(self, album_id): return tracks

        class LastFmFake:
            def __init__(self, candidates=None, info=None, search_error=None):
                self.candidates = [candidate] if candidates is None else candidates
                self.info = info or {**candidate, "tracks": {},
                                     "toptags": {"tag": [{"name": "rock"}]}}
                self.search_error = search_error
                self.getinfo_calls = []
                self.toptags_calls = []
            def album_search(self, album_name, limit=10):
                if self.search_error: raise self.search_error
                return self.candidates
            def album_getinfo(self, **kwargs):
                self.getinfo_calls.append(kwargs)
                return self.info
            def album_gettoptags(self, **kwargs):
                self.toptags_calls.append(kwargs)
                return {"toptags": {"tag": [{"name": "rock"}]}}
            def artist_gettoptags(self, *args, **kwargs):
                return {"toptags": {"tag": []}}

        cache = {"artist": {"Artist": 10}, "genre": {"rock": 20},
                 "release_type": {"Album": 30, "Single": 31}}
        class WordPressFake:
            def list_tax_terms(self, tax): return cache[tax]
        wp = WordPressFake()
        for fake, reason in ((LastFmFake(search_error=RuntimeError("down")),
                              "lastfm_provider_error"),
                             (LastFmFake(candidates=[]), "lastfm_catalog_unavailable")):
            with self.subTest(reason=reason):
                result = cast(dict, mod.enrich(post, SpotifyFake(), fake, {7: "Artist"}))
                self.assertEqual(result["diagnostics"][0]["code"], reason)
                self.assertEqual(result.get("ignored", False),
                                 reason == "lastfm_catalog_unavailable")

        rejected = LastFmFake(info={**candidate, "artist": "Other", "tracks": {}})
        result = cast(dict, mod.enrich(post, SpotifyFake(), rejected, {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "lastfm_identity_mismatch")

        fallback = LastFmFake()
        body = cast(dict, mod.enrich(post, SpotifyFake(), fallback, {7: "Artist"}))
        self.assertEqual(fallback.getinfo_calls, [{"artist": "Artist", "album": "Album", "autocorrect": 0}])
        self.assertNotIn("music_mood_tags", body["write"]["acf"])
        self.assertEqual(body["write"]["taxonomies"]["genre"], ["rock"])

        mbid_candidate = {**candidate,
                          "mbid": "123e4567-e89b-12d3-a456-426614174000",
                          "url": "https://www.last.fm/music/Artist/Album"}
        info_url = "https://www.last.fm/music/Artist/Album+Details"
        pinned = LastFmFake(
            candidates=[mbid_candidate],
            info={**candidate, "mbid": "", "url": info_url, "tracks": {},
                  "toptags": {"tag": [{"name": "rock"}]}},
        )
        caravelle = cast(dict, mod.enrich(post, SpotifyFake(), pinned, {7: "Artist"}))
        self.assertEqual(pinned.getinfo_calls, [{"mbid": mbid_candidate["mbid"]}])
        self.assertEqual(caravelle["write"]["acf"]["mbid"], mbid_candidate["mbid"])
        self.assertEqual(caravelle["write"]["acf"]["lastfm_url"], info_url)
        self.assertEqual(caravelle["matches"]["lastfm"]["mbid"], mbid_candidate["mbid"])
        self.assertEqual(caravelle["matches"]["lastfm"]["url"], info_url)
        self.assertNotIn("lastfm_no_mbid", [row["code"] for row in caravelle["diagnostics"]])

        class FallbackFake(LastFmFake):
            def __init__(self, alternate_error=False, alternate_valid=True):
                super().__init__(candidates=[mbid_candidate])
                self.alternate_error = alternate_error
                self.alternate_valid = alternate_valid
            def album_getinfo(self, **kwargs):
                self.getinfo_calls.append(kwargs)
                if "mbid" in kwargs:
                    return {**candidate, "name": "Different Album", "tracks": {}}
                if self.alternate_error:
                    raise RuntimeError("fallback down")
                return {**candidate, "artist": "Artist" if self.alternate_valid else "Other",
                        "tracks": {"track": [{"name": "Song"}]},
                        "toptags": {"tag": []}}

        recovered = FallbackFake()
        recovered_body = cast(dict, mod.enrich(post, SpotifyFake(), recovered, {7: "Artist"}))
        self.assertEqual(recovered.getinfo_calls, [
            {"mbid": mbid_candidate["mbid"]},
            {"artist": "Artist", "album": "Album", "autocorrect": 0}])
        self.assertIn("lastfm_lookup_fallback",
                      [row["code"] for row in recovered_body["diagnostics"]])
        self.assertEqual(recovered.toptags_calls, [
            {"artist": "Artist", "album": "Album", "autocorrect": 0}])
        for failed in (FallbackFake(alternate_valid=False), FallbackFake(alternate_error=True)):
            unresolved = cast(dict, mod.enrich(post, SpotifyFake(), failed, {7: "Artist"}))
            self.assertEqual(unresolved["diagnostics"][0]["code"], "lastfm_identity_mismatch")
            self.assertIn("selected='Album'", unresolved["diagnostics"][0]["message"])
            self.assertNotEqual(unresolved["diagnostics"][0]["code"], "lastfm_provider_error")

        successful = LastFmFake(candidates=[mbid_candidate], info={
            **candidate, "tracks": {"track": [{"name": "Song"}]},
            "toptags": {"tag": []}})
        mod.enrich(post, SpotifyFake(), successful, {7: "Artist"})
        self.assertEqual(successful.getinfo_calls, [{"mbid": mbid_candidate["mbid"]}])
        self.assertEqual(successful.toptags_calls, [{"mbid": mbid_candidate["mbid"]}])

        malformed = LastFmFake(info={**candidate, "tracks": {"track": ["bad"]}})
        result = cast(dict, mod.enrich(post, SpotifyFake(), malformed, {7: "Artist"}))
        self.assertEqual(result["diagnostics"][0]["code"], "lastfm_provider_error")

        for track_name in (1, ["Song"], {"value": "Song"}):
            with self.subTest(track_name=track_name):
                malformed = LastFmFake(
                    info={**candidate, "tracks": {"track": [{"name": track_name}]}}
                )
                result = cast(
                    dict,
                    mod.enrich(post, SpotifyFake(), malformed, {7: "Artist"}),
                )
                diagnostic = result["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "lastfm_provider_error")
                self.assertEqual(diagnostic["message"],
                                 "Last.fm album.getinfo failed unexpectedly.")
                self.assertEqual(diagnostic["details"], {
                    "provider": "lastfm", "operation": "album.getinfo",
                    "failure_kind": "unexpected", "retryable": False,
                    "attempts": 1, "circuit_state": "closed",
                })

    def test_cli_parser_and_fuzzy_missing_artist(self):
        args = mod.build_parser().parse_args(["fuzzy", "Album"])
        self.assertEqual(args.artists, [])
        class FakeSpotify:
            def __init__(self, *a): pass
            def search_albums(self, *a, **k): return []
        with patch.object(mod, "Spotify", FakeSpotify), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mod.cmd_fuzzy(args, {"SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": ""}), 0)
        self.assertIn("spotify_missing_artist", output.getvalue())


if __name__ == "__main__":
    unittest.main()
