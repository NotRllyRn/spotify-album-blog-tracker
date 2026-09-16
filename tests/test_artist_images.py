import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from album_metadata.providers import ProviderCircuit
from artist_images import (
    artist_image_entry,
    artist_search_ladder,
    choose_artist_candidate,
    image_is_missing,
    select_artist_image,
    validate_artist_image_plan,
)
from metadata_cli import cli
from publisher import Publisher


def artist(
    spotify_id: str = "1234567890123456789012",
    name: str = "The Artist",
    popularity: int = 80,
    images=None,
):
    return {
        "id": spotify_id,
        "name": name,
        "popularity": popularity,
        "images": images if images is not None else [
            {"url": "https://i.scdn.co/image/large", "width": 640, "height": 640},
            {"url": "https://i.scdn.co/image/medium", "width": 320, "height": 320},
            {"url": "https://i.scdn.co/image/small", "width": 160, "height": 160},
        ],
    }


def entry():
    match = {
        "candidate": artist(),
        "score": 1.0,
        "selection_evidence": "exact_name",
    }
    return artist_image_entry(10, "The Artist", match)


class ArtistImageCoreTests(unittest.TestCase):
    def test_selects_largest_native_image_with_both_dimensions_bounded(self):
        value = artist(images=[
            {"url": "https://i.scdn.co/image/wide", "width": 256, "height": 300},
            {"url": "https://i.scdn.co/image/unknown", "width": None, "height": None},
            {"url": "https://i.scdn.co/image/256", "width": 256, "height": 256},
            {"url": "https://i.scdn.co/image/160", "width": 160, "height": 160},
        ])
        self.assertEqual(select_artist_image(value)["url"], "https://i.scdn.co/image/256")

    def test_matching_uses_exact_name_then_unique_popularity_and_keeps_ties_ambiguous(self):
        primary = artist(name="$uicideboy$", popularity=85)
        duplicate = artist("ABCDEFGHIJ123456789012", "$uicideBoy$", 19)
        result = choose_artist_candidate([duplicate, primary], "$uicideboy$")
        self.assertEqual(result["candidate"]["id"], primary["id"])
        self.assertEqual(result["selection_evidence"], "unique_popularity")

        duplicate["popularity"] = primary["popularity"]
        self.assertEqual(
            choose_artist_candidate([primary, duplicate], "$uicideboy$")["reason"],
            "spotify_artist_ambiguous",
        )

    def test_search_ladder_is_quoted_first_and_deduplicates(self):
        candidate = artist()

        class Spotify:
            def __init__(self):
                self.queries = []

            def search_artists(self, query, limit=10):
                self.queries.append((query, limit))
                return [candidate]

        spotify = Spotify()
        self.assertEqual(artist_search_ladder(spotify, "The Artist"), [candidate])
        self.assertEqual(spotify.queries, [('artist:"The Artist"', 10)])

    def test_plan_validation_rejects_oversized_or_untrusted_images(self):
        plan = {
            "schema_version": 1,
            "generated_at": "2026-09-16T00:00:00Z",
            "max_dimension": 256,
            "images": [entry()],
        }
        validate_artist_image_plan(plan)
        for key, value in (("width", 257), ("url", "https://example.com/image")):
            changed = json.loads(json.dumps(plan))
            changed["images"][0]["image"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_artist_image_plan(changed)

    def test_false_and_absent_acf_images_are_missing_but_media_ids_are_not(self):
        self.assertTrue(image_is_missing({"acf": {"image": False}}))
        self.assertTrue(image_is_missing({}))
        self.assertFalse(image_is_missing({"acf": {"image": 42}}))


class ArtistImageCliTests(unittest.TestCase):
    env = {
        "WORDPRESS_BASE_URL": "https://example.test",
        "WORDPRESS_USERNAME": "user",
        "WORDPRESS_APP_PASSWORD": "password",
        "SPOTIFY_CLIENT_ID": "client",
        "SPOTIFY_CLIENT_SECRET": "secret",
    }

    def test_plan_command_scans_only_missing_terms_and_writes_review_artifacts(self):
        class WordPress:
            def list_taxonomy_terms(self, taxonomy):
                self.taxonomy = taxonomy
                return [
                    {"id": 1, "name": "Already Done", "acf": {"image": 99}},
                    {"id": 10, "name": "The Artist", "acf": {"image": False}},
                ]

        class Spotify:
            def __init__(self, *_):
                self._circuit = ProviderCircuit("spotify")

            def search_artists(self, query, limit=10):
                return [artist()]

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cli, "WordPress", return_value=WordPress()), \
                patch.object(cli, "Spotify", Spotify):
            args = Namespace(offset=0, limit=None, out_dir=directory)
            self.assertEqual(cli.cmd_artist_images(args, self.env), 0)
            planned = json.loads(
                (Path(directory) / "artist-images-planned.json").read_text())
            corrections = json.loads(
                (Path(directory) / "artist-images-corrections.json").read_text())

        self.assertEqual([row["term_id"] for row in planned["images"]], [10])
        self.assertEqual(planned["images"][0]["image"]["width"], 160)
        self.assertEqual(corrections["corrections"], [])

    def test_apply_is_fill_only_and_records_success_and_skip(self):
        terms = {
            10: {"id": 10, "name": "The Artist", "acf": {"image": False}},
            11: {"id": 11, "name": "Existing", "acf": {"image": 77}},
        }

        class WordPress:
            def get_taxonomy_term(self, taxonomy, term_id):
                return terms[term_id]

            def download_image(self, url):
                return b"image", "image/jpeg"

            def upload_media_bytes(self, *args):
                return {"id": 55}

            def update_taxonomy_term(self, taxonomy, term_id, body):
                terms[term_id]["acf"] = body["acf"]

            def delete_media(self, media_id):
                raise AssertionError("successful media must not be deleted")

        second = json.loads(json.dumps(entry()))
        second.update({"term_id": 11, "term_name": "Existing"})
        succeeded, skipped, failed = cli.apply_artist_image_entries(
            WordPress(), [entry(), second])
        self.assertEqual(succeeded[0]["media_id"], 55)
        self.assertEqual(skipped[0]["reason"], "image_already_present")
        self.assertEqual(failed, [])

    def test_apply_removes_uploaded_media_when_term_write_fails(self):
        deleted = []

        class WordPress:
            def get_taxonomy_term(self, taxonomy, term_id):
                return {"name": "The Artist", "acf": {"image": False}}

            def download_image(self, url):
                return b"image", "image/jpeg"

            def upload_media_bytes(self, *args):
                return {"id": 55}

            def update_taxonomy_term(self, *args):
                raise RuntimeError("term update failed")

            def delete_media(self, media_id):
                deleted.append(media_id)

        succeeded, skipped, failed = cli.apply_artist_image_entries(WordPress(), [entry()])
        self.assertEqual((succeeded, skipped, deleted), ([], [], [55]))
        self.assertEqual(failed[0]["term_id"], 10)

    def test_apply_command_exports_detailed_success_data(self):
        term = {"name": "The Artist", "acf": {"image": False}}

        class WordPress:
            def get_taxonomy_term(self, taxonomy, term_id):
                return term

            def download_image(self, url):
                return b"image", "image/jpeg"

            def upload_media_bytes(self, *args):
                return {"id": 55}

            def update_taxonomy_term(self, taxonomy, term_id, body):
                term["acf"] = body["acf"]

            def delete_media(self, media_id):
                raise AssertionError("successful media must not be deleted")

        plan = {
            "schema_version": 1,
            "generated_at": "2026-09-16T00:00:00Z",
            "max_dimension": 256,
            "images": [entry()],
        }
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "artist-images-planned.json"
            plan_path.write_text(json.dumps(plan))
            args = Namespace(plan=str(plan_path), offset=0, limit=None, out_dir=None)
            with patch.object(cli, "WordPress", return_value=WordPress()):
                self.assertEqual(cli.cmd_apply_artist_images(args, self.env), 0)
            applied = json.loads(
                (Path(directory) / "artist-images-applied.json").read_text())

        self.assertEqual(applied["succeeded"], [{
            "term_id": 10,
            "term_name": "The Artist",
            "spotify_artist_id": "1234567890123456789012",
            "media_id": 55,
        }])
        self.assertEqual((applied["skipped"], applied["failed"]), ([], []))


class ArtistImagePublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_release_artist_id_populates_and_verifies_term_image(self):
        terms = {10: {"acf": {"image": False}}}

        class WordPress:
            async def get_taxonomy_term(self, taxonomy, term_id):
                return terms[term_id]

            async def upload_media_bytes(self, *args):
                return {"id": 55}

            async def update_taxonomy_term(self, taxonomy, term_id, body):
                terms[term_id] = {"acf": body["acf"]}

            async def delete_media(self, media_id, force=True):
                raise AssertionError("successful media must not be deleted")

        response = SimpleNamespace(
            content=b"image",
            headers={"content-type": "image/jpeg"},
            url="https://i.scdn.co/image/small",
            raise_for_status=lambda: None,
        )
        client = SimpleNamespace(get=AsyncMock(return_value=response))

        class Context:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                return None

        publisher = Publisher.__new__(Publisher)
        publisher.wordpress = WordPress()
        publisher.spotify = SimpleNamespace(get_artist=AsyncMock(return_value=artist()))
        with patch("publisher.httpx.AsyncClient", return_value=Context()):
            result = await publisher._populate_artist_image(
                10, "1234567890123456789012", "The Artist")

        self.assertEqual(result, 55)
        self.assertEqual(terms[10]["acf"]["image"], 55)
        publisher.spotify.get_artist.assert_awaited_once_with("1234567890123456789012")


if __name__ == "__main__":
    unittest.main()
