import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import post_to_album as mod


class WordPressTagCacheTests(unittest.TestCase):
    def setUp(self):
        self.wp = mod.WordPress("https://example.test", "user", "password")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "tag-cache.json"

    @staticmethod
    def headers(total, pages=1, mixed_case=False):
        names = ("x-Wp-ToTaL", "X-wP-tOtAlPaGeS") if mixed_case else (
            "X-WP-Total", "X-WP-TotalPages")
        return {names[0]: str(total), names[1]: str(pages)}

    @classmethod
    def collection_response(cls, tags, url, *, mixed_case=False):
        query = parse_qs(urlparse(url).query)
        size, page = int(query["per_page"][0]), int(query["page"][0])
        reverse = query.get("order") == ["desc"]
        rows = [{"id": tag_id, "name": tags[tag_id]} for tag_id in sorted(tags, reverse=reverse)]
        pages = max(1, (len(rows) + size - 1) // size)
        return rows[(page - 1) * size:page * size], cls.headers(len(rows), pages, mixed_case)

    def write_cache(self, *, age=0, fetched_at=None, api="https://example.test/wp-json/wp/v2",
                    tags=None, total=None, highest_id=None):
        tags = tags or {1: "Artist"}
        self.cache.write_text(json.dumps({
            "api": api,
            "fetched_at": time.time() - age if fetched_at is None else fetched_at,
            "total": len(tags) if total is None else total,
            "highest_id": max(tags, default=0) if highest_id is None else highest_id,
            "tags": tags,
        }))

    def test_cold_scan_uses_collection_rows_and_writes_complete_cache(self):
        tags = {1: "A", 2: "B", 3: "C"}
        calls = []

        def request(url):
            calls.append(url)
            return self.collection_response(tags, url)

        self.wp._req_get = request
        self.assertEqual(self.wp.list_tags({}, self.cache), tags)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("/tags/" not in url for url in calls))
        saved = json.loads(self.cache.read_text())
        self.assertEqual((saved["total"], saved["highest_id"]), (3, 3))
        self.assertEqual(saved["tags"], {"1": "A", "2": "B", "3": "C"})

    def test_fresh_matching_cache_needs_one_probe(self):
        tags = {1: "A", 4: "D"}
        self.write_cache(tags=tags)
        calls = []
        self.wp._req_get = lambda url: calls.append(url) or self.collection_response(tags, url)

        self.assertEqual(self.wp.list_tags({}, self.cache), tags)
        self.assertEqual(len(calls), 1)
        query = parse_qs(urlparse(calls[0]).query)
        self.assertEqual((query["per_page"], query["orderby"], query["order"]),
                         (["1"], ["id"], ["desc"]))

    def test_changed_count_or_highest_id_invalidates_fresh_cache(self):
        for tags in ({1: "A", 2: "B"}, {3: "C"}):
            with self.subTest(tags=tags):
                self.write_cache(tags={1: "A"})
                calls = []
                self.wp._req_get = lambda url: calls.append(url) or self.collection_response(tags, url)
                self.assertEqual(self.wp.list_tags({}, self.cache), tags)
                self.assertEqual(len(calls), 4)

    def test_older_than_24_hours_refreshes_names(self):
        self.write_cache(age=mod.TAG_CACHE_MAX_AGE + 1, tags={1: "Old"})
        tags = {1: "Renamed"}
        calls = []
        self.wp._req_get = lambda url: calls.append(url) or self.collection_response(tags, url)

        self.assertEqual(self.wp.list_tags({}, self.cache), tags)
        self.assertEqual(len(calls), 3)

    def test_headers_are_case_insensitive(self):
        tags = {1: "A", 2: "B"}
        self.wp._req_get = lambda url: self.collection_response(tags, url, mixed_case=True)
        self.assertEqual(self.wp.list_tags({}, self.cache), tags)

    def test_future_and_non_finite_timestamps_are_rejected(self):
        tags = {2: "Fresh"}
        for fetched_at in (time.time() + 60, float("inf"), float("-inf")):
            with self.subTest(fetched_at=fetched_at):
                self.write_cache(fetched_at=fetched_at, tags={1: "Invalid"})
                calls = []
                self.wp._req_get = lambda url: calls.append(url) or self.collection_response(tags, url)
                self.assertEqual(self.wp.list_tags({}, self.cache), tags)
                self.assertEqual(len(calls), 3)

    def test_scan_retries_after_delete_and_add_shift_pagination(self):
        original = {tag_id: str(tag_id) for tag_id in range(1, 102)}
        changed = {tag_id: str(tag_id) for tag_id in range(1, 103) if tag_id != 50}
        scans = 0

        def request(url):
            nonlocal scans
            query = parse_qs(urlparse(url).query)
            if query["per_page"] == ["100"]:
                scans += 1
                tags = original if scans == 1 else changed
                if scans == 1 and query["page"] == ["2"]:
                    tags = changed
                return self.collection_response(tags, url)
            return self.collection_response(original if scans < 1 else changed, url)

        self.wp._req_get = request
        self.assertEqual(self.wp.list_tags({}, self.cache), changed)
        self.assertEqual(scans, 4)
        self.assertEqual(json.loads(self.cache.read_text())["tags"],
                         {str(tag_id): name for tag_id, name in changed.items()})

    def test_unstable_scan_falls_back_without_overwriting_valid_cache(self):
        cached = {1: "Cached"}
        self.write_cache(age=mod.TAG_CACHE_MAX_AGE + 1, tags=cached)
        original = self.cache.read_text()
        highest_id = 2

        def request(url):
            nonlocal highest_id
            query = parse_qs(urlparse(url).query)
            if query["per_page"] == ["1"]:
                highest_id += 1
                return ([{"id": highest_id, "name": "Changing"}], self.headers(1))
            return ([{"id": highest_id, "name": "Changing"}], self.headers(1))

        self.wp._req_get = request
        with self.assertLogs("post_to_album", level="WARNING") as logs:
            self.assertEqual(self.wp.list_tags({}, self.cache), cached)
        self.assertIn("using cached tags", "\n".join(logs.output))
        self.assertEqual(self.cache.read_text(), original)

    def test_malformed_and_wrong_site_caches_are_not_reused(self):
        tags = {2: "Fresh"}
        for contents in ("not-json", json.dumps({"api": "https://other.test", "tags": {1: "A"}})):
            with self.subTest(contents=contents):
                self.cache.write_text(contents)
                calls = []
                self.wp._req_get = lambda url: calls.append(url) or self.collection_response(tags, url)
                self.assertEqual(self.wp.list_tags({}, self.cache), tags)
                self.assertEqual(len(calls), 3)

    def test_valid_cache_is_safe_fallback_but_invalid_cache_is_not(self):
        self.write_cache(age=mod.TAG_CACHE_MAX_AGE + 1, tags={1: "Cached"})
        self.wp._req_get = lambda url: (_ for _ in ()).throw(urllib.error.URLError("down"))
        with self.assertLogs("post_to_album", level="WARNING") as logs:
            self.assertEqual(self.wp.list_tags({}, self.cache), {1: "Cached"})
        self.assertIn("using cached tags", "\n".join(logs.output))

        self.cache.write_text("broken")
        with self.assertRaises(urllib.error.URLError):
            self.wp.list_tags({}, self.cache)


if __name__ == "__main__":
    unittest.main()
