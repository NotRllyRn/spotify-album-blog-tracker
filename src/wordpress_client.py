"""
Async WordPress REST API client.
"""

import httpx
import logging
import hashlib
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path
import base64

from config import Config

logger = logging.getLogger(__name__)


def _payload_type_summary(data: Dict[str, Any]) -> str:
    acf = data.get("acf")
    if isinstance(acf, dict):
        return ", ".join(f"{field}={type(value).__name__}" for field, value in sorted(acf.items()))
    return ", ".join(f"{field}={type(value).__name__}" for field, value in sorted(data.items()))


def _response_error_summary(response: httpx.Response) -> Dict[str, Any]:
    """Return actionable error structure without logging server-supplied messages."""
    try:
        body = response.json()
    except ValueError:
        return {"code": "non_json_response", "status": response.status_code}
    if not isinstance(body, dict):
        return {"code": "unexpected_response", "status": response.status_code}
    data = body.get("data")
    params = data.get("params") if isinstance(data, dict) else None
    details = data.get("details") if isinstance(data, dict) else None
    field_paths = []
    if isinstance(details, dict):
        field_paths = [
            detail.get("data", {}).get("param")
            for detail in details.values()
            if isinstance(detail, dict) and isinstance(detail.get("data"), dict)
        ]
    return {
        "code": body.get("code"),
        "params": sorted(params) if isinstance(params, dict) else [],
        "field_paths": [path for path in field_paths if path],
    }


@dataclass
class WordPressPostsResult:
    posts: List[Dict[str, Any]]
    cache_unchanged: bool
    message: str
    x_wp_total: Optional[str]
    first_page_hash: str


class WordPressClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.wordpress_url.rstrip("/")
        self.api_url = f"{self.base_url}/wp-json/wp/v2"
        self.username = config.wordpress_username
        self.app_password = config.wordpress_app_password

        # Basic auth header
        auth_string = f"{self.username}:{self.app_password}"
        auth_b64 = base64.b64encode(auth_string.encode()).decode()
        self.auth_header = f"Basic {auth_b64}"

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0
        )
        self._cached_tags: Optional[List[Dict[str, Any]]] = None
        self._cached_tags_x_wp_total: Optional[str] = None
        self._cached_tags_first_page_hash: Optional[str] = None

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    async def get_posts(
        self,
        validate_first_page: bool = False,
        previous_x_wp_total: Optional[str] = None,
        previous_first_page_hash: Optional[str] = None,
        **params
    ) -> WordPressPostsResult:
        """Get posts with pagination, optionally short-circuiting on page-1 metadata."""
        url = f"{self.api_url}/posts"
        all_posts = []

        request_params = {**params, "per_page": params.get("per_page", 100), "page": 1}
        response = await self.client.get(url, params=request_params)
        response.raise_for_status()

        first_page_data = response.json()
        x_wp_total = response.headers.get("X-WP-Total")
        first_page_hash = hashlib.sha256(response.content).hexdigest()

        if self._first_page_cache_matches(
            validate_first_page,
            x_wp_total,
            first_page_hash,
            previous_x_wp_total,
            previous_first_page_hash,
        ):
            return WordPressPostsResult(
                posts=[],
                cache_unchanged=True,
                message=(
                    "WordPress post cache is current; X-WP-Total and "
                    "first-page hash both matched."
                ),
                x_wp_total=x_wp_total,
                first_page_hash=first_page_hash,
            )

        all_posts.extend(first_page_data)

        try:
            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
        except ValueError:
            total_pages = 1

        for page in range(2, total_pages + 1):
            request_params = {**params, "per_page": params.get("per_page", 100), "page": page}
            response = await self.client.get(url, params=request_params)
            response.raise_for_status()
            data = response.json()
            all_posts.extend(data)

        return WordPressPostsResult(
            posts=all_posts,
            cache_unchanged=False,
            message=f"Fetched {len(all_posts)} WordPress posts.",
            x_wp_total=x_wp_total,
            first_page_hash=first_page_hash,
        )

    def _first_page_cache_matches(
        self,
        validate_first_page: bool,
        x_wp_total: Optional[str],
        first_page_hash: str,
        previous_x_wp_total: Optional[str],
        previous_first_page_hash: Optional[str],
    ) -> bool:
        """Return whether page-1 metadata proves the cached post list is current."""
        if not validate_first_page:
            return False

        if not all([
            x_wp_total,
            first_page_hash,
            previous_x_wp_total,
            previous_first_page_hash,
        ]):
            return False

        if x_wp_total is None or not x_wp_total.isdigit():
            return False

        return (
            x_wp_total == previous_x_wp_total
            and first_page_hash == previous_first_page_hash
        )

    async def create_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new post."""
        url = f"{self.api_url}/posts"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def update_post(self, post_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update a post."""
        url = f"{self.api_url}/posts/{post_id}"
        response = await self.client.post(url, json=data)
        if not response.is_success:
            logger.error(
                "WordPress post %s update failed: %s; payload types: %s",
                post_id,
                _response_error_summary(response),
                _payload_type_summary(data),
            )
        response.raise_for_status()
        return response.json()

    async def get_post(self, post_id: int, **params) -> Dict[str, Any]:
        """Fetch one WordPress post."""
        response = await self.client.get(f"{self.api_url}/posts/{post_id}", params=params)
        response.raise_for_status()
        return response.json()

    async def get_post_acf(self, post_id: int) -> dict:
        """Fetch the live ``acf`` block for a post so the editor can read current SCF values."""
        url = f"{self.api_url}/posts/{post_id}"
        response = await self.client.get(url, params={"context": "edit"})
        response.raise_for_status()
        return (response.json().get("acf") or {})

    async def get_post_content_raw(self, post_id: int) -> str:
        """Fetch the live WP ``content.raw`` for a post so the editor can pre-fill the body modal."""
        url = f"{self.api_url}/posts/{post_id}"
        response = await self.client.get(url, params={"context": "edit", "_fields": "content"})
        response.raise_for_status()
        content = (response.json().get("content") or {})
        return content.get("raw") or ""

    async def delete_post(self, post_id: int, force: bool = False) -> Dict[str, Any]:
        """Delete a post (move to trash if force=False)."""
        url = f"{self.api_url}/posts/{post_id}"
        params = {"force": "true"} if force else {}
        response = await self.client.delete(url, params=params)
        response.raise_for_status()
        return response.json()

    async def get_categories(self) -> List[Dict[str, Any]]:
        """Get all categories."""
        url = f"{self.api_url}/categories?per_page=100"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def create_category(self, name: str) -> Dict[str, Any]:
        """Create a category."""
        url = f"{self.api_url}/categories"
        response = await self.client.post(url, json={"name": name})
        response.raise_for_status()
        return response.json()

    async def get_tags(self) -> List[Dict[str, Any]]:
        """Get all tags, reusing the in-memory cache when page-1 metadata matches."""
        per_page = 100
        url = f"{self.api_url}/tags"

        response = await self.client.get(
            url,
            params={"per_page": per_page, "page": 1}
        )
        response.raise_for_status()
        first_page_tags = response.json()
        if not isinstance(first_page_tags, list):
            raise ValueError("Unexpected response when fetching tags")

        x_wp_total = response.headers.get("X-WP-Total")
        first_page_hash = hashlib.sha256(response.content).hexdigest()

        if self._tag_cache_matches(x_wp_total, first_page_hash):
            logger.info("Using cached WordPress tags; X-WP-Total and first-page hash matched.")
            return list(self._cached_tags or [])

        tags = list(first_page_tags)
        total_pages = self._parse_total_pages(response)

        if total_pages is not None:
            pages_to_fetch = range(2, total_pages + 1)
            stop_on_short_page = False
        elif len(first_page_tags) < per_page:
            pages_to_fetch = range(0)
            stop_on_short_page = True
        else:
            pages_to_fetch = range(2, 10_000)
            stop_on_short_page = True

        for page in pages_to_fetch:
            response = await self.client.get(
                url,
                params={"per_page": per_page, "page": page}
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError("Unexpected response when fetching tags")

            tags.extend(data)

            if stop_on_short_page and len(data) < per_page:
                break

        self._cache_tags(tags, x_wp_total, first_page_hash)

        return tags

    def _parse_total_pages(self, response: httpx.Response) -> Optional[int]:
        """Parse a WordPress total-pages header, falling back when it is absent or invalid."""
        total_pages = response.headers.get("X-WP-TotalPages")
        if total_pages is None:
            return None

        try:
            return int(total_pages)
        except ValueError:
            return None

    def _tag_cache_matches(self, x_wp_total: Optional[str], first_page_hash: str) -> bool:
        cached_tags = getattr(self, "_cached_tags", None)
        cached_x_wp_total = getattr(self, "_cached_tags_x_wp_total", None)
        cached_first_page_hash = getattr(self, "_cached_tags_first_page_hash", None)

        if cached_tags is None:
            return False

        if not all([x_wp_total, first_page_hash, cached_x_wp_total, cached_first_page_hash]):
            return False

        if x_wp_total is None or not x_wp_total.isdigit():
            return False

        return (
            x_wp_total == cached_x_wp_total
            and first_page_hash == cached_first_page_hash
        )

    def _cache_tags(
        self,
        tags: List[Dict[str, Any]],
        x_wp_total: Optional[str],
        first_page_hash: str,
    ):
        self._cached_tags = list(tags)
        self._cached_tags_x_wp_total = x_wp_total
        self._cached_tags_first_page_hash = first_page_hash

    def _reconcile_cached_tag(self, tag: Dict[str, Any]):
        cached_tags = getattr(self, "_cached_tags", None)
        if cached_tags is None:
            return

        tag_id = tag.get("id")
        for index, cached_tag in enumerate(cached_tags):
            if cached_tag.get("id") == tag_id:
                cached_tags[index] = tag
                break
        else:
            cached_tags.append(tag)

        self._cached_tags_x_wp_total = None
        self._cached_tags_first_page_hash = None

    async def get_tag_by_id(self, tag_id: int) -> Dict[str, Any]:
        """Get a tag by WordPress ID."""
        url = f"{self.api_url}/tags/{tag_id}"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def get_tag_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a tag by exact name."""
        for tag in await self.get_tags():
            if tag.get("name") == name:
                return tag
        return None

    async def create_tag(self, name: str) -> Dict[str, Any]:
        """Create a tag."""
        url = f"{self.api_url}/tags"
        response = await self.client.post(url, json={"name": name})
        try:
            response.raise_for_status()
            tag = response.json()
            self._reconcile_cached_tag(tag)
            return tag
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                try:
                    body = exc.response.json()
                except ValueError:
                    raise

                if body.get("code") in {"term_exists", "existing_term_slug", "term_exists_invalid"}:
                    term_id = body.get("data", {}).get("term_id")
                    if term_id:
                        tag = await self.get_tag_by_id(term_id)
                        self._reconcile_cached_tag(tag)
                        return tag

                    existing_tag = await self.get_tag_by_name(name)
                    if existing_tag:
                        self._reconcile_cached_tag(existing_tag)
                        return existing_tag

            raise

    async def upload_media(self, file_path: Path, alt_text: str = "") -> Dict[str, Any]:
        """Upload media file."""
        url = f"{self.api_url}/media"

        try:
            media_file = open(file_path, "rb")
        except OSError:
            logger.exception("Unable to open media file %s", file_path)
            raise

        with media_file as f:
            files = {"file": (file_path.name, f, "image/jpeg")}
            data = {"alt_text": alt_text} if alt_text else {}

            # Remove content-type header for file upload
            headers = self.client.headers.copy()
            headers.pop("Content-Type", None)

            async with httpx.AsyncClient(headers=headers, timeout=60.0) as upload_client:
                response = await upload_client.post(url, files=files, data=data)
                response.raise_for_status()
                return response.json()

    async def update_media(self, media_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update media metadata."""
        url = f"{self.api_url}/media/{media_id}"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def delete_media(self, media_id: int, force: bool = False) -> Dict[str, Any]:
        """Delete media."""
        url = f"{self.api_url}/media/{media_id}"
        params = {"force": "true"} if force else {}
        response = await self.client.delete(url, params=params)
        response.raise_for_status()
        return response.json()
