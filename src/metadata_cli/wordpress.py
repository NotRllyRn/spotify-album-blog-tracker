"""Synchronous WordPress REST adapter for the manual metadata CLI."""

import base64
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from album_metadata.common import safe_error as _safe_error, write_json_atomic
from album_metadata.schema import TAG_CACHE_MAX_AGE, TAG_CACHE_PATH

log = logging.getLogger("post_to_album")

class WordPress:
    def __init__(self, base: str, user: str, app_pw: str):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("WORDPRESS_BASE_URL must be an http(s) URL.")
        self.api       = f"{self.base}/wp-json/wp/v2"
        self._auth     = "Basic " + base64.b64encode(f"{user}:{app_pw}".encode()).decode()
        self._hdr_json = {"Authorization": self._auth, "Accept": "application/json",
                          "Content-Type": "application/json"}
        self._hdr_get  = {"Authorization": self._auth, "Accept": "application/json"}

    def _url(self, path: str, **qs) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.api}{path}" + (sep + urllib.parse.urlencode(qs) if qs else "")

    def _req_get(self, url: str) -> tuple[Any, dict]:
        req = urllib.request.Request(url, headers=self._hdr_get)
        with urllib.request.urlopen(req, timeout=30) as r:
            try:
                return json.loads(r.read()), dict(r.headers)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("WordPress returned malformed JSON.") from exc

    def _req_post(self, url: str, body: dict) -> Any:
        for attempt in (0, 1):
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers=self._hdr_json, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    if attempt == 0:
                        retry = int(exc.headers.get("Retry-After", "2"))
                        log.warning("WP 429, sleeping %ds", retry)
                        time.sleep(retry)
                        continue
                raise

    # ---- reads ----

    def list_posts(self, per_page: int = 100) -> Iterable[dict]:
        page = 1
        while True:
            url = self._url("/posts", per_page=per_page, page=page, context="edit")
            chunk, hdrs = self._req_get(url)
            if not chunk:
                return
            for p in chunk:
                yield p
            total_pages_hdr = hdrs.get("X-WP-TotalPages", str(page))
            try:
                total_pages = int(total_pages_hdr)
            except (TypeError, ValueError):
                total_pages = page
            if page >= total_pages:
                return
            page += 1

    def total_posts(self) -> int:
        url = self._url("/posts", per_page=1, context="edit")
        req = urllib.request.Request(url, headers=self._hdr_get)
        with urllib.request.urlopen(req, timeout=30) as r:
            try:
                return int(r.headers.get("X-WP-Total", "0"))
            except (TypeError, ValueError):
                return 0

    def list_tax_terms(self, tax: str) -> dict[str, int]:
        """Return every term; only a later out-of-range 400 ends pagination."""
        found: dict[str, int] = {}
        page = 1
        while True:
            try:
                rows, headers = self._req_get(
                    self._url(f"/{tax}", per_page=100, page=page))
            except urllib.error.HTTPError as exc:
                # WordPress uses this specific REST error to end headerless pagination.
                if exc.code == 400:
                    if page > 1:
                        try:
                            error = json.loads(exc.read().decode("utf-8"))
                        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                            raise exc
                        if isinstance(error, dict):
                            if error.get("code") == "rest_post_invalid_page_number":
                                return found
                raise
            if not isinstance(rows, list):
                raise RuntimeError(f"WordPress {tax} response was not a list")
            for row in rows:
                found[row["name"]] = row["id"]
            raw_pages = headers.get("X-WP-TotalPages") or headers.get("x-wp-totalpages")
            if raw_pages is not None:
                try:
                    if page >= int(raw_pages):
                        return found
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Invalid X-WP-TotalPages header") from exc
            elif len(rows) < 100:
                return found
            page += 1

    @staticmethod
    def _header(headers: dict, name: str) -> Any:
        return next((value for key, value in headers.items() if key.lower() == name.lower()), None)

    def _read_tag_cache(self, path: Path) -> tuple[dict, dict[int, str]] | None:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            raw_tags = cached["tags"]
            tags = {int(tag_id): name for tag_id, name in raw_tags.items()}
            now = time.time()
            valid = (cached["api"] == self.api and isinstance(cached["fetched_at"], (int, float))
                     and not isinstance(cached["fetched_at"], bool)
                     and math.isfinite(cached["fetched_at"]) and cached["fetched_at"] <= now
                     and isinstance(cached["total"], int) and not isinstance(cached["total"], bool)
                     and isinstance(cached["highest_id"], int)
                     and not isinstance(cached["highest_id"], bool)
                     and isinstance(raw_tags, dict) and all(
                         str(tag_id) == str(int(tag_id)) and int(tag_id) > 0 and isinstance(name, str)
                         for tag_id, name in raw_tags.items())
                     and cached["total"] == len(tags)
                     and cached["highest_id"] == max(tags, default=0))
            return (cached, tags) if valid else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _tag_probe(self) -> tuple[int, int]:
        rows, headers = self._req_get(
            self._url("/tags", per_page=1, page=1, orderby="id", order="desc"))
        if not isinstance(rows, list):
            raise RuntimeError("WordPress tags response was not a list")
        try:
            total = int(self._header(headers, "X-WP-Total"))
            highest_id = int(rows[0]["id"]) if rows else 0
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise RuntimeError("Invalid WordPress tag metadata") from exc
        return total, highest_id

    def _scan_tags(self) -> tuple[dict[int, str], int, int]:
        tags: dict[int, str] = {}
        page = 1
        while True:
            rows, headers = self._req_get(
                self._url("/tags", per_page=100, page=page, orderby="id", order="asc"))
            if not isinstance(rows, list):
                raise RuntimeError("WordPress tags response was not a list")
            try:
                total = int(self._header(headers, "X-WP-Total"))
                total_pages = int(self._header(headers, "X-WP-TotalPages"))
                tags.update((int(row["id"]), row["name"]) for row in rows)
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError("Invalid WordPress tag response") from exc
            if page >= total_pages:
                break
            page += 1
        if len(tags) != total:
            raise RuntimeError("Incomplete WordPress tag scan")
        return tags, total, max(tags, default=0)

    def _stable_tag_scan(self) -> tuple[dict[int, str], int, int]:
        for _ in range(2):
            before = self._tag_probe()
            result = self._scan_tags()
            after = self._tag_probe()
            if before == result[1:] == after:
                return result
        raise RuntimeError("WordPress tags changed during scan")

    def list_tags(self, name_to_id: dict[int, str], cache_path: str | Path | None = None) -> dict[int, str]:
        path = Path(cache_path) if cache_path is not None else TAG_CACHE_PATH
        cache = self._read_tag_cache(path)
        cached_meta, cached_tags = cache or ({}, {})
        age = time.time() - cached_meta["fetched_at"] if cache else -1
        fresh = bool(cache and 0 <= age < TAG_CACHE_MAX_AGE)
        try:
            if fresh and self._tag_probe() == (cached_meta["total"], cached_meta["highest_id"]):
                tags = cached_tags
            else:
                tags, total, highest_id = self._stable_tag_scan()
                try:
                    write_json_atomic(path, {"api": self.api, "fetched_at": time.time(),
                                             "total": total, "highest_id": highest_id,
                                             "tags": tags})
                except OSError as exc:
                    log.warning("Could not write WordPress tag cache: %s", _safe_error(exc))
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            if not cache:
                raise
            log.warning("WordPress tag refresh failed; using cached tags: %s", _safe_error(exc))
            tags = cached_tags
        name_to_id.clear()
        name_to_id.update(tags)
        return name_to_id

    def create_term(self, tax: str, name: str) -> int | None:
        try:
            t = self._req_post(self._url(f"/{tax}"), {"name": name})
            return t["id"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400:
                if "term_exists" not in body:
                    raise
                # re-fetch by slug
                slug = urllib.parse.quote(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))
                try:
                    rows, _ = self._req_get(self._url(f"/{tax}", slug=slug))
                    if rows:
                        return rows[0]["id"]
                except urllib.error.HTTPError as _slug_err:
                    log.debug("slug re-lookup failed: %s", _slug_err)
                    pass
                # fallback: list all and find by name
                all_rows, _ = self._req_get(self._url(f"/{tax}", per_page=100))
                for r in all_rows:
                    if r["name"] == name:
                        return r["id"]
            log.warning("create_term %s/%s failed: HTTP %d %s",
                        tax, name, exc.code, body[:200])
            return None

    def update_post(self, pid: int, body: dict) -> dict:
        url = self._url(f"/posts/{pid}")
        return self._req_post(url, body)
