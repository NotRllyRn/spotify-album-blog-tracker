import unittest
from copy import deepcopy
from typing import cast

from album_metadata.common import match_key
from album_metadata.enrichment import enrich, enrich_known
from album_metadata.plans import materialize_body
from album_metadata.schema import WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED


class SpotifyFixture:
    def __init__(self, album, tracks):
        self.album_data = album
        self.tracks = tracks

    def search_albums(self, query, limit=10):
        return [dict(self.album_data)]

    def album(self, album_id):
        return dict(self.album_data)

    def all_tracks(self, album_id):
        return deepcopy(self.tracks)

    def artist(self, artist_id):
        return {"genres": []}


class LastFmFixture:
    def __init__(self, album, tags=(), mbid=""):
        self.album = album
        self.tags = list(tags)
        self.mbid = mbid

    @property
    def primary_artist(self):
        return self.album["artists"][0]["name"]

    def album_search(self, *args, **kwargs):
        return [{"name": self.album["name"], "artist": self.primary_artist,
                 "mbid": self.mbid}]

    def album_getinfo(self, **kwargs):
        return {
            "name": self.album["name"],
            "artist": self.primary_artist,
            "mbid": self.mbid,
            "url": "https://www.last.fm/music/artist/album",
            "tracks": {},
            "toptags": {"tag": [{"name": tag} for tag in self.tags]},
        }

    def album_gettoptags(self, **kwargs):
        return {"toptags": {"tag": []}}

    def artist_gettoptags(self, *args, **kwargs):
        return {"toptags": {"tag": []}}


def tracks(count, explicit=False):
    return [
        {
            "id": f"track-{index}",
            "name": f"Track {index}",
            "duration_ms": 240_000,
            "disc_number": 1,
            "track_number": index,
            "explicit": explicit and index == 1,
        }
        for index in range(1, count + 1)
    ]


def term_ids(write):
    return {
        taxonomy: {
            match_key(name): index
            for index, name in enumerate(names, start=100)
        }
        for taxonomy, names in write.get("taxonomies", {}).items()
    }


class InterfaceParityTests(unittest.TestCase):
    def assert_parity(
        self,
        *,
        album_type,
        track_count,
        release_date,
        artists=("The Artist",),
        tags=("Rock", "Pop"),
        mbid="",
        categories=(),
        explicit=False,
    ):
        album = {
            "id": "album-id",
            "name": "Parity Release",
            "artists": [
                {"id": f"artist-{index}", "name": name}
                for index, name in enumerate(artists, start=1)
            ],
            "total_tracks": track_count,
            "album_type": album_type,
            "release_date": release_date,
        }
        spotify_tracks = tracks(track_count, explicit)
        spotify = SpotifyFixture(album, spotify_tracks)
        lastfm = LastFmFixture(album, tags, mbid)
        tag_map = {index: name for index, name in enumerate(artists, start=1)}
        post = {
            "id": 42,
            "title": {"rendered": album["name"]},
            "date": "2024-03-04T12:00:00",
            "tags": list(tag_map),
            "acf": {},
            "categories": list(categories),
            "artist": [],
            "genre": [],
            "release_type": [],
        }

        cli_patch = cast(dict, enrich(post, spotify, lastfm, tag_map))
        tracker_patch = cast(dict, enrich_known(
            post, spotify, lastfm, album, spotify_tracks, list(artists)))

        self.assertIsNotNone(cli_patch)
        self.assertIsNotNone(tracker_patch)
        self.assertEqual(cli_patch["write"], tracker_patch["write"])
        ids = term_ids(cli_patch["write"])
        self.assertEqual(
            materialize_body(cli_patch["write"], ids),
            materialize_body(tracker_patch["write"], ids),
        )
        return cli_patch["write"]

    def test_release_shape_matrix(self):
        cases = [
            ({"album_type": "album", "track_count": 7, "release_date": "2024"}, "Album"),
            ({"album_type": "album", "track_count": 4, "release_date": "2024-03"}, "EP"),
            ({"album_type": "single", "track_count": 2,
              "release_date": "2024-03-15", "explicit": True}, "Single"),
            ({"album_type": "compilation", "track_count": 8,
              "release_date": "2024-03-15"}, "Compilation"),
            ({"album_type": "album", "track_count": 7,
              "release_date": "2024-03-15", "artists": ("The Artist", "Guest")}, "Album"),
            ({"album_type": "single", "track_count": 1,
              "release_date": "2024-03-15", "tags": (), "categories": (50,)}, "Single"),
            ({"album_type": "album", "track_count": 7,
              "release_date": "2024-03-15",
              "mbid": "123e4567-e89b-12d3-a456-426614174000"}, "Album"),
        ]
        for case, expected_type in cases:
            with self.subTest(case=case):
                write = self.assert_parity(**case)
                self.assertEqual(write["taxonomies"]["release_type"], [expected_type])

    def test_relisten_category_and_explicit_track_are_identical(self):
        write = self.assert_parity(
            album_type="single",
            track_count=2,
            release_date="2024-03",
            categories=(50,),
            explicit=True,
        )
        self.assertIn(50, write["categories"])
        self.assertTrue(write["acf"]["music_explicit"])

    def test_overwrite_managed_preserves_identical_highlights(self):
        album = {
            "id": "album-id", "name": "Parity Release",
            "artists": [{"id": "artist-1", "name": "The Artist"}],
            "total_tracks": 2, "album_type": "single", "release_date": "2024-03-15",
        }
        spotify_tracks = tracks(2)
        spotify = SpotifyFixture(album, spotify_tracks)
        lastfm = LastFmFixture(album, ("Rock",))
        existing_tracks = [{"spotify_id": "track-1", "highlight": True}]
        post = {
            "id": 42, "title": {"rendered": album["name"]},
            "date": "2024-03-04", "tags": [1], "categories": [5],
            "artist": [10], "genre": [20], "release_type": [30],
            "acf": {"spotify_title": "Old", "music_tracks": existing_tracks},
        }

        cli_patch = cast(dict, enrich(
            post, spotify, lastfm, {1: "The Artist"}, WRITE_OVERWRITE_MANAGED))
        tracker_patch = cast(dict, enrich_known(
            post, spotify, lastfm, album, spotify_tracks, ["The Artist"],
            WRITE_OVERWRITE_MANAGED, track_highlights={"track-1": True}))

        self.assertEqual(cli_patch["write"], tracker_patch["write"])
        self.assertTrue(cli_patch["write"]["acf"]["music_tracks"][0]["highlight"])

    def test_fill_only_keeps_existing_managed_values_in_both_paths(self):
        album = {
            "id": "album-id", "name": "Parity Release",
            "artists": [{"id": "artist-1", "name": "The Artist"}],
            "total_tracks": 1, "album_type": "single", "release_date": "2024-03-15",
        }
        spotify_tracks = tracks(1)
        spotify = SpotifyFixture(album, spotify_tracks)
        lastfm = LastFmFixture(album, ())
        post = {
            "id": 42, "title": {"rendered": album["name"]},
            "date": "2024-03-04", "tags": [1], "categories": [],
            "artist": [], "genre": [], "release_type": [],
            "acf": {"spotify_title": "Keep me"},
        }

        cli_patch = cast(dict, enrich(
            post, spotify, lastfm, {1: "The Artist"}, WRITE_FILL_ONLY))
        tracker_patch = cast(dict, enrich_known(
            post, spotify, lastfm, album, spotify_tracks,
            ["The Artist"], WRITE_FILL_ONLY))

        self.assertEqual(cli_patch["write"], tracker_patch["write"])
        self.assertNotIn("spotify_title", cli_patch["write"]["acf"])

    def test_lastfm_failure_produces_no_write_in_both_paths(self):
        class FailingLastFm(LastFmFixture):
            def album_search(self, *args, **kwargs):
                raise OSError("network unavailable")

        album = {
            "id": "album-id", "name": "Parity Release",
            "artists": [{"id": "artist-1", "name": "The Artist"}],
            "total_tracks": 1, "album_type": "single", "release_date": "2024-03-15",
        }
        spotify_tracks = tracks(1)
        spotify = SpotifyFixture(album, spotify_tracks)
        lastfm = FailingLastFm(album)
        post = {
            "id": 42, "title": {"rendered": album["name"]},
            "date": "2024-03-04", "tags": [1], "acf": {}, "categories": [],
            "artist": [], "genre": [], "release_type": [],
        }

        cli_result = cast(dict, enrich(post, spotify, lastfm, {1: "The Artist"}))
        tracker_result = cast(dict, enrich_known(
            post, spotify, lastfm, album, spotify_tracks, ["The Artist"]))

        self.assertNotIn("write", cli_result)
        self.assertEqual(cli_result, tracker_result)
