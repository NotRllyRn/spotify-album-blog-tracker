import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from typing import cast

import post_to_album as mod


class FakeSpotify:
    def __init__(self):
        self.queries = []
        self.album_data = {
            "id": "spotify-1", "name": "Été & Amour (Deluxe Edition)",
            "artists": [{"name": "Beyoncé"}], "total_tracks": 7,
            "album_type": "album", "release_date": "2026-07-05",
        }
        self.tracks = [
            {"disc_number": 1, "track_number": i, "name": f"Chanson {i}",
             "duration_ms": 100000, "id": f"track-{i}", "explicit": i == 1}
            for i in range(1, 8)
        ]

    def search_albums(self, query, limit=10):
        self.queries.append(query)
        return [{"id": "spotify-1", "name": self.album_data["name"],
                 "artists": self.album_data["artists"], "total_tracks": 7}]

    def album(self, album_id):
        return self.album_data

    def all_tracks(self, album_id):
        return self.tracks


class FakeLastFM:
    def __init__(self):
        self.calls = []

    def album_search(self, album, limit=10):
        self.calls.append(("search", album, limit))
        return [{"name": album, "artist": "Beyoncé", "mbid": ""}]

    def album_getinfo(self, **kwargs):
        self.calls.append(("getinfo", kwargs))
        return {"name": "Été & Amour (Deluxe Edition)", "artist": "Beyoncé",
                "toptags": {"tag": [{"name": "Pop"}]}}


class FakeWordPress:
    def __init__(self):
        self.updated = []
        self.created = []

    def list_tax_terms(self, taxonomy):
        return {"Album": 30} if taxonomy == "release_type" else {}

    def create_term(self, taxonomy, name):
        self.created.append((taxonomy, name))
        raise AssertionError("existing nonempty artist/genre must not be resolved")

    def update_post(self, post_id, body):
        self.updated.append((post_id, body))
        return {}


class EndToEndWorkflowTests(unittest.TestCase):
    def test_enrich_serialize_reload_and_apply_exact_integer_body(self):
        post = {
            "id": 42, "title": {"rendered": "Été &amp; Amour (Deluxe Edition)"},
            "date": "2026-07-06T12:00:00", "modified": "2026-07-07T00:00:00",
            "tags": [9], "categories": [93, 200, 777, 5],
            "artist": [501], "genre": [601], "release_type": [],
            "acf": {"spotify_album_id": "keep-existing", "music_notes": "keep"},
        }
        spotify, lastfm = FakeSpotify(), FakeLastFM()
        result = mod.enrich(post, spotify, lastfm, {9: "Beyoncé"})
        self.assertIsNotNone(result)
        result = cast(dict, result)

        self.assertIn('album:"Été & Amour (Deluxe Edition)"', spotify.queries[0])
        self.assertEqual(lastfm.calls, [
            ("search", "Été & Amour (Deluxe Edition) Beyoncé", 10),
            ("search", "Été & Amour (Deluxe Edition)", 10),
            ("getinfo", {"artist": "Beyoncé", "album": "Été & Amour (Deluxe Edition)",
                         "autocorrect": 0}),
        ])
        self.assertNotIn("spotify_album_id", result["write"]["acf"])
        self.assertNotIn("artist", result["write"]["taxonomies"])
        self.assertNotIn("genre", result["write"]["taxonomies"])
        self.assertNotIn("mbid", result["matches"]["lastfm"])
        self.assertEqual(result["diagnostics"][0]["code"], "lastfm_no_mbid")
        self.assertEqual(result["write"]["categories"], [93, 200, 777, 6])

        plan = {"schema_version": 2, "generated_at": "2026-07-23T00:00:00Z",
                "write_policy": mod.WRITE_FILL_ONLY, "patches": [result]}
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "planned.json"
            mod.write_json_atomic(plan_path, mod.validate_plan(plan))
            wp = FakeWordPress()
            env = {"WORDPRESS_BASE_URL": "https://invalid.example",
                   "WORDPRESS_USERNAME": "user", "WORDPRESS_APP_PASSWORD": "password"}
            args = Namespace(plan=str(plan_path), offset=0, limit=None, out_dir=directory)
            with patch.object(mod, "WordPress", return_value=wp), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
                self.assertEqual(mod.cmd_apply_plan(args, env), 0)

            expected_acf = dict(result["write"]["acf"])
            expected_acf.update({
                "music_release_date": "20260705",
                "music_listened_at": "20260706",
            })
            self.assertEqual(wp.updated, [(42, {
                "acf": expected_acf,
                "categories": [93, 200, 777, 6],
                "release_type": [30],
            })])
            self.assertTrue(all(type(value) is int for value in wp.updated[0][1]["release_type"]))
            self.assertEqual(json.loads((Path(directory) / "applied.json").read_text())["succeeded"], [42])
            self.assertFalse((Path(directory) / "applied.json.tmp").exists())

    def test_main_command_credential_matrix(self):
        cases = [
            (["run", "--dry-run"],
             ["WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD",
              "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "LASTFM_API_KEY"], "cmd_run"),
            (["apply-plan", "plan.json"],
             ["WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD"], "cmd_apply_plan"),
            (["stats"],
             ["WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD"], "cmd_stats"),
            (["fuzzy", "Album"],
             ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"], "cmd_fuzzy"),
        ]
        all_names = ["WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD",
                     "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "LASTFM_API_KEY"]
        for argv, required, command in cases:
            with self.subTest(argv=argv), patch.dict(os.environ, {}, clear=True), \
                 patch.object(mod, "load_env", return_value={name: "" for name in all_names}), \
                 patch.object(mod, command) as handler:
                with self.assertRaisesRegex(SystemExit, ", ".join(required)):
                    mod.main(argv)
                handler.assert_not_called()

            supplied = {name: "value" for name in required}
            with self.subTest(argv=argv, supplied=True), \
                 patch.object(mod, "load_env", return_value=supplied), \
                 patch.object(mod, command, return_value=0) as handler:
                self.assertEqual(mod.main(argv), 0)
                handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
