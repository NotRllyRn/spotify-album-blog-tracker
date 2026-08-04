import contextlib
import importlib
import io
import unittest
from typing import cast
from unittest.mock import patch

import post_to_album as mod

enrichment_mod = importlib.import_module("album_metadata.enrichment")


class SpotifyFake:
    album_data = {
        "id": "album-id", "name": "Élan (Deluxe Edition) - Single",
        "artists": [{"name": "The Artist"}], "total_tracks": 2,
        "album_type": "single", "release_date": "2024-02-03",
    }
    tracks = [
        {"id": "one", "name": "First – Edit", "duration_ms": 1000,
         "disc_number": 1, "track_number": 1, "explicit": False},
        {"id": "two", "name": "Second", "duration_ms": 2000,
         "disc_number": 1, "track_number": 2, "explicit": False},
    ]

    def search_albums(self, *args, **kwargs):
        return [{"id": "album-id", "name": "Post Title",
                 "artists": [{"name": "The Artist"}]}]

    def album(self, album_id):
        return dict(self.album_data)

    def all_tracks(self, album_id):
        return list(self.tracks)


class LastFmFake:
    def album_search(self, *args, **kwargs):
        return [{"name": SpotifyFake.album_data["name"], "artist": "The Artist"}]

    def album_getinfo(self, **kwargs):
        return {"name": SpotifyFake.album_data["name"], "artist": "The Artist",
                "tracks": {}, "toptags": {"tag": [
                    {"name": "THE ARTIST"}, {"name": "Rock"},
                    {"name": "rock"}, {"name": "Pop"}, {"name": "Ambient"},
                    {"name": "Jazz"}]}}

    def album_gettoptags(self, **kwargs):
        return {"toptags": {"tag": []}}

    def artist_gettoptags(self, *args, **kwargs):
        return {"toptags": {"tag": []}}


class WordPressFake:
    def __init__(self):
        self.resolved = []
        self.ids = {"The Artist": 10, "Rock": 20, "Pop": 21,
                    "Ambient": 22, "Single": 30}

    def list_tax_terms(self, tax):
        self.resolved.append(("list", tax))
        return {}

    def create_term(self, tax, name) -> int | None:
        self.resolved.append((tax, name))
        return self.ids[name]


def make_post(**changes):
    post = {"id": 1, "title": {"rendered": "Post Title"},
            "date": "2024-03-04", "tags": [7], "acf": {},
            "artist": [], "genre": [], "release_type": [],
            "categories": [93, 6, 200, 42, 93]}
    post.update(changes)
    return post


def enrich(post, wp=None, write_policy=mod.WRITE_FILL_ONLY):
    return cast(dict, mod.enrich(
        post, SpotifyFake(), LastFmFake(), {7: "The Artist"}, write_policy))["write"]


class PayloadTests(unittest.TestCase):
    def test_cli_overwrite_option_scope_and_help(self):
        parser = mod.build_parser()
        self.assertFalse(parser.parse_args(["run"]).overwrite_managed)
        self.assertTrue(parser.parse_args(["run", "--overwrite-managed"]).overwrite_managed)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["apply-plan", "plan.json", "--overwrite-managed"])
        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out), self.assertRaises(SystemExit):
            parser.parse_args(["run", "--help"])
        text = help_out.getvalue()
        for phrase in ("rating, favorite, notes", "track highlights", "never clear"):
            self.assertIn(phrase, text)

    def test_approved_auto_fields_exactly(self):
        self.assertNotIn("spotify_ambiguity_recovered", mod.DIAGNOSTIC_CODES)
        self.assertEqual(mod.AUTO_FILLABLE_FIELDS, (
            "spotify_title", "music_tracks", "music_length_ms",
            "spotify_album_id", "spotify_album_url", "music_release_date",
            "music_listened_at", "lastfm_url", "mbid", "music_total_tracks",
            "music_avg_track_ms", "music_explicit", "listen_count"))

    def test_payload_raw_title_keys_false_highlights_categories_and_taxonomies(self):
        post = make_post(acf={"music_tracks": [
            {"spotify_id": "one", "highlight": True},
            {"spotify_id": "old", "highlight": True}]})
        body = enrich(post)
        acf = body["acf"]
        self.assertEqual(acf["spotify_title"], "Élan (Deluxe Edition) - Single")
        self.assertEqual(acf["music_explicit"], False)
        self.assertEqual(acf["listen_count"], 1)
        self.assertNotIn("listen-count", acf)
        self.assertNotIn("music_mood_tags", acf)
        self.assertNotIn("unreleased", acf)
        self.assertNotIn("music_tracks", acf)  # existing provider rows are fill-only
        # Category order is preserved, legacy release IDs replaced, and duplicates removed.
        self.assertEqual(body["categories"], [93, 200, 42, 5])
        self.assertEqual(body["taxonomies"]["artist"], ["The Artist"])
        self.assertEqual(body["taxonomies"]["genre"], ["Rock", "Pop", "Ambient"])
        self.assertEqual(body["taxonomies"]["release_type"], ["Single"])

    def test_null_destination_tracks_are_rebuilt_without_highlights(self):
        rows = enrich(make_post(acf={"music_tracks": None}))["acf"]["music_tracks"]
        self.assertEqual([row["spotify_id"] for row in rows], ["one", "two"])
        self.assertEqual([row["highlight"] for row in rows], [False, False])

    def test_rebuilt_tracks_preserve_highlight_by_spotify_id(self):
        post = make_post(acf={"music_tracks": [
            {"spotify_id": "one", "highlight": True}]})
        original = mod.is_field_present

        def destination_presence(field, value):
            # Simulate a schema adapter reporting this repeater as replaceable.
            return False if field == "music_tracks" else original(field, value)

        with patch.object(enrichment_mod, "is_field_present", side_effect=destination_presence):
            rows = enrich(post)["acf"]["music_tracks"]
        self.assertTrue(rows[0]["highlight"])
        self.assertFalse(rows[1]["highlight"])
        self.assertEqual([row["spotify_id"] for row in rows], ["one", "two"])

    def test_fill_only_acf_and_editorial_fields_untouched(self):
        acf = {"spotify_title": "Editor title", "music_rating": 5,
               "music_favorite": True, "music_notes": "keep"}
        body = enrich(make_post(acf=acf))
        self.assertNotIn("spotify_title", body["acf"])
        for key in ("music_rating", "music_favorite", "music_notes"):
            self.assertNotIn(key, body["acf"])

    def test_missing_optional_provider_values_are_omitted(self):
        old_date = SpotifyFake.album_data["release_date"]
        SpotifyFake.album_data["release_date"] = "bad"
        try:
            body = enrich(make_post())
        finally:
            SpotifyFake.album_data["release_date"] = old_date
        self.assertNotIn("music_release_date", body["acf"])
        self.assertNotIn("lastfm_url", body["acf"])
        self.assertNotIn("mbid", body["acf"])

    def test_existing_artist_and_genre_are_omitted_without_resolution(self):
        wp = WordPressFake()
        body = enrich(make_post(artist=[99], genre=[98]), wp)
        self.assertNotIn("artist", body["taxonomies"])
        self.assertNotIn("genre", body["taxonomies"])
        self.assertFalse(any(tax in ("artist", "genre") for tax, _ in wp.resolved))
        self.assertEqual(body["taxonomies"]["release_type"], ["Single"])

    def test_completion_requires_artist_and_release_type_not_genre(self):
        acf = {name: (False if name == "music_explicit" else 1)
               for name in mod.AUTO_FILLABLE_FIELDS}
        self.assertTrue(mod.post_is_complete(
            make_post(acf=acf, artist=[1], genre=[], release_type=[2])))
        self.assertFalse(mod.post_is_complete(make_post(acf=acf, release_type=[2])))
        self.assertFalse(mod.post_is_complete(make_post(acf=acf, artist=[1])))

    def test_genre_filter_is_case_insensitive_deduped_ordered_and_capped(self):
        info = {"toptags": {"tag": ["Seen Live", "Artist", "Rock", "rock",
                                     "Pop", "Ambient", "Jazz"]}}
        self.assertEqual(mod.pick_top_tags(info, 3, mod.LFM_BLOCKLIST, ["artist"]),
                         ["Rock", "Pop", "Ambient"])
        self.assertEqual(mod.pick_top_tags({"toptags": {"tag": ["AOTY", "Artist"]}},
                                           3, mod.LFM_BLOCKLIST, ["artist"]), [])

    def test_embedded_album_tags_prevent_fallback_requests(self):
        class EmbeddedTags(LastFmFake):
            def album_gettoptags(self, **kwargs):
                raise AssertionError("album.getTopTags should not be called")

            def artist_gettoptags(self, *args, **kwargs):
                raise AssertionError("artist.getTopTags should not be called")

        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), EmbeddedTags(), {7: "The Artist"}))
        self.assertEqual(result["write"]["taxonomies"]["genre"],
                         ["Rock", "Pop", "Ambient"])

    def test_album_top_tags_are_used_when_embedded_tags_are_filtered_out(self):
        class AlbumFallback(LastFmFake):
            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info["toptags"] = {"tag": ["AOTY", "The Artist"]}
                return info

            def album_gettoptags(self, **kwargs):
                self.album_args = kwargs
                return {"toptags": {"tag": ["Seen Live", "Rock"]}}

            def artist_gettoptags(self, *args, **kwargs):
                raise AssertionError("artist.getTopTags should not be called")

        lfm = AlbumFallback()
        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), lfm, {7: "The Artist"}))
        self.assertEqual(lfm.album_args, {
            "artist": "The Artist", "album": SpotifyFake.album_data["name"],
            "autocorrect": 0})
        self.assertEqual(result["write"]["taxonomies"]["genre"], ["Rock"])

    def test_artist_top_tags_are_last_fallback_and_reuse_filtering(self):
        class ArtistFallback(LastFmFake):
            def __init__(self):
                self.fallback_calls = []

            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info["toptags"] = {"tag": []}
                return info

            def album_gettoptags(self, **kwargs):
                self.fallback_calls.append(("album", kwargs))
                return {"toptags": {"tag": ["The Artist", "AOTY"]}}

            def artist_gettoptags(self, *args, **kwargs):
                self.fallback_calls.append(("artist", args, kwargs))
                return {"toptags": {"tag": ["Seen Live", "Pop", "pop"]}}

        lfm = ArtistFallback()
        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), lfm, {7: "The Artist"}))
        self.assertEqual(lfm.fallback_calls, [
            ("album", {"artist": "The Artist", "album": SpotifyFake.album_data["name"],
                       "autocorrect": 0}),
            ("artist", ("The Artist",), {"autocorrect": 0}),
        ])
        self.assertEqual(result["write"]["taxonomies"]["genre"], ["Pop"])

    def test_artist_tags_retry_with_canonical_alias(self):
        class CanonicalArtistFallback(LastFmFake):
            def __init__(self):
                self.autocorrect = []

            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info["toptags"] = {"tag": []}
                return info

            def artist_gettoptags(self, *args, **kwargs):
                self.autocorrect.append(kwargs["autocorrect"])
                tags = [] if kwargs["autocorrect"] == 0 else ["Hip-Hop", "rap"]
                return {"toptags": {"tag": tags}}

        lfm = CanonicalArtistFallback()
        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), lfm, {7: "The Artist"}))
        self.assertEqual(lfm.autocorrect, [0, 1])
        self.assertEqual(result["write"]["taxonomies"]["genre"], ["Hip-Hop", "rap"])
        codes = [row["code"] for row in result["diagnostics"]]
        self.assertNotIn("lastfm_no_tags", codes)
        self.assertNotIn("lastfm_no_tracks", codes)

    def test_artist_tags_fall_back_from_mbid_disambiguator_to_selected_artist(self):
        class DisambiguatedArtist(LastFmFake):
            def __init__(self):
                self.calls = []

            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info.update(artist="The Artist (2)", toptags={"tag": []})
                return info

            def artist_gettoptags(self, artist, **kwargs):
                self.calls.append((artist, kwargs["autocorrect"]))
                tags = ["Pop"] if artist == "The Artist" else []
                return {"toptags": {"tag": tags}}

        lfm = DisambiguatedArtist()
        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), lfm, {7: "The Artist"}))
        self.assertEqual(lfm.calls, [
            ("The Artist (2)", 0), ("The Artist (2)", 1), ("The Artist", 0)])
        self.assertEqual(result["write"]["taxonomies"]["genre"], ["Pop"])

    def test_spotify_artist_genres_are_final_exact_id_fallback(self):
        class SpotifyArtistGenres(SpotifyFake):
            album_data = {**SpotifyFake.album_data,
                          "artists": [{"id": "artist-id", "name": "The Artist"}]}

            def artist(self, artist_id):
                self.artist_id = artist_id
                return {"genres": ["indie pop", "Pop", "pop"]}

        class EmptyLastFmTags(LastFmFake):
            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info["toptags"] = {"tag": []}
                return info

        spotify = SpotifyArtistGenres()
        result = cast(dict, mod.enrich(
            make_post(), spotify, EmptyLastFmTags(), {7: "The Artist"}))
        self.assertEqual(spotify.artist_id, "artist-id")
        self.assertEqual(result["write"]["taxonomies"]["genre"], ["indie pop", "Pop"])

    def test_album_top_tags_failure_continues_to_artist_tags(self):
        class FailedAlbumTags(LastFmFake):
            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info["toptags"] = {"tag": []}
                return info

            def album_gettoptags(self, **kwargs):
                raise RuntimeError("optional album tags failed")

            def artist_gettoptags(self, *args, **kwargs):
                return {"toptags": {"tag": ["Pop"]}}

        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), FailedAlbumTags(), {7: "The Artist"}))
        self.assertEqual(result["write"]["taxonomies"]["genre"], ["Pop"])
        self.assertNotIn("lastfm_provider_error",
                         [row["code"] for row in result["diagnostics"]])

    def test_artist_top_tags_failure_preserves_enrichment_without_genre(self):
        class FailedArtistTags(LastFmFake):
            def album_getinfo(self, **kwargs):
                info = super().album_getinfo(**kwargs)
                info["toptags"] = {"tag": []}
                return info

            def album_gettoptags(self, **kwargs):
                return {"toptags": {"tag": []}}

            def artist_gettoptags(self, *args, **kwargs):
                raise RuntimeError("optional artist tags failed")

        result = cast(dict, mod.enrich(
            make_post(), SpotifyFake(), FailedArtistTags(), {7: "The Artist"}))
        self.assertEqual(result["write"]["acf"]["spotify_album_id"], "album-id")
        self.assertNotIn("genre", result["write"]["taxonomies"])
        self.assertIn("lastfm_no_tags", [row["code"] for row in result["diagnostics"]])

    def test_managed_helper_rejects_unmanaged_keys_and_unknown_policy(self):
        with self.assertRaisesRegex(ValueError, "Unmanaged ACF key"):
            mod._set_managed({}, {}, "music_rating", 5, mod.WRITE_OVERWRITE_MANAGED)
        with self.assertRaisesRegex(ValueError, "Unknown write policy"):
            mod._set_managed({}, {}, "spotify_title", "x", "surprise")

    def test_overwrite_rebuilds_managed_values_and_protects_editor_data(self):
        old_rows = [
            {"spotify_id": "one", "highlight": True, "title": "old"},
            {"spotify_id": "two", "highlight": False, "title": "also old"},
            {"spotify_id": "removed", "highlight": True, "title": "gone"},
        ]
        post = make_post(
            acf={"spotify_title": SpotifyFake.album_data["name"],
                 "music_tracks": old_rows, "lastfm_url": "https://last.fm/old",
                 "mbid": "old-mbid", "music_rating": 5,
                 "music_favorite": True, "music_notes": "keep"},
            artist=[90], genre=[91], release_type=[92], categories=[42, 5])
        body = enrich(post, write_policy=mod.WRITE_OVERWRITE_MANAGED)
        acf = body["acf"]
        self.assertEqual(acf["spotify_title"], SpotifyFake.album_data["name"])
        self.assertEqual([row["spotify_id"] for row in acf["music_tracks"]], ["one", "two"])
        self.assertEqual([row["highlight"] for row in acf["music_tracks"]], [True, False])
        for key in mod.EDITOR_OWNED_ACF_FIELDS:
            self.assertNotIn(key, acf)
        self.assertNotIn("lastfm_url", acf)
        self.assertNotIn("mbid", acf)
        self.assertEqual(body["taxonomies"]["artist"], ["The Artist"])
        self.assertEqual(body["taxonomies"]["genre"], ["Rock", "Pop", "Ambient"])
        self.assertEqual(body["taxonomies"]["release_type"], ["Single"])
        self.assertEqual(body["categories"], [42, 5])

    def test_overwrite_writes_every_valid_managed_field(self):
        mbid = "123e4567-e89b-12d3-a456-426614174000"
        lastfm_url = "https://www.last.fm/music/The+Artist/Album"

        class CompleteLastFm(LastFmFake):
            def album_search(self, *args, **kwargs):
                return [{"name": SpotifyFake.album_data["name"],
                         "artist": "The Artist", "mbid": mbid, "url": lastfm_url}]

            def album_getinfo(self, **kwargs):
                return {**super().album_getinfo(**kwargs),
                        "mbid": mbid, "url": lastfm_url}

        existing: dict[str, object] = {name: 1 for name in mod.AUTO_FILLABLE_FIELDS}
        existing["music_tracks"] = [{"spotify_id": "one", "highlight": True}]
        result = cast(dict, mod.enrich(
            make_post(acf=existing), SpotifyFake(), CompleteLastFm(),
            {7: "The Artist"}, mod.WRITE_OVERWRITE_MANAGED))["write"]["acf"]

        self.assertEqual(set(result), set(mod.AUTO_FILLABLE_FIELDS))
        self.assertEqual(result["spotify_title"], SpotifyFake.album_data["name"])
        self.assertEqual(result["spotify_album_id"], "album-id")
        self.assertEqual(result["spotify_album_url"],
                         "https://open.spotify.com/album/album-id")
        self.assertEqual(result["music_release_date"], "03/02/2024")
        self.assertEqual(result["music_listened_at"], "04/03/2024")
        self.assertEqual(result["lastfm_url"], lastfm_url)
        self.assertEqual(result["mbid"], mbid)
        self.assertEqual(result["music_length_ms"], 3000)
        self.assertEqual(result["music_total_tracks"], 2)
        self.assertEqual(result["music_avg_track_ms"], 1500)
        self.assertFalse(result["music_explicit"])
        self.assertEqual(result["listen_count"], 1)
        self.assertEqual([row["highlight"] for row in result["music_tracks"]],
                         [True, False])

    def test_overwrite_missing_genres_omits_replacement(self):
        class EmptyGenres(LastFmFake):
            def album_getinfo(self, **kwargs):
                data = super().album_getinfo(**kwargs)
                data["toptags"] = {"tag": ["AOTY", "The Artist"]}
                return data
        result = cast(dict, mod.enrich(
            make_post(genre=[98]), SpotifyFake(), EmptyGenres(), {7: "The Artist"},
            mod.WRITE_OVERWRITE_MANAGED))["write"]
        self.assertNotIn("genre", result["taxonomies"])

    def test_empty_spotify_tracks_are_unresolved_in_overwrite_mode(self):
        class EmptySpotify(SpotifyFake):
            tracks = []

        result = cast(dict, mod.enrich(
            make_post(), EmptySpotify(), LastFmFake(), {7: "The Artist"},
            mod.WRITE_OVERWRITE_MANAGED))
        self.assertNotIn("write", result)
        self.assertEqual(result["diagnostics"], [{
            "code": "spotify_provider_error",
            "message": "Spotify track.list returned no tracks.",
            "details": {
                "provider": "spotify", "operation": "track.list",
                "failure_kind": "malformed_response", "retryable": False,
                "attempts": 1, "circuit_state": "closed",
            },
        }])

    def test_malformed_spotify_track_rows_are_provider_errors_before_lastfm(self):
        malformed_values = [
            ("id", None), ("name", ""), ("duration_ms", True),
            ("duration_ms", "1000"), ("duration_ms", 0), ("duration_ms", -1),
            ("disc_number", False), ("track_number", 1.5),
            ("explicit", None), ("explicit", "false"), ("explicit", 0),
        ]
        for field, value in malformed_values:
            with self.subTest(field=field, value=value):
                class MalformedSpotify(SpotifyFake):
                    tracks = [dict(SpotifyFake.tracks[0]), dict(SpotifyFake.tracks[1])]
                if value is None and field == "explicit":
                    del MalformedSpotify.tracks[0][field]
                else:
                    MalformedSpotify.tracks[0][field] = value
                lfm = LastFmFake()
                with patch.object(lfm, "album_search", wraps=lfm.album_search) as search:
                    result = cast(dict, mod.enrich(
                        make_post(), MalformedSpotify(), lfm, {7: "The Artist"}))
                search.assert_not_called()
                self.assertNotIn("write", result)
                diagnostic = result["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "spotify_provider_error")
                self.assertEqual(diagnostic["details"]["operation"], "track.list")
                self.assertEqual(diagnostic["details"]["failure_kind"],
                                 "malformed_response")
                self.assertFalse(diagnostic["details"]["retryable"])

    def test_complete_post_skips_only_in_fill_only_mode(self):
        acf: dict[str, object] = {name: 1 for name in mod.AUTO_FILLABLE_FIELDS}
        acf["music_explicit"] = False
        acf["music_tracks"] = [{"spotify_id": "one", "highlight": True}]
        post = make_post(acf=acf, artist=[1], release_type=[2])
        spt = SpotifyFake()
        with patch.object(spt, "search_albums", wraps=spt.search_albums) as search:
            self.assertIsNone(mod.enrich(post, spt, LastFmFake(), {7: "The Artist"}))
            search.assert_not_called()
            result = mod.enrich(post, spt, LastFmFake(), {7: "The Artist"},
                                mod.WRITE_OVERWRITE_MANAGED)
            self.assertIn("write", cast(dict, result))
            self.assertTrue(search.called)

    def test_no_accepted_genres_emits_no_genre_and_creates_no_unknown(self):
        class EmptyGenres(LastFmFake):
            def album_getinfo(self, **kwargs):
                data = super().album_getinfo(**kwargs)
                data["toptags"] = {"tag": ["AOTY", "The Artist"]}
                return data
        wp = WordPressFake()
        body = cast(dict, mod.enrich(make_post(), SpotifyFake(), EmptyGenres(), {7: "The Artist"}))["write"]
        self.assertNotIn("genre", body["taxonomies"])
        self.assertNotIn(("genre", "Unknown"), wp.resolved)


if __name__ == "__main__":
    unittest.main()
