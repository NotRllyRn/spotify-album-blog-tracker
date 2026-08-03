import copy
import io
import json
import math
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import post_to_album as mod


def patch_row(pid=1):
    return {"post_id": pid, "post_title": "Album", "source_modified": None,
            "matches": {"spotify": {"id": "s", "title": "Album", "artists": ["Artist"], "score": 1.0},
                        "lastfm": {"title": "Album", "artist": "Artist", "score": .9}},
            "write": {"acf": {"spotify_title": "Album", "music_explicit": False},
                      "categories": [93, 6],
                      "taxonomies": {"artist": ["Artist"], "release_type": ["Album"]}},
            "diagnostics": [{"code": "lastfm_no_mbid", "message": "No MBID"}]}


def plan(*rows, write_policy: Any = mod.WRITE_FILL_ONLY):
    return {"schema_version": 2, "generated_at": "2026-01-01T00:00:00Z",
            "write_policy": write_policy, "patches": list(rows or (patch_row(),))}


class FakeWP:
    def __init__(self, creation_succeeds=True):
        self.created = []
        self.updated = []
        self.creation_succeeds = creation_succeeds
    def list_tax_terms(self, tax):
        return {"Album": 30} if tax == "release_type" else {}
    def create_term(self, tax, name):
        self.created.append((tax, name)); return 10 if self.creation_succeeds else None
    def update_post(self, pid, body):
        self.updated.append((pid, body))
        if pid == 2: raise RuntimeError("failed")


class PlannedReplayTests(unittest.TestCase):
    @staticmethod
    def provider_unresolved(provider="spotify", failure_kind="http_status"):
        code = f"{provider}_provider_error"
        details = {
            "provider": provider, "operation": "album.search",
            "failure_kind": failure_kind, "retryable": True,
            "attempts": 3, "circuit_state": "closed",
        }
        if failure_kind == "http_status":
            details["http_status"] = 503
        if failure_kind == "circuit_open":
            details.update(attempts=0, circuit_state="open")
        return {"schema_version": 3, "unresolved": [{
            "post_id": 1, "post_title": "Album", "diagnostics": [{
                "code": code, "message": "Provider unavailable.", "details": details,
            }],
        }]}

    def test_unresolved_v3_provider_diagnostics_are_exact(self):
        mod.validate_unresolved(self.provider_unresolved())
        mod.validate_unresolved(self.provider_unresolved("lastfm", "circuit_open"))
        ordinary = {"schema_version": 3, "unresolved": [{
            "post_id": 1, "post_title": "Album", "diagnostics": [{
                "code": "spotify_no_results", "message": "No match.",
            }],
        }]}
        mod.validate_unresolved(ordinary)
        ignored = {"schema_version": 3, "ignored": [{
            "post_id": 1, "post_title": "Album", "diagnostics": [{
                "code": "spotify_catalog_unavailable", "message": "Unavailable.",
            }],
        }]}
        mod.validate_ignored(ignored)

        invalid = []
        value = self.provider_unresolved(); value["schema_version"] = 2; invalid.append(value)
        value = self.provider_unresolved(); value["extra"] = True; invalid.append(value)
        value = self.provider_unresolved(); value["unresolved"][0]["diagnostics"][0]["extra"] = 1; invalid.append(value)
        value = self.provider_unresolved(); value["unresolved"][0]["diagnostics"][0]["details"]["attempts"] = True; invalid.append(value)
        value = self.provider_unresolved(); del value["unresolved"][0]["diagnostics"][0]["details"]["http_status"]; invalid.append(value)
        value = self.provider_unresolved(); value["unresolved"][0]["diagnostics"][0]["details"]["http_status"] = True; invalid.append(value)
        value = self.provider_unresolved(); value["unresolved"][0]["diagnostics"][0]["details"]["provider"] = "lastfm"; invalid.append(value)
        value = self.provider_unresolved(); value["unresolved"][0]["diagnostics"][0]["details"]["retryable"] = False; invalid.append(value)
        value = self.provider_unresolved("spotify", "circuit_open"); value["unresolved"][0]["diagnostics"][0]["details"]["attempts"] = 1; invalid.append(value)
        value = copy.deepcopy(ordinary); value["unresolved"][0]["diagnostics"][0]["details"] = {}; invalid.append(value)
        for artifact in invalid:
            with self.subTest(artifact=artifact), self.assertRaises(ValueError):
                mod.validate_unresolved(artifact)

    def test_v2_accepts_only_shaped_lastfm_recovery_diagnostics(self):
        for code in ("lastfm_lookup_fallback", "lastfm_transliteration_alignment"):
            with self.subTest(code=code):
                row = patch_row()
                row["diagnostics"] = [{"code": code, "message": "Validated recovery."}]
                self.assertEqual(mod.validate_plan(plan(row))["schema_version"], 2)
                row["diagnostics"][0]["route"] = "provider payload"
                with self.assertRaises(ValueError):
                    mod.validate_plan(plan(row))

    def test_v2_write_policies_and_v1_regeneration_error(self):
        mod.validate_plan(plan(write_policy=mod.WRITE_FILL_ONLY))
        mod.validate_plan(plan(write_policy=mod.WRITE_OVERWRITE_MANAGED))
        for invalid in ("overwrite", "", None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "write_policy"):
                mod.validate_plan(plan(write_policy=invalid))
        missing = plan(); del missing["write_policy"]
        with self.assertRaisesRegex(ValueError, "root"):
            mod.validate_plan(missing)

        old = {"schema_version": 1, "generated_at": "2025-01-01T00:00:00Z", "patches": []}
        with self.assertRaisesRegex(ValueError, r"Unsupported plan schema version 1.*current CLI"):
            mod.validate_plan(old)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v1.json"; path.write_text(json.dumps(old))
            args = Namespace(plan=str(path), offset=0, limit=None, out_dir=td)
            with patch.object(mod, "WordPress") as wordpress, self.assertRaisesRegex(
                    ValueError, r"Unsupported plan schema version 1.*current CLI"):
                mod.cmd_apply_plan(args, {})
            wordpress.assert_not_called()

    def test_v2_rejects_obsolete_lastfm_field_before_wordpress_construction(self):
        obsolete = plan()
        obsolete["patches"][0]["write"]["acf"]["lastfm_release_id"] = "legacy"
        with self.assertRaisesRegex(ValueError, "lastfm_release_id"):
            mod.validate_plan(obsolete)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "obsolete.json"
            path.write_text(json.dumps(obsolete))
            args = Namespace(plan=str(path), offset=0, limit=None, out_dir=td)
            with patch.object(mod, "WordPress") as wordpress, self.assertRaisesRegex(
                    ValueError, "lastfm_release_id"):
                mod.cmd_apply_plan(args, {})
            wordpress.assert_not_called()

    def test_strict_validation_unknown_bool_nonfinite_duplicate_and_out_of_slice(self):
        mutations = []
        p = plan(); p["extra"] = 1; mutations.append(p)
        p = plan(); p["patches"][0]["post_id"] = True; mutations.append(p)
        p = plan(); p["patches"][0]["matches"]["spotify"]["score"] = math.nan; mutations.append(p)
        mutations.append(plan(patch_row(1), patch_row(1)))
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ValueError): mod.validate_plan(value)
        with self.assertRaises(ValueError):
            mod.validate_plan(plan(patch_row(1), {"bad": "outside slice"}))
        for offset, limit in ((-1, None), (0, -1)):
            with self.assertRaises(ValueError): mod.slice_items([], offset, limit)

    def test_lastfm_urls_and_mbids_require_valid_syntax(self):
        valid_mbid = "123e4567-e89b-12d3-a456-426614174000"
        row = patch_row()
        row["write"] = {"acf": {"lastfm_url": "https://last.fm/music/A/B", "mbid": valid_mbid}}
        row["matches"]["lastfm"].update({"url": "http://last.fm/music/A/B", "mbid": valid_mbid})
        mod.validate_plan(plan(row))

        for location, key, value in (("write", "lastfm_url", "ftp://last.fm/A"),
                                     ("write", "mbid", "not-an-mbid"),
                                     ("matches", "url", "https:///missing-host"),
                                     ("matches", "mbid", "not-an-mbid")):
            row = patch_row()
            if location == "write":
                row["write"] = {"acf": {key: value}}
            else:
                row["matches"]["lastfm"][key] = value
            with self.subTest(location=location, key=key), self.assertRaises(ValueError):
                mod.validate_plan(plan(row))

    def test_replay_dates_require_calendar_valid_dmy_values(self):
        for field in ("music_release_date", "music_listened_at"):
            for value in ("29/02/2024", "31/12/2026"):
                row = patch_row()
                row["write"] = {"acf": {field: value}}
                with self.subTest(field=field, value=value):
                    mod.validate_plan(plan(row))
            for value in ("2026-12-31", "31/02/2026", "1/12/2026", "not-a-date"):
                row = patch_row()
                row["write"] = {"acf": {field: value}}
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                        ValueError, r"valid dd/mm/YYYY date"):
                    mod.validate_plan(plan(row))

        write = {"acf": {
            "music_release_date": "29/02/2024",
            "music_listened_at": "31/12/2026",
        }}
        self.assertEqual(mod.materialize_body(write, {}), {"acf": {
            "music_release_date": "20240229",
            "music_listened_at": "20261231",
        }})

    def test_tracks_and_materialization_preserve_absent_replacements(self):
        row = patch_row(); row["write"] = {"acf": {"music_tracks": [{
            "disc_number": 1, "track_number": 1, "title": "Song", "duration_ms": 1,
            "spotify_id": "t", "highlight": False, "explicit": False}]}}
        mod.validate_plan(plan(row))
        self.assertEqual(mod.materialize_body(row["write"], {}), row["write"])
        self.assertNotIn("categories", mod.materialize_body(row["write"], {}))
        row["write"]["acf"]["music_tracks"] = []
        with self.assertRaisesRegex(ValueError, "music_tracks.*nonempty"):
            mod.validate_plan(plan(row))

    def test_resolution_once_before_updates_and_mixed_result(self):
        wp = FakeWP()
        succeeded, failed = mod.apply_patches(wp, [patch_row(1), patch_row(2)])
        self.assertEqual(wp.created, [("artist", "Artist")])
        self.assertEqual(succeeded, [1]); self.assertEqual(failed[0]["post_id"], 2)
        self.assertEqual(wp.updated[0][1]["artist"], [10])

    def test_term_resolution_failure_prevents_all_updates(self):
        wp = FakeWP(creation_succeeds=False)
        with self.assertRaises(RuntimeError): mod.apply_patches(wp, [patch_row()])
        self.assertEqual(wp.updated, [])

    def test_apply_plan_isolated_and_atomic_result(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "planned.json"
            path.write_text(json.dumps(plan(write_policy=mod.WRITE_OVERWRITE_MANAGED)))
            wp = FakeWP()
            with patch.object(mod, "WordPress", return_value=wp), \
                 patch.object(mod, "Spotify", side_effect=AssertionError), \
                 patch.object(mod, "LastFM", side_effect=AssertionError), \
                 patch.object(mod, "is_field_present", side_effect=AssertionError):
                rc = mod.cmd_apply_plan(Namespace(plan=str(path), offset=0, limit=None, out_dir=td), {
                    "WORDPRESS_BASE_URL": "x", "WORDPRESS_USERNAME": "u", "WORDPRESS_APP_PASSWORD": "p"})
            self.assertEqual(rc, 0)
            applied = json.loads((Path(td) / "applied.json").read_text())
            self.assertEqual(applied["succeeded"], [1])
            self.assertFalse((Path(td) / "applied.json.tmp").exists())

    def test_command_env_requirements_and_loader_does_not_warn(self):
        with self.assertRaisesRegex(SystemExit, "SPOTIFY_CLIENT_ID"):
            mod.require_env({}, "SPOTIFY_CLIENT_ID")
        with patch.object(mod.log, "warning") as warning:
            mod.load_env(None)
        warning.assert_not_called()

    def test_taxonomy_pagination_headers_fallback_and_errors(self):
        wp = object.__new__(mod.WordPress); wp._url = lambda *a, **k: "url"
        wp._req_get = lambda url: ([{"name": "A", "id": 1}], {"X-WP-TotalPages": "1"})
        self.assertEqual(wp.list_tax_terms("artist"), {"A": 1})
        wp._req_get = lambda url: ([], {})
        self.assertEqual(wp.list_tax_terms("artist"), {})

        def http_error(status, body):
            return urllib.error.HTTPError("u", status, "bad", Message(), io.BytesIO(body))

        calls = 0
        def recognized_later(url):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ([{"name": f"A{i}", "id": i + 1} for i in range(100)], {})
            raise http_error(400, b'{"code":"rest_post_invalid_page_number"}')
        wp._req_get = recognized_later
        self.assertEqual(len(wp.list_tax_terms("artist")), 100)

        for responses in (
                [http_error(400, b'{"code":"rest_post_invalid_page_number"}')],
                [([{"name": f"A{i}", "id": i + 1} for i in range(100)], {}),
                 http_error(400, b'{"code":"rest_invalid_param"}')],
                [([{"name": f"A{i}", "id": i + 1} for i in range(100)], {}),
                 http_error(400, b'not-json')],
                [http_error(500, b'{}')]):
            queue = iter(responses)
            def fail(url, queue=queue):
                value = next(queue)
                if isinstance(value, BaseException): raise value
                return value
            wp._req_get = fail
            with self.subTest(responses=responses), self.assertRaises(urllib.error.HTTPError):
                wp.list_tax_terms("artist")

    def test_run_commands_write_artifacts_and_share_replay_materialization(self):
        class CommandWP(FakeWP):
            def list_tags(self, target): return target
            def list_posts(self, per_page=100): return iter([{"id": 1}])

        env = {"WORDPRESS_BASE_URL": "x", "WORDPRESS_USERNAME": "u",
               "WORDPRESS_APP_PASSWORD": "p", "SPOTIFY_CLIENT_ID": "s",
               "SPOTIFY_CLIENT_SECRET": "ss", "LASTFM_API_KEY": "l"}
        with tempfile.TemporaryDirectory() as td:
            args = Namespace(offset=0, limit=None, out_dir=td, apply=False, dry_run=True)
            dry_wp = CommandWP()
            with patch.object(mod, "WordPress", return_value=dry_wp), \
                 patch.object(mod, "Spotify"), patch.object(mod, "LastFM"), \
                 patch.object(mod, "enrich", return_value=patch_row()):
                self.assertEqual(mod.cmd_run(args, env), 0)
            self.assertEqual(dry_wp.created, []); self.assertEqual(dry_wp.updated, [])
            saved = json.loads((Path(td) / "planned.json").read_text())
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["write_policy"], mod.WRITE_FILL_ONLY)
            self.assertTrue((Path(td) / "unresolved.json").exists())
            self.assertTrue((Path(td) / "ignored.json").exists())

            run_wp, replay_wp = CommandWP(), CommandWP()
            args.apply, args.dry_run = True, False
            original_materialize = mod.materialize_body
            with patch.object(mod, "WordPress", return_value=run_wp), \
                 patch.object(mod, "Spotify"), patch.object(mod, "LastFM"), \
                 patch.object(mod, "enrich", return_value=patch_row()), \
                 patch.object(mod, "materialize_body", wraps=original_materialize) as materialize:
                self.assertEqual(mod.cmd_run(args, env), 0)
                self.assertEqual(materialize.call_count, 1)
            apply_args = Namespace(plan=str(Path(td) / "planned.json"), offset=0,
                                   limit=None, out_dir=td)
            with patch.object(mod, "WordPress", return_value=replay_wp), \
                 patch.object(mod, "materialize_body", wraps=original_materialize) as materialize:
                self.assertEqual(mod.cmd_apply_plan(apply_args, env), 0)
                self.assertEqual(materialize.call_count, 1)
            self.assertEqual(run_wp.updated, replay_wp.updated)

    def test_run_continues_after_malformed_spotify_track_and_validates_plan(self):
        class CommandWP(FakeWP):
            def list_tags(self, target): return target
            def list_posts(self, per_page=100): return iter([{"id": 1}, {"id": 2}])

        album = {"id": "album-id", "name": "Broken", "artists": [{"name": "Artist"}],
                 "total_tracks": 1}
        track = {"id": "track-id", "name": "Song", "duration_ms": 1000,
                 "disc_number": 1, "track_number": 1}
        try:
            mod.validate_spotify_album_tracks(album, [track])
        except mod.SpotifyProviderError as exc:
            malformed = mod._provider_unresolved(
                {"id": 1, "title": {"rendered": "Broken"}},
                "spotify_provider_error", exc, "track.list")
        else:
            self.fail("missing explicit value was accepted")
        env = {"WORDPRESS_BASE_URL": "x", "WORDPRESS_USERNAME": "u",
               "WORDPRESS_APP_PASSWORD": "p", "SPOTIFY_CLIENT_ID": "s",
               "SPOTIFY_CLIENT_SECRET": "ss", "LASTFM_API_KEY": "l"}
        with tempfile.TemporaryDirectory() as td:
            args = Namespace(offset=0, limit=None, out_dir=td, apply=False,
                             dry_run=True, overwrite_managed=False)
            with patch.object(mod, "WordPress", return_value=CommandWP()), \
                 patch.object(mod, "Spotify"), patch.object(mod, "LastFM"), \
                 patch.object(mod, "enrich", side_effect=[malformed, patch_row(2)]):
                self.assertEqual(mod.cmd_run(args, env), 0)
            planned = json.loads((Path(td) / "planned.json").read_text())
            unresolved = json.loads((Path(td) / "unresolved.json").read_text())
            mod.validate_plan(planned)
            mod.validate_unresolved(unresolved)
            self.assertEqual([row["post_id"] for row in planned["patches"]], [2])
            self.assertEqual(unresolved["unresolved"][0]["diagnostics"][0]["details"], {
                "provider": "spotify", "operation": "track.list",
                "failure_kind": "malformed_response", "retryable": False,
                "attempts": 1, "circuit_state": "closed",
            })

    def test_outage_writes_artifacts_exits_nonzero_and_blocks_deprecated_apply(self):
        class CommandWP(FakeWP):
            def list_tags(self, target): return target
            def list_posts(self, per_page=100):
                return iter([{"id": i, "title": {"rendered": f"Album {i}"}} for i in range(1, 5)])

        class Client:
            def __init__(self, provider): self._circuit = mod.ProviderCircuit(provider)

        env = {"WORDPRESS_BASE_URL": "x", "WORDPRESS_USERNAME": "u",
               "WORDPRESS_APP_PASSWORD": "p", "SPOTIFY_CLIENT_ID": "s",
               "SPOTIFY_CLIENT_SECRET": "ss", "LASTFM_API_KEY": "l"}
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as td:
                wp, spotify, lastfm = CommandWP(), Client("spotify"), Client("lastfm")
                calls = 0
                def failed(post, *args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        spotify._circuit.is_open = True
                    error = mod.SpotifyProviderError(
                        "Spotify album.search unavailable.", operation="album.search",
                        failure_kind="circuit_open" if calls == 4 else "http_status",
                        retryable=True, attempts=0 if calls == 4 else 3,
                        http_status=None if calls == 4 else 503,
                        circuit_state="open" if calls >= 3 else "closed")
                    return mod._provider_unresolved(post, "spotify_provider_error", error,
                                                    "album.search")
                args = Namespace(offset=0, limit=None, out_dir=td, apply=apply,
                                 dry_run=not apply, overwrite_managed=False)
                with patch.object(mod, "WordPress", return_value=wp), \
                     patch.object(mod, "Spotify", return_value=spotify), \
                     patch.object(mod, "LastFM", return_value=lastfm), \
                     patch.object(mod, "enrich", side_effect=failed), \
                     patch.object(mod, "apply_patches") as apply_patches:
                    self.assertEqual(mod.cmd_run(args, env), 1)
                apply_patches.assert_not_called()
                self.assertEqual(json.loads((Path(td) / "planned.json").read_text())["schema_version"], 2)
                unresolved = json.loads((Path(td) / "unresolved.json").read_text())
                self.assertEqual(unresolved["schema_version"], 3)
                self.assertEqual(len(unresolved["unresolved"]), 4)
                self.assertEqual(unresolved["unresolved"][-1]["diagnostics"][0]["details"]["attempts"], 0)
                self.assertEqual(wp.updated, [])

    def test_cmd_run_propagates_policy_and_loads_release_types_once(self):
        class CommandWP(FakeWP):
            def __init__(self): super().__init__(); self.release_type_reads = 0
            def list_tags(self, target): return target
            def list_posts(self, per_page=100): return iter([{"id": 1}, {"id": 2}])
            def list_tax_terms(self, tax):
                self.release_type_reads += tax == "release_type"
                return super().list_tax_terms(tax)

        env = {"WORDPRESS_BASE_URL": "x", "WORDPRESS_USERNAME": "u",
               "WORDPRESS_APP_PASSWORD": "p", "SPOTIFY_CLIENT_ID": "s",
               "SPOTIFY_CLIENT_SECRET": "ss", "LASTFM_API_KEY": "l"}
        with tempfile.TemporaryDirectory() as td:
            args = Namespace(offset=0, limit=None, out_dir=td, apply=False,
                             dry_run=True, overwrite_managed=True)
            wp = CommandWP()
            with patch.object(mod, "WordPress", return_value=wp), \
                 patch.object(mod, "Spotify"), patch.object(mod, "LastFM"), \
                 patch.object(mod, "enrich", return_value=None) as enrich, \
                 self.assertLogs("post_to_album", level="INFO") as logs:
                self.assertEqual(mod.cmd_run(args, env), 0)
            self.assertEqual(wp.release_type_reads, 1)
            self.assertEqual(enrich.call_count, 2)
            self.assertTrue(all(call.args[-1] == mod.WRITE_OVERWRITE_MANAGED
                                for call in enrich.call_args_list))
            self.assertTrue(all(call.kwargs["release_type_terms"] == {"Album": 30}
                                for call in enrich.call_args_list))
            saved = json.loads((Path(td) / "planned.json").read_text())
            self.assertEqual(saved["write_policy"], mod.WRITE_OVERWRITE_MANAGED)
            output = "\n".join(logs.output)
            self.assertIn("Planning write policy: overwrite_managed", output)
            self.assertIn("reviewed plans may replace existing managed data", output)

    def test_atomic_writer_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.json"; path.write_text("old")
            mod.write_json_atomic(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__": unittest.main()
