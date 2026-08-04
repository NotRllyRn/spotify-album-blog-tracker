import importlib
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from album_metadata.common import match_key, post_dmy
from album_metadata.plans import materialize_body
from album_metadata.schema import REMOVED_ACF_FIELDS
from metadata_cli.cli import apply_patches, load_env
from models import Artist, LifecycleStatus, Release, ReleaseType, Track, WordPressPost
from publisher import Publisher
from wordpress_client import WordPressClient

TrackerMetadata = importlib.import_module("tracker_metadata").TrackerMetadata


class SpotifyFake:
    album_data = {
        "id": "album-id",
        "name": "Élan",
        "artists": [{"id": "artist-id", "name": "The Artist"}],
        "total_tracks": 2,
        "album_type": "single",
        "release_date": "2024-02-03",
    }
    tracks = [
        {"id": "one", "name": "First", "duration_ms": 1_000,
         "disc_number": 1, "track_number": 1, "explicit": False},
        {"id": "two", "name": "Second", "duration_ms": 2_000,
         "disc_number": 1, "track_number": 2, "explicit": True},
    ]

    def album(self, album_id):
        assert album_id == "album-id"
        return dict(self.album_data)

    def all_tracks(self, album_id):
        assert album_id == "album-id"
        return list(self.tracks)

    def artist(self, artist_id):
        return {"genres": []}


class LastFmFake:
    def album_search(self, *args, **kwargs):
        return [{"name": "Élan", "artist": "The Artist"}]

    def album_getinfo(self, **kwargs):
        return {
            "name": "Élan",
            "artist": "The Artist",
            "tracks": {},
            "toptags": {"tag": [{"name": "Rock"}, {"name": "Pop"}]},
        }

    def album_gettoptags(self, **kwargs):
        return {"toptags": {"tag": []}}

    def artist_gettoptags(self, *args, **kwargs):
        return {"toptags": {"tag": []}}


def make_release() -> Release:
    return Release(
        spotify_id="album-id",
        title="Élan",
        normalized_title="élan",
        artists=[Artist("artist-id", "The Artist", "the artist")],
        release_type=ReleaseType.SINGLE,
        raw_spotify_type="single",
        cover_url="https://example.test/cover.jpg",
        release_date="2024-02-03",
        total_tracks=2,
        total_duration_ms=3_000,
        tracks=[
            Track("one", "First", "first", 1_000, 1, 1, True, True,
                  explicit=False, highlight=True),
            Track("two", "Second", "second", 2_000, 1, 2, True, True,
                  explicit=True),
        ],
        progress=1.0,
        status=LifecycleStatus.ACTIVE,
        first_seen=datetime(2024, 3, 4),
        last_seen=datetime(2024, 3, 4),
    )


def term_ids():
    return {
        "artist": {match_key("The Artist"): 10},
        "genre": {match_key("Rock"): 20, match_key("Pop"): 21},
        "release_type": {match_key("Single"): 30},
    }


class CliWordPressFake:
    def __init__(self):
        self.body = None

    def list_tax_terms(self, taxonomy):
        return {
            "artist": {"The Artist": 10},
            "genre": {"Rock": 20, "Pop": 21},
            "release_type": {"Single": 30},
        }[taxonomy]

    def create_term(self, taxonomy, name):
        raise AssertionError(f"unexpected term creation: {taxonomy}/{name}")

    def update_post(self, post_id, body):
        self.body = body


class TrackerMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = SimpleNamespace(
            spotify_client_id="id", spotify_client_secret="secret", lastfm_api_key="key")
        self.adapter = TrackerMetadata(config, SpotifyFake(), LastFmFake())
        self.release = make_release()
        self.post = {
            "id": 42,
            "title": {"rendered": "Élan"},
            "date": "2024-03-04T12:00:00",
        }

    async def test_known_spotify_path_uses_shared_managed_contract(self):
        patch = await self.adapter.build_patch(self.release, self.post, [7], [5], 3)
        acf = patch["write"]["acf"]

        self.assertEqual(acf["listen_count"], 3)
        self.assertTrue(acf["music_tracks"][0]["highlight"])
        self.assertEqual(acf["music_release_date"], "03/02/2024")
        self.assertEqual(acf["music_listened_at"], "04/03/2024")
        self.assertFalse(REMOVED_ACF_FIELDS & set(acf))
        self.assertEqual(patch["write"]["taxonomies"]["release_type"], ["Single"])

    async def test_cli_and_tracker_materialize_identical_managed_request_body(self):
        patch = await self.adapter.build_patch(self.release, self.post, [7], [5], 3)
        expected = materialize_body(patch["write"], term_ids())
        wordpress = CliWordPressFake()

        succeeded, failed = apply_patches(wordpress, [patch])

        self.assertEqual((succeeded, failed), ([42], []))
        self.assertEqual(wordpress.body, expected)

    async def test_publisher_sends_shared_body_and_preserves_editor_values(self):
        patch = await self.adapter.build_patch(self.release, self.post, [7], [5], 3)
        self.release.rating = 91
        self.release.favorite = True
        self.release.notes = "Editorial notes"
        created, updates = {}, []

        class WordPressFake:
            async def create_post(_, data):
                created.update(data)
                return {**self.post, **data, "title": {"rendered": data["title"]}}

            async def resolve_taxonomy_terms(_, wanted):
                return term_ids()

            async def update_post(_, post_id, body):
                updates.append(body)
                return {"id": post_id}

        publisher: Any = Publisher.__new__(Publisher)
        publisher.wordpress = WordPressFake()
        publisher.db = AsyncMock()
        publisher.category_cache = {"Single": 5}
        publisher.metadata = SimpleNamespace(
            build_patch=AsyncMock(return_value=patch),
            editor_acf=TrackerMetadata.editor_acf,
        )
        publisher._fill_scf_enabled = True
        publisher._count_listen_index = AsyncMock(return_value=3)
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[7])
        publisher.refresh_post_cache = AsyncMock()

        result = await publisher.publish_release(self.release)

        self.assertEqual(updates, [materialize_body(patch["write"], term_ids())])
        self.assertEqual(created["acf"], {
            "music_rating": 91,
            "music_favorite": True,
            "music_notes": "Editorial notes",
        })
        self.assertEqual(result.scf_pending_tags, [])
        self.assertEqual(result.listen_count, 3)

    async def test_publisher_surfaces_metadata_failure_without_losing_post(self):
        class WordPressFake:
            async def create_post(_, data):
                return {**self.post, **data, "title": {"rendered": data["title"]}}

        publisher: Any = Publisher.__new__(Publisher)
        publisher.wordpress = WordPressFake()
        publisher.db = AsyncMock()
        publisher.category_cache = {"Single": 5}
        publisher.metadata = SimpleNamespace(
            build_patch=AsyncMock(side_effect=RuntimeError("provider down")))
        publisher._fill_scf_enabled = True
        publisher._count_listen_index = AsyncMock(return_value=1)
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[7])
        publisher.refresh_post_cache = AsyncMock()

        result = await publisher.publish_release(self.release)

        self.assertEqual(result.post["id"], 42)
        self.assertEqual(result.scf_pending_tags, ["metadata_error"])

    async def test_listen_count_counts_matching_title_and_artist(self):
        publisher: Any = Publisher.__new__(Publisher)
        publisher.db = SimpleNamespace(get_wordpress_posts=AsyncMock(return_value=[
            WordPressPost(1, "Élan", "élan", ["The Artist"], ["the artist"], "one"),
            WordPressPost(2, "Other", "other", ["The Artist"], ["the artist"], "two"),
        ]))

        self.assertEqual(await publisher._count_listen_index(self.release), 2)


class WordPressTaxonomyTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_existing_and_new_terms_before_update(self):
        class Response:
            headers = {"X-WP-TotalPages": "1"}

            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                return None

            def json(self):
                return self.value

        class Client:
            async def get(self, url, params=None):
                rows = [{"id": 10, "name": "The Artist"}] if url.endswith("/artist") else []
                return Response(rows)

            async def post(self, url, json):
                ids = {"Rock": 20, "Pop": 21, "Single": 30}
                return Response({"id": ids[json["name"]], "name": json["name"]})

        wordpress: Any = WordPressClient.__new__(WordPressClient)
        wordpress.api_url = "https://example.test/wp-json/wp/v2"
        wordpress.client = Client()

        resolved = await wordpress.resolve_taxonomy_terms({
            "artist": ["the artist"], "genre": ["Rock", "Pop"],
            "release_type": ["Single"],
        })

        self.assertEqual(resolved, term_ids())


class SharedDateTests(unittest.TestCase):
    def test_cli_accepts_the_tracker_wordpress_url_name(self):
        with patch.dict(os.environ, {"WORDPRESS_URL": "https://example.test"}, clear=True):
            self.assertEqual(load_env(None)["WORDPRESS_BASE_URL"], "https://example.test")

    def test_partial_spotify_dates_expand_for_scf(self):
        self.assertEqual(post_dmy("2024"), "01/01/2024")
        self.assertEqual(post_dmy("2024-03"), "01/03/2024")
        self.assertEqual(post_dmy("2024-03-15T14:30:00"), "15/03/2024")
