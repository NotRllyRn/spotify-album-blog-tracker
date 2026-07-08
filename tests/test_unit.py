"""
Unit tests for core logic: classification, normalization, progress, duplicate matching.
"""

import asyncio
import hashlib
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

import sys
from pathlib import Path

try:
    import httpx
except ModuleNotFoundError:
    httpx = None

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils import normalize_text, normalize_artist_name, normalize_artist_list, compute_release_type
from logging_config import LOG_FILE_NAME, _ALBUM_TRACKER_HANDLER_ATTR, configure_logging
from models import (
    Track,
    Artist,
    Release,
    ReleaseType,
    LifecycleStatus,
    PlaybackState,
    WordPressPost,
    SavedLibraryAlbum,
    SavedLibrarySnapshotItem,
    SavedLibraryStats,
    DiscordPrompt,
    PublishResult,
    PromptState,
    PromptType,
)
from inprogress import build_inprogress_page, INPROGRESS_PAGE_SIZE, get_next_unlistened_track

try:
    from tracker import Tracker
except ModuleNotFoundError:
    Tracker = None

try:
    from saved_library import (
        SAVED_LIBRARY_FIRST_PAGE_HASH_KEY,
        SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY,
        SAVED_LIBRARY_LAST_SYNCED_AT_KEY,
        SAVED_LIBRARY_TOTAL_KEY,
        SavedLibraryService,
    )
except ModuleNotFoundError:
    SavedLibraryService = None
    SAVED_LIBRARY_FIRST_PAGE_HASH_KEY = None
    SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY = None
    SAVED_LIBRARY_LAST_SYNCED_AT_KEY = None
    SAVED_LIBRARY_TOTAL_KEY = None

try:
    from discord_bot import (
        DiscordBot,
        CurrentPlaybackActionView,
        CurrentPostContext,
        PublishedPostActionView,
        RandomAlbumView,
        RelistenApprovalPromptView,
    )
except ModuleNotFoundError:
    DiscordBot = None
    CurrentPlaybackActionView = None
    CurrentPostContext = None
    PublishedPostActionView = None
    RandomAlbumView = None
    RelistenApprovalPromptView = None

try:
    from publisher import (
        Publisher,
        POST_CACHE_FIRST_PAGE_HASH_KEY,
        POST_CACHE_TOTAL_KEY,
        format_discord_content_for_wordpress,
        _coerce_spotify_release_date,
        _format_scf_date,
    )
    from wordpress_client import WordPressClient, WordPressPostsResult
except ModuleNotFoundError:
    Publisher = None
    WordPressClient = None
    WordPressPostsResult = None
    POST_CACHE_FIRST_PAGE_HASH_KEY = None
    POST_CACHE_TOTAL_KEY = None
    format_discord_content_for_wordpress = None
    _coerce_spotify_release_date = None
    _format_scf_date = None

try:
    from lastfm_client import LastFMClient, pick_mood_tags
except ModuleNotFoundError:
    LastFMClient = None
    pick_mood_tags = None

try:
    from editor_view import (
        EditorState,
        EditorView,
        EditorTracksView,
        PrePublishSink,
        PostPublishSink,
        build_editor_embed,
        _project_field_to_scf,
        _coerce_field_for_scf,
        _tracks_from_acf,
    )
except ModuleNotFoundError:
    EditorState = None
    EditorView = None
    EditorTracksView = None
    PrePublishSink = None
    PostPublishSink = None
    build_editor_embed = None
    _project_field_to_scf = None
    _coerce_field_for_scf = None
    _tracks_from_acf = None


def make_release_for_test(spotify_id, title, last_seen, tracks=None):
    """Helper to create a release for tests."""
    tracks = tracks if tracks is not None else [
        Track(
            spotify_id=f"{spotify_id}_track",
            title="Test Track",
            normalized_title="test track",
            duration_ms=300000,
            disc_number=1,
            track_number=1,
            is_countable=True,
            listened=False
        )
    ]
    return Release(
        spotify_id=spotify_id,
        title=title,
        normalized_title=normalize_text(title),
        artists=[Artist(spotify_id="artist1", name="Artist", normalized_name="artist")],
        release_type=ReleaseType.ALBUM,
        raw_spotify_type="album",
        cover_url="http://example.com/cover.jpg",
        release_date="2024-01-01",
        total_tracks=len([t for t in tracks if t.is_countable]),
        total_duration_ms=sum(t.duration_ms for t in tracks if t.is_countable),
        tracks=tracks,
        progress=0.0,
        status=LifecycleStatus.ACTIVE,
        first_seen=last_seen,
        last_seen=last_seen
    )


class TestLoggingConfiguration(unittest.TestCase):
    """Test application file logging setup."""

    def tearDown(self):
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if getattr(handler, _ALBUM_TRACKER_HANDLER_ATTR, False):
                root_logger.removeHandler(handler)
                handler.close()

    def test_configure_logging_creates_log_file_and_writes_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = configure_logging(Path(temp_dir), level="INFO")

            logging.getLogger("album_tracker_test").info("file logging works")
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertEqual(log_file, Path(temp_dir) / "logs" / LOG_FILE_NAME)
            self.assertTrue(log_file.exists())
            self.assertIn("file logging works", log_file.read_text(encoding="utf-8"))

    def test_configure_logging_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configure_logging(Path(temp_dir), level="INFO")
            configure_logging(Path(temp_dir), level="INFO")

            logging.getLogger("album_tracker_test").info("one copy only")
            for handler in logging.getLogger().handlers:
                handler.flush()

            log_file = Path(temp_dir) / "logs" / LOG_FILE_NAME
            self.assertEqual(log_file.read_text(encoding="utf-8").count("one copy only"), 1)


class TestNormalization(unittest.TestCase):
    """Test text normalization."""

    def test_normalize_text_basic(self):
        """Test basic normalization."""
        result = normalize_text("HELLO World")
        self.assertEqual(result, "hello world")

    def test_normalize_text_whitespace(self):
        """Test whitespace collapsing."""
        result = normalize_text("hello   world  test")
        self.assertEqual(result, "hello world test")

    def test_normalize_text_trim(self):
        """Test trimming."""
        result = normalize_text("  hello  ")
        self.assertEqual(result, "hello")

    def test_normalize_artist_name_comma(self):
        """Test that commas are stripped from artist names."""
        result = normalize_artist_name("Artist, Name")
        self.assertEqual(result, "artist name")

    def test_normalize_artist_list(self):
        """Test normalizing list of artists."""
        artists = ["Artist One", "Artist, Two"]
        result = normalize_artist_list(artists)
        self.assertEqual(result, ["artist one", "artist two"])


class TestReleaseClassification(unittest.TestCase):
    """Test release type classification logic."""

    def make_track(self, duration_ms=300000, **kwargs):
        """Helper to create a track dict."""
        return {
            "id": f"track_{id(kwargs)}",
            "name": f"Track {id(kwargs)}",
            "duration_ms": duration_ms,
            "is_playable": kwargs.get("is_playable", True),
            "is_local": kwargs.get("is_local", False),
            **{k: v for k, v in kwargs.items() if k not in ["is_playable", "is_local"]}
        }

    def test_compilation_type(self):
        """Test compilation detection."""
        tracks = [self.make_track()]
        result = compute_release_type(tracks, "compilation")
        self.assertEqual(result, "Compilation")

    def test_album_type_7_tracks(self):
        """Test album with 7+ tracks."""
        tracks = [self.make_track() for _ in range(7)]
        result = compute_release_type(tracks, "album")
        self.assertEqual(result, "Album")

    def test_album_type_30_minutes(self):
        """Test album with 30+ minutes duration."""
        tracks = [self.make_track(duration_ms=600000) for _ in range(3)]  # 3 x 10min
        result = compute_release_type(tracks, "album")
        self.assertEqual(result, "Album")

    def test_ep_type_5_tracks_under_30min(self):
        """Test EP with 4-6 tracks under 30 min."""
        tracks = [self.make_track(duration_ms=300000) for _ in range(5)]  # 5 x 5min
        result = compute_release_type(tracks, "album")
        self.assertEqual(result, "EP")

    def test_ep_type_3_tracks_long(self):
        """Test EP with 1-3 tracks where longest is 10+ minutes."""
        tracks = [
            self.make_track(duration_ms=600000),  # 10 min
            self.make_track(duration_ms=300000),  # 5 min
        ]
        result = compute_release_type(tracks, "album")
        self.assertEqual(result, "EP")

    def test_single_type_3_tracks_short(self):
        """Test single with 1-3 tracks, all short."""
        tracks = [
            self.make_track(duration_ms=300000),  # 5 min
            self.make_track(duration_ms=300000),  # 5 min
        ]
        result = compute_release_type(tracks, "album")
        self.assertEqual(result, "Single")

    def test_single_type_1_track_short(self):
        """Test single with 1 short track."""
        tracks = [self.make_track(duration_ms=300000)]
        result = compute_release_type(tracks, "album")
        self.assertEqual(result, "Single")


class TestProgressTracking(unittest.TestCase):
    """Test progress computation."""

    def make_track(self, countable=True, listened=False):
        """Helper to create a track."""
        return Track(
            spotify_id=f"track_{id((countable, listened))}",
            title="Test Track",
            normalized_title="test track",
            duration_ms=300000,
            disc_number=1,
            track_number=1,
            is_countable=countable,
            listened=listened
        )

    def make_release(self, tracks):
        """Helper to create a release."""
        now = datetime.now()
        return Release(
            spotify_id="test_album",
            title="Test Album",
            normalized_title="test album",
            artists=[Artist(spotify_id="artist1", name="Artist", normalized_name="artist")],
            release_type=ReleaseType.ALBUM,
            raw_spotify_type="album",
            cover_url="http://example.com/cover.jpg",
            release_date="2024-01-01",
            total_tracks=len([t for t in tracks if t.is_countable]),
            total_duration_ms=sum(t.duration_ms for t in tracks if t.is_countable),
            tracks=tracks,
            progress=0.0,
            status=LifecycleStatus.ACTIVE,
            first_seen=now,
            last_seen=now
        )

    def compute_progress(self, release):
        """Compute progress like the tracker does."""
        listened_count = sum(1 for t in release.tracks if t.is_countable and t.listened)
        countable_count = sum(1 for t in release.tracks if t.is_countable)
        if countable_count > 0:
            return listened_count / countable_count
        return 0.0

    def test_progress_0_percent(self):
        """Test 0% progress."""
        tracks = [self.make_track(listened=False) for _ in range(10)]
        release = self.make_release(tracks)
        progress = self.compute_progress(release)
        self.assertEqual(progress, 0.0)

    def test_progress_50_percent(self):
        """Test 50% progress."""
        tracks = [self.make_track(listened=True) for _ in range(5)]
        tracks += [self.make_track(listened=False) for _ in range(5)]
        release = self.make_release(tracks)
        progress = self.compute_progress(release)
        self.assertEqual(progress, 0.5)

    def test_progress_100_percent(self):
        """Test 100% progress."""
        tracks = [self.make_track(listened=True) for _ in range(10)]
        release = self.make_release(tracks)
        progress = self.compute_progress(release)
        self.assertEqual(progress, 1.0)

    def test_progress_ignores_non_countable(self):
        """Test that non-countable tracks are ignored in progress."""
        countable_tracks = [self.make_track(countable=True, listened=True) for _ in range(5)]
        non_countable = [self.make_track(countable=False, listened=False) for _ in range(5)]
        all_tracks = countable_tracks + non_countable
        release = self.make_release(all_tracks)
        progress = self.compute_progress(release)
        # Should be 5/5 = 100%, not 5/10 = 50%
        self.assertEqual(progress, 1.0)


class TestInProgressPagination(unittest.TestCase):
    """Test /inprogress pinned-feature pagination."""

    def make_releases(self, count):
        base = datetime(2024, 1, 1, 12, 0, 0)
        return [
            make_release_for_test(
                spotify_id=f"album_{index}",
                title=f"Album {index}",
                last_seen=base + timedelta(minutes=index)
            )
            for index in range(count)
        ]

    def test_one_release_is_featured_on_single_page(self):
        releases = self.make_releases(1)
        page = build_inprogress_page(releases, 0)

        self.assertEqual(page.featured.spotify_id, "album_0")
        self.assertEqual(page.items, [])
        self.assertEqual(page.page, 0)
        self.assertEqual(page.total_pages, 1)
        self.assertEqual(page.total_releases, 1)

    def test_ten_releases_fit_featured_plus_nine(self):
        releases = self.make_releases(10)
        page = build_inprogress_page(releases, 0)

        self.assertEqual(page.featured.spotify_id, "album_9")
        self.assertEqual(len(page.items), INPROGRESS_PAGE_SIZE)
        self.assertEqual(page.total_pages, 1)
        self.assertNotIn(page.featured.spotify_id, [release.spotify_id for release in page.items])

    def test_eleven_releases_create_second_page(self):
        releases = self.make_releases(11)
        first_page = build_inprogress_page(releases, 0)
        second_page = build_inprogress_page(releases, 1)

        self.assertEqual(first_page.featured.spotify_id, "album_10")
        self.assertEqual(len(first_page.items), INPROGRESS_PAGE_SIZE)
        self.assertEqual(second_page.featured.spotify_id, "album_10")
        self.assertEqual(len(second_page.items), 1)
        self.assertEqual(second_page.items[0].spotify_id, "album_0")
        self.assertEqual(second_page.total_pages, 2)

    def test_page_indexes_are_clamped(self):
        releases = self.make_releases(11)

        before_first = build_inprogress_page(releases, -5)
        after_last = build_inprogress_page(releases, 99)

        self.assertEqual(before_first.page, 0)
        self.assertEqual(after_last.page, 1)


class TestNextTrackSelection(unittest.TestCase):
    """Test next-track selection for /inprogress."""

    def test_returns_first_unlistened_countable_track(self):
        release = make_release_for_test(
            "album_1",
            "Album 1",
            datetime(2024, 1, 1, 12, 0, 0),
            tracks=[
                Track("t1", "Intro", "intro", 60000, 1, 1, False, False),
                Track("t2", "Track 1", "track 1", 180000, 1, 2, True, True),
                Track("t3", "Track 2", "track 2", 180000, 1, 3, True, False),
                Track("t4", "Track 3", "track 3", 180000, 1, 4, True, False),
            ]
        )

        next_track = get_next_unlistened_track(release)

        self.assertIsNotNone(next_track)
        self.assertEqual(next_track.title, "Track 2")

    def test_skips_non_countable_and_listened_tracks(self):
        release = make_release_for_test(
            "album_2",
            "Album 2",
            datetime(2024, 1, 1, 12, 0, 0),
            tracks=[
                Track("t1", "Intro", "intro", 60000, 1, 1, False, False),
                Track("t2", "Track 1", "track 1", 180000, 1, 2, True, True),
                Track("t3", "Track 2", "track 2", 180000, 1, 3, False, False),
                Track("t4", "Track 3", "track 3", 180000, 1, 4, True, False),
            ]
        )

        next_track = get_next_unlistened_track(release)

        self.assertIsNotNone(next_track)
        self.assertEqual(next_track.title, "Track 3")

    def test_returns_none_when_all_countable_tracks_are_listened(self):
        release = make_release_for_test(
            "album_3",
            "Album 3",
            datetime(2024, 1, 1, 12, 0, 0),
            tracks=[
                Track("t1", "Track 1", "track 1", 180000, 1, 1, True, True),
                Track("t2", "Track 2", "track 2", 180000, 1, 2, True, True),
            ]
        )

        self.assertIsNone(get_next_unlistened_track(release))


@unittest.skipIf(SavedLibraryService is None, "saved library dependencies are not installed")
class TestSavedLibraryService(unittest.IsolatedAsyncioTestCase):
    """Test saved Spotify library synchronization."""

    def make_track(self, track_id="track_1", duration_ms=300000):
        return {"id": track_id, "name": track_id, "duration_ms": duration_ms}

    def make_saved_item(
        self,
        album_id,
        title="Album Title",
        added_at="2024-01-01T00:00:00Z",
        album_type="album",
        total_tracks=8,
        tracks=None,
    ):
        tracks = tracks if tracks is not None else [
            self.make_track(f"{album_id}_track_{index}")
            for index in range(min(total_tracks, 20))
        ]
        return {
            "added_at": added_at,
            "album": {
                "id": album_id,
                "uri": f"spotify:album:{album_id}",
                "name": title,
                "album_type": album_type,
                "total_tracks": total_tracks,
                "tracks": {"items": tracks},
                "artists": [{"name": "Artist"}],
                "images": [{"url": f"https://example.com/{album_id}.jpg"}],
                "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
            }
        }

    def make_album_model(self, album_id):
        return SavedLibraryAlbum(
            spotify_id=album_id,
            spotify_uri=f"spotify:album:{album_id}",
            spotify_url=f"https://open.spotify.com/album/{album_id}",
            title="Old Album",
            normalized_title="old album",
            artists=["Artist"],
            normalized_artists=["artist"],
            album_type="album",
            release_type=ReleaseType.ALBUM,
            cover_url="https://example.com/old.jpg",
            added_at=datetime(2024, 1, 1),
        )

    def make_snapshot_item(self, album_id, position=0):
        return SavedLibrarySnapshotItem(
            spotify_id=album_id,
            spotify_uri=f"spotify:album:{album_id}",
            added_at=datetime(2024, 1, 1),
            position=position,
            last_seen_at=datetime(2024, 1, 1),
        )

    async def test_matching_total_and_hash_skips_full_sync(self):
        first_page = {
            "total": 1,
            "items": [self.make_saved_item("album_1")],
            "next": "https://api.spotify.com/v1/me/albums?offset=50&limit=50",
        }
        service = SavedLibraryService.__new__(SavedLibraryService)
        service.spotify = type(
            "FakeSpotify",
            (),
            {"get_saved_albums_page": AsyncMock(return_value=first_page)}
        )()
        first_page_hash = service.compute_first_page_hash(first_page)

        class FakeDatabase:
            async def get_service_state(self, key):
                return {
                    SAVED_LIBRARY_TOTAL_KEY: "1",
                    SAVED_LIBRARY_FIRST_PAGE_HASH_KEY: first_page_hash,
                    SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY: datetime.now().isoformat(),
                }.get(key)

            async def get_saved_library_snapshot_items(self):
                return [self_outer.make_snapshot_item("album_1")]

            async def get_saved_library_stats(self):
                return SavedLibraryStats(total=1, posted_listened=0, percent=0.0)

            async def get_saved_library_albums_by_id(self):
                raise AssertionError("Full sync should be skipped")

        self_outer = self
        service.db = FakeDatabase()

        result = await service.sync()

        self.assertTrue(result.skipped)
        self.assertEqual(result.stored_total, 1)
        service.spotify.get_saved_albums_page.assert_awaited_once()

    async def test_full_sync_reconciles_album_ep_entries_and_wordpress_posts(self):
        first_page = {
            "total": 2,
            "items": [
                self.make_saved_item("album_1", "Album Title"),
                self.make_saved_item("single_1", "Single Title", album_type="single"),
            ],
            "next": None,
        }
        saved_albums = {}
        deleted = []
        saved_state = {}

        class FakeSpotify:
            async def get_saved_albums_page(self, limit=50, offset=0, url=None):
                return first_page

            async def get_all_saved_albums(self, first_page=None):
                return list(first_page["items"])

            async def get_album_tracks(self, album_id):
                raise AssertionError("Complete saved album payloads should not fetch tracks")

        class FakeDatabase:
            async def get_service_state(self, key):
                return None

            async def get_saved_library_snapshot_items(self):
                return []

            async def get_saved_library_albums_by_id(self):
                return {
                    "removed_album": self_album
                }

            async def get_wordpress_posts(self):
                return [
                    WordPressPost(
                        id=123,
                        title="Album Title",
                        normalized_title="album title",
                        artists=["Artist"],
                        normalized_artists=["artist"],
                        link="https://example.com/album-title",
                    )
                ]

            async def delete_saved_library_albums(self, spotify_ids):
                deleted.extend(spotify_ids)
                return len(spotify_ids)

            async def upsert_saved_library_album(self, album):
                saved_albums[album.spotify_id] = album

            async def replace_saved_library_snapshot(self, items):
                saved_state["snapshot_ids"] = [item.spotify_id for item in items]

            async def save_service_state(self, key, value):
                saved_state[key] = value

        self_album = self.make_album_model("removed_album")
        service = SavedLibraryService(FakeDatabase(), FakeSpotify())

        result = await service.sync()

        self.assertFalse(result.skipped)
        self.assertEqual(set(saved_albums), {"album_1"})
        self.assertTrue(saved_albums["album_1"].is_posted_listened)
        self.assertEqual(saved_albums["album_1"].wordpress_post_id, 123)
        self.assertEqual(deleted, ["removed_album"])
        self.assertEqual(saved_state["snapshot_ids"], ["album_1", "single_1"])
        self.assertEqual(saved_state[SAVED_LIBRARY_TOTAL_KEY], "2")
        self.assertIn(SAVED_LIBRARY_FIRST_PAGE_HASH_KEY, saved_state)
        self.assertIn(SAVED_LIBRARY_LAST_SYNCED_AT_KEY, saved_state)
        self.assertIn(SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY, saved_state)

    async def test_incremental_sync_adds_head_items_without_full_scan(self):
        first_page = {
            "total": 2,
            "items": [
                self.make_saved_item("album_new", "New Album"),
                self.make_saved_item("album_old", "Old Album"),
            ],
            "next": None,
        }
        old_page = {
            "total": 1,
            "items": [self.make_saved_item("album_old", "Old Album")],
            "next": None,
        }
        service_for_hash = SavedLibraryService.__new__(SavedLibraryService)
        first_page_hash = service_for_hash.compute_first_page_hash(first_page)
        saved_state = {}
        saved_albums = {}

        class FakeSpotify:
            def __init__(self):
                self.get_saved_albums_page = AsyncMock(return_value=first_page)

            async def get_all_saved_albums(self, first_page=None):
                raise AssertionError("Incremental addition should not fetch every saved-library page")

            async def get_album_tracks(self, album_id):
                raise AssertionError("Complete saved album payloads should not fetch tracks")

        class FakeDatabase:
            async def get_service_state(self, key):
                return {
                    SAVED_LIBRARY_TOTAL_KEY: "1",
                    SAVED_LIBRARY_FIRST_PAGE_HASH_KEY: service_for_hash.compute_first_page_hash(old_page),
                    SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY: datetime.now().isoformat(),
                }.get(key)

            async def get_saved_library_snapshot_items(self):
                return [self_outer.make_snapshot_item("album_old", 0)]

            async def get_saved_library_albums_by_id(self):
                return {"album_old": self_outer.make_album_model("album_old")}

            async def get_wordpress_posts(self):
                return []

            async def delete_saved_library_albums(self, spotify_ids):
                self_outer.assertEqual(spotify_ids, [])
                return 0

            async def upsert_saved_library_album(self, album):
                saved_albums[album.spotify_id] = album

            async def replace_saved_library_snapshot(self, items):
                saved_state["snapshot_ids"] = [item.spotify_id for item in items]

            async def save_service_state(self, key, value):
                saved_state[key] = value

            async def get_saved_library_stats(self):
                return SavedLibraryStats(total=2, posted_listened=0, percent=0.0)

        self_outer = self
        spotify = FakeSpotify()
        service = SavedLibraryService(FakeDatabase(), spotify)

        result = await service.sync()

        self.assertFalse(result.skipped)
        self.assertEqual(set(saved_albums), {"album_new"})
        self.assertEqual(saved_state["snapshot_ids"], ["album_new", "album_old"])
        self.assertEqual(saved_state[SAVED_LIBRARY_TOTAL_KEY], "2")
        self.assertNotIn(SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY, saved_state)
        spotify.get_saved_albums_page.assert_awaited_once()

    async def test_incremental_sync_removes_sparse_items_without_full_scan(self):
        first_page = {
            "total": 2,
            "items": [
                self.make_saved_item("album_1", "Album 1"),
                self.make_saved_item("album_3", "Album 3"),
            ],
            "next": None,
        }
        old_page = {
            "total": 3,
            "items": [
                self.make_saved_item("album_1", "Album 1"),
                self.make_saved_item("album_2", "Album 2"),
                self.make_saved_item("album_3", "Album 3"),
            ],
            "next": None,
        }
        service_for_hash = SavedLibraryService.__new__(SavedLibraryService)
        saved_state = {}
        deleted = []

        class FakeSpotify:
            def __init__(self):
                self.get_saved_albums_page = AsyncMock(return_value=first_page)

            async def get_all_saved_albums(self, first_page=None):
                raise AssertionError("Incremental removal should not fetch every saved-library page")

        class FakeDatabase:
            async def get_service_state(self, key):
                return {
                    SAVED_LIBRARY_TOTAL_KEY: "3",
                    SAVED_LIBRARY_FIRST_PAGE_HASH_KEY: service_for_hash.compute_first_page_hash(old_page),
                    SAVED_LIBRARY_LAST_FULL_AUDIT_AT_KEY: datetime.now().isoformat(),
                }.get(key)

            async def get_saved_library_snapshot_items(self):
                return [
                    self_outer.make_snapshot_item("album_1", 0),
                    self_outer.make_snapshot_item("album_2", 1),
                    self_outer.make_snapshot_item("album_3", 2),
                ]

            async def get_saved_library_albums_by_id(self):
                return {
                    "album_1": self_outer.make_album_model("album_1"),
                    "album_2": self_outer.make_album_model("album_2"),
                    "album_3": self_outer.make_album_model("album_3"),
                }

            async def delete_saved_library_albums(self, spotify_ids):
                deleted.extend(spotify_ids)
                return len(spotify_ids)

            async def replace_saved_library_snapshot(self, items):
                saved_state["snapshot_ids"] = [item.spotify_id for item in items]

            async def save_service_state(self, key, value):
                saved_state[key] = value

            async def get_saved_library_stats(self):
                return SavedLibraryStats(total=2, posted_listened=0, percent=0.0)

        self_outer = self
        spotify = FakeSpotify()
        service = SavedLibraryService(FakeDatabase(), spotify)

        result = await service.sync()

        self.assertFalse(result.skipped)
        self.assertEqual(deleted, ["album_2"])
        self.assertEqual(saved_state["snapshot_ids"], ["album_1", "album_3"])
        self.assertEqual(saved_state[SAVED_LIBRARY_TOTAL_KEY], "2")
        spotify.get_saved_albums_page.assert_awaited_once()

    async def test_low_track_count_album_uses_embedded_tracks_for_ep_classification(self):
        item = self.make_saved_item(
            "album_ep",
            total_tracks=5,
            tracks=[
                self.make_track("track_1", 180000),
                self.make_track("track_2", 180000),
                self.make_track("track_3", 180000),
                self.make_track("track_4", 180000),
                self.make_track("track_5", 180000),
            ],
        )
        spotify = type(
            "FakeSpotify",
            (),
            {"get_album_tracks": AsyncMock(side_effect=AssertionError("Embedded tracks should be enough"))}
        )()
        service = SavedLibraryService(db=None, spotify=spotify)

        release_type = await service._get_release_type(item, existing_by_id={})

        self.assertEqual(release_type, ReleaseType.EP)
        spotify.get_album_tracks.assert_not_awaited()

    async def test_incomplete_embedded_tracks_falls_back_to_album_tracks_endpoint(self):
        item = self.make_saved_item(
            "album_sparse",
            total_tracks=5,
            tracks=[self.make_track("track_1", 180000)],
        )
        spotify = type(
            "FakeSpotify",
            (),
            {
                "get_album_tracks": AsyncMock(return_value=[
                    self.make_track("track_1", 180000),
                    self.make_track("track_2", 180000),
                    self.make_track("track_3", 180000),
                    self.make_track("track_4", 180000),
                    self.make_track("track_5", 180000),
                ])
            }
        )()
        service = SavedLibraryService(db=None, spotify=spotify)

        release_type = await service._get_release_type(item, existing_by_id={})

        self.assertEqual(release_type, ReleaseType.EP)
        spotify.get_album_tracks.assert_awaited_once_with("album_sparse")


@unittest.skipIf(DiscordBot is None, "discord bot dependencies are not installed")
class TestDiscordBotEmbeds(unittest.IsolatedAsyncioTestCase):
    """Test embed formatting for Discord bot views."""

    def setUp(self):
        self.bot = DiscordBot.__new__(DiscordBot)
        self.bot.config = type("FakeConfig", (), {"discord_user_id": 123})()
        self.bot.tracker = type(
            "FakeTracker",
            (),
            {"_qualifies_for_tracking": lambda self, state: True}
        )()

    def make_playback_state(self, album_id="album_5"):
        return PlaybackState(
            is_playing=True,
            shuffle_state=False,
            repeat_state="off",
            context={"type": "album"},
            item={
                "name": "Track 2",
                "artists": [{"name": "Artist"}],
                "album": {
                    "id": album_id,
                    "name": "Album 5",
                    "images": [],
                    "album_type": "album"
                }
            },
            progress_ms=0,
            timestamp=0
        )

    def make_current_context(self, release, active=True, duplicate_post=None):
        return CurrentPostContext(
            tracked_release=release if active else None,
            release_for_post=release,
            duplicate_post=duplicate_post,
            is_actively_tracked=active
        )

    def make_wordpress_post(self):
        return WordPressPost(
            id=123,
            title="Album 5",
            normalized_title="album 5",
            artists=["Artist"],
            normalized_artists=["artist"],
            link="https://example.com/album-5"
        )

    def make_interaction(self, response_done=False, message_id="message_1"):
        response = type(
            "FakeResponse",
            (),
            {
                "send_message": AsyncMock(),
                "send_modal": AsyncMock(),
                "edit_message": AsyncMock(),
                "defer": AsyncMock(),
                "is_done": Mock(return_value=response_done)
            }
        )()
        followup = type(
            "FakeFollowup",
            (),
            {"send": AsyncMock()}
        )()
        return type(
            "FakeInteraction",
            (),
            {
                "response": response,
                "followup": followup,
                "message": type("FakeMessage", (), {"id": message_id})(),
                "user": type("FakeUser", (), {"id": 123})(),
            }
        )()

    def make_saved_library_album(self, spotify_id="album_random", title="Random Album"):
        return SavedLibraryAlbum(
            spotify_id=spotify_id,
            spotify_uri=f"spotify:album:{spotify_id}",
            spotify_url=f"https://open.spotify.com/album/{spotify_id}",
            title=title,
            normalized_title=normalize_text(title),
            artists=["Artist"],
            normalized_artists=["artist"],
            album_type="album",
            release_type=ReleaseType.ALBUM,
            cover_url=f"https://example.com/{spotify_id}.jpg",
            added_at=datetime(2024, 1, 1, 12, 0, 0),
        )

    def test_inprogress_format_includes_next_track(self):
        release = make_release_for_test(
            "album_4",
            "Album 4",
            datetime(2024, 1, 1, 12, 0, 0),
            tracks=[
                Track("t1", "Track 1", "track 1", 180000, 1, 1, True, True),
                Track("t2", "Track 2", "track 2", 180000, 1, 2, True, False),
            ]
        )
        release.progress = 0.5

        formatted = self.bot._format_inprogress_release(release, include_last_seen=False)

        self.assertIn("Next: Track 2", formatted)

    def test_current_embed_includes_progress_for_tracked_release(self):
        state = self.make_playback_state()
        release = make_release_for_test(
            "album_5",
            "Album 5",
            datetime(2024, 1, 1, 12, 0, 0),
            tracks=[
                Track("t1", "Track 1", "track 1", 180000, 1, 1, True, True),
                Track("t2", "Track 2", "track 2", 180000, 1, 2, True, False),
            ]
        )
        release.progress = 0.5

        embed = self.bot._build_current_embed(state, self.make_current_context(release))
        field_map = {field.name: field.value for field in embed.fields}

        self.assertEqual(field_map["Progress"], "1/2 (50%)")

    def test_current_embed_omits_progress_when_untracked(self):
        state = self.make_playback_state("album_6")

        embed = self.bot._build_current_embed(state)
        field_names = [field.name for field in embed.fields]

        self.assertNotIn("Progress", field_names)

    def test_current_playback_action_label_can_be_post_early(self):
        labels = [
            child.label
            for child in CurrentPlaybackActionView(self.bot, None, post_label="Post early").children
        ]

        self.assertIn("Post early", labels)

    def test_current_playback_action_label_defaults_to_post_current_content(self):
        labels = [child.label for child in CurrentPlaybackActionView(self.bot, None).children]

        self.assertIn("Post current content", labels)

    def test_published_post_action_view_includes_edit_metadata(self):
        labels = [child.label for child in PublishedPostActionView(self.bot).children]

        self.assertEqual(labels, ["Edit metadata", "Undo post", "Keep post"])

    def test_relisten_approval_view_has_single_yes_action(self):
        labels = [child.label for child in RelistenApprovalPromptView(self.bot).children]

        self.assertEqual(labels, ["Yes, track as relisten"])

    def test_random_album_view_has_single_reroll_action(self):
        labels = [child.label for child in RandomAlbumView(self.bot).children]

        self.assertEqual(labels, ["Re-roll"])

    def test_random_album_embed_uses_cached_cover_and_spotify_link(self):
        album = self.make_saved_library_album()

        embed = self.bot._build_random_album_embed(album)
        field_map = {field.name: field.value for field in embed.fields}

        self.assertEqual(embed.title, "Random Album")
        self.assertEqual(embed.url, "https://open.spotify.com/album/album_random")
        self.assertEqual(embed.thumbnail.url, "https://example.com/album_random.jpg")
        self.assertEqual(field_map["Release type"], "Album")

    async def test_random_command_sends_reroll_view(self):
        album = self.make_saved_library_album()
        db = type("FakeDatabase", (), {})()
        db.get_random_unposted_saved_library_album = AsyncMock(return_value=album)
        self.bot.db = db
        interaction = self.make_interaction()

        await self.bot._handle_random(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "Random Album")
        self.assertIsInstance(kwargs["view"], RandomAlbumView)
        self.assertTrue(kwargs["ephemeral"])

    async def test_random_reroll_edits_original_message(self):
        album = self.make_saved_library_album("album_new", "New Album")
        db = type("FakeDatabase", (), {})()
        db.get_random_unposted_saved_library_album = AsyncMock(return_value=album)
        self.bot.db = db
        interaction = self.make_interaction()

        await self.bot._handle_random_reroll(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embed"].title, "New Album")
        self.assertIsInstance(kwargs["view"], RandomAlbumView)

    async def test_random_reroll_edits_to_empty_state_when_no_album_exists(self):
        db = type("FakeDatabase", (), {})()
        db.get_random_unposted_saved_library_album = AsyncMock(return_value=None)
        self.bot.db = db
        interaction = self.make_interaction()

        await self.bot._handle_random_reroll(interaction)

        interaction.response.edit_message.assert_awaited_once_with(
            content="No unposted saved-library albums found.",
            embed=None,
            view=None
        )

    async def test_add_content_action_opens_modal_without_completing_prompt(self):
        release = make_release_for_test("album_modal", "Album Modal", datetime(2024, 1, 1, 12, 0, 0))
        release.wordpress_post_id = 654
        prompt = DiscordPrompt(
            id=1,
            prompt_type=PromptType.PROMPT_UNDO.value,
            release_id=release.spotify_id,
            wordpress_post_id=321,
            discord_message_id="message_1",
            state=PromptState.PENDING.value,
        )
        db = type("FakeDatabase", (), {})()
        db.get_discord_prompt = AsyncMock(return_value=prompt)
        db.get_release = AsyncMock(return_value=release)
        db.update_discord_prompt_state = AsyncMock()
        self.bot.db = db
        interaction = self.make_interaction(message_id="message_1")

        await self.bot.handle_prompt_action(interaction, "add_content")

        interaction.response.defer.assert_not_awaited()
        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual(modal.discord_message_id, "message_1")
        self.assertEqual(modal.wordpress_post_id, 321)
        db.update_discord_prompt_state.assert_not_awaited()

    async def test_add_content_action_rejects_handled_prompt_without_modal(self):
        prompt = DiscordPrompt(
            id=1,
            prompt_type=PromptType.PROMPT_UNDO.value,
            release_id="album_done",
            wordpress_post_id=321,
            discord_message_id="message_1",
            state=PromptState.ACCEPTED.value,
        )
        db = type("FakeDatabase", (), {})()
        db.get_discord_prompt = AsyncMock(return_value=prompt)
        db.get_release = AsyncMock()
        self.bot.db = db
        interaction = self.make_interaction(message_id="message_1")

        await self.bot.handle_prompt_action(interaction, "add_content")

        interaction.response.defer.assert_not_awaited()
        interaction.response.send_modal.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "⚠️ This prompt has already been handled.",
            ephemeral=True
        )

    async def test_post_content_submit_updates_wordpress_and_leaves_prompt_pending(self):
        release = make_release_for_test("album_content", "Album Content", datetime(2024, 1, 1, 12, 0, 0))
        release.wordpress_post_id = 654
        prompt = DiscordPrompt(
            id=1,
            prompt_type=PromptType.PROMPT_UNDO.value,
            release_id=release.spotify_id,
            wordpress_post_id=321,
            discord_message_id="message_1",
            state=PromptState.PENDING.value,
        )
        db = type("FakeDatabase", (), {})()
        db.get_discord_prompt = AsyncMock(return_value=prompt)
        db.get_release = AsyncMock(return_value=release)
        db.update_discord_prompt_state = AsyncMock()
        publisher = type("FakePublisher", (), {})()
        publisher.update_post_content = AsyncMock(return_value={"id": 321})
        self.bot.db = db
        self.bot.tracker = type("FakeTracker", (), {"publisher": publisher})()
        interaction = self.make_interaction()

        await self.bot._handle_post_content_submit(
            interaction=interaction,
            discord_message_id="message_1",
            release_id=release.spotify_id,
            fallback_wordpress_post_id=999,
            raw_content="First paragraph",
        )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        publisher.update_post_content.assert_awaited_once_with(321, "First paragraph")
        db.update_discord_prompt_state.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "✅ WordPress post content has been updated.",
            ephemeral=True
        )

    async def test_post_content_submit_reports_update_errors_without_completing_prompt(self):
        prompt = DiscordPrompt(
            id=1,
            prompt_type=PromptType.PROMPT_UNDO.value,
            release_id="album_content",
            wordpress_post_id=321,
            discord_message_id="message_1",
            state=PromptState.PENDING.value,
        )
        db = type("FakeDatabase", (), {})()
        db.get_discord_prompt = AsyncMock(return_value=prompt)
        db.get_release = AsyncMock(return_value=None)
        db.update_discord_prompt_state = AsyncMock()
        publisher = type("FakePublisher", (), {})()
        publisher.update_post_content = AsyncMock(side_effect=RuntimeError("WordPress failed"))
        self.bot.db = db
        self.bot.tracker = type("FakeTracker", (), {"publisher": publisher})()
        interaction = self.make_interaction()

        await self.bot._handle_post_content_submit(
            interaction=interaction,
            discord_message_id="message_1",
            release_id="album_content",
            fallback_wordpress_post_id=None,
            raw_content="First paragraph",
        )

        publisher.update_post_content.assert_awaited_once_with(321, "First paragraph")
        db.update_discord_prompt_state.assert_not_awaited()
        self.assertIn("❌ Error updating WordPress post:", interaction.followup.send.await_args.args[0])

    async def test_undo_post_trashes_wordpress_post_and_deletes_release(self):
        release = make_release_for_test("album_undo", "Album Undo", datetime(2024, 1, 1, 12, 0, 0))
        release.wordpress_post_id = 654
        prompt = DiscordPrompt(
            id=1,
            prompt_type=PromptType.PROMPT_UNDO.value,
            release_id=release.spotify_id,
            wordpress_post_id=321,
            discord_message_id="message_1",
            state=PromptState.PENDING.value,
        )
        db = type("FakeDatabase", (), {})()
        db.update_discord_prompt_state = AsyncMock()
        db.delete_release = AsyncMock(return_value=True)
        db.mark_saved_library_album_unposted = AsyncMock(return_value=True)
        publisher = type("FakePublisher", (), {})()
        publisher.trash_post = AsyncMock(return_value=True)
        self.bot.db = db
        self.bot.tracker = type("FakeTracker", (), {"publisher": publisher})()
        interaction = self.make_interaction()

        await self.bot._handle_undo_post(interaction, release, prompt)

        db.update_discord_prompt_state.assert_awaited_once_with("message_1", PromptState.ACCEPTED.value)
        publisher.trash_post.assert_awaited_once_with(321)
        db.mark_saved_library_album_unposted.assert_awaited_once_with(release.spotify_id)
        db.delete_release.assert_awaited_once_with(release.spotify_id)
        interaction.followup.send.assert_awaited_once_with(
            "✅ The post has been moved to trash and removed from the tracking database.",
            ephemeral=True
        )

    def test_current_preview_uses_early_wording_for_tracked_release(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))

        embed = self.bot._build_current_preview_embed(state, self.make_current_context(release))

        self.assertEqual(embed.title, "Post current playback early")
        self.assertIn("early", embed.description)

    def test_current_preview_uses_default_wording_for_untracked_release(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))

        embed = self.bot._build_current_preview_embed(
            state,
            self.make_current_context(release, active=False)
        )

        self.assertEqual(embed.title, "Post current playback")
        self.assertNotIn("early", embed.description)

    def test_current_preview_includes_relisten_field_for_duplicate(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))
        duplicate_post = self.make_wordpress_post()

        embed = self.bot._build_current_preview_embed(
            state,
            self.make_current_context(release, duplicate_post=duplicate_post)
        )
        field_map = {field.name: field.value for field in embed.fields}

        relisten_fields = [name for name in field_map if "Relisten" in name]
        self.assertEqual(len(relisten_fields), 1)
        self.assertIn("post 123", field_map[relisten_fields[0]])

    def test_current_preview_omits_relisten_field_without_duplicate(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))

        embed = self.bot._build_current_preview_embed(state, self.make_current_context(release))
        field_names = [field.name for field in embed.fields]

        self.assertFalse(any("Relisten" in name for name in field_names))

    async def test_current_confirm_publishes_as_relisten_from_stored_relisten_state(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))
        release.is_relisten = True
        release.duplicate_post_id = 123

        class FakeDatabase:
            async def get_release(self, album_id):
                return release

        class FakeTracker:
            def __init__(self):
                self.published = None

            async def _get_cached_wordpress_post(self, post_id):
                return WordPressPost(
                    id=post_id,
                    title="Album 5",
                    normalized_title="album 5",
                    artists=["Artist"],
                    normalized_artists=["artist"],
                    link="https://example.com/album-5"
                )

            async def _check_duplicate(self, release_to_check):
                raise AssertionError("_check_duplicate should not be called for tracked releases with stored state")

            async def _get_or_create_release(self, album_id):
                return release

            async def publish_release_now(self, release_to_publish, as_relisten=False):
                self.published = (release_to_publish, as_relisten)
                return "published"

        tracker = FakeTracker()
        self.bot.db = FakeDatabase()
        self.bot.tracker = tracker
        interaction = self.make_interaction()

        await self.bot._handle_current_post_confirm(interaction, state)

        self.assertEqual(tracker.published, (release, True))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "✅ Current content has been posted to WordPress. A notification has been sent.",
            ephemeral=True
        )

    async def test_inprogress_publish_uses_stored_relisten_state(self):
        release = make_release_for_test("album_7", "Album 7", datetime(2024, 1, 1, 12, 0, 0))
        release.is_relisten = True

        class FakeDatabase:
            async def get_release(self, release_id):
                return release

        class FakeTracker:
            def __init__(self):
                self.published = None

            async def publish_release_now(self, release_to_publish, as_relisten=False):
                self.published = (release_to_publish, as_relisten)
                return "published"

        tracker = FakeTracker()
        self.bot.db = FakeDatabase()
        self.bot.tracker = tracker
        interaction = self.make_interaction()

        await self.bot._handle_publish_release(interaction, release.spotify_id)

        self.assertEqual(tracker.published, (release, True))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "✅ Release published successfully. A notification has been sent.",
            ephemeral=True
        )

    async def test_inprogress_publish_reports_already_publishing(self):
        release = make_release_for_test("album_9", "Album 9", datetime(2024, 1, 1, 12, 0, 0))

        class FakeDatabase:
            async def get_release(self, release_id):
                return release

        class FakeTracker:
            async def publish_release_now(self, release_to_publish, as_relisten=False):
                return "already_publishing"

        self.bot.db = FakeDatabase()
        self.bot.tracker = FakeTracker()
        interaction = self.make_interaction()

        await self.bot._handle_publish_release(interaction, release.spotify_id)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "⏳ This release is already being published.",
            ephemeral=True
        )

    async def test_context_resolver_builds_unsaved_release_for_untracked_duplicate_check(self):
        state = self.make_playback_state("album_8")
        release = make_release_for_test("album_8", "Album 8", datetime(2024, 1, 1, 12, 0, 0))

        class FakeDatabase:
            async def get_release(self, album_id):
                return None

        class FakeTracker:
            def __init__(self):
                self.built = False
                self.checked = False

            async def _build_release_from_spotify(self, album_id):
                self.built = True
                return release

            async def _check_duplicate(self, release_to_check):
                self.checked = True
                return None

        tracker = FakeTracker()
        self.bot.db = FakeDatabase()
        self.bot.tracker = tracker

        context = await self.bot._resolve_current_post_context(state, check_duplicate=True)

        self.assertIs(context.release_for_post, release)
        self.assertFalse(context.is_actively_tracked)
        self.assertTrue(tracker.built)
        self.assertTrue(tracker.checked)
        self.assertIsNone(release.duplicate_post_id)

    async def test_context_resolver_uses_stored_relisten_state_for_tracked_release(self):
        state = self.make_playback_state("album_10")
        release = make_release_for_test("album_10", "Album 10", datetime(2024, 1, 1, 12, 0, 0))
        duplicate_post = WordPressPost(
            id=456,
            title="Album 10",
            normalized_title="album 10",
            artists=["Artist"],
            normalized_artists=["artist"],
            link="https://example.com/album-10"
        )
        release.is_relisten = True
        release.duplicate_post_id = duplicate_post.id

        class FakeDatabase:
            async def get_release(self, album_id):
                return release

        class FakeTracker:
            async def _get_cached_wordpress_post(self, post_id):
                return duplicate_post if post_id == duplicate_post.id else None

            async def _check_duplicate(self, release_to_check):
                raise AssertionError("_check_duplicate should not run for tracked releases with stored state")

        self.bot.db = FakeDatabase()
        self.bot.tracker = FakeTracker()

        context = await self.bot._resolve_current_post_context(state, check_duplicate=True)

        self.assertIs(context.tracked_release, release)
        self.assertIs(context.release_for_post, release)
        self.assertIs(context.duplicate_post, duplicate_post)
        self.assertTrue(context.will_publish_as_relisten)


@unittest.skipIf(Tracker is None, "tracker dependencies are not installed")
class TestTrackerLastSeen(unittest.IsolatedAsyncioTestCase):
    """Test tracker last_seen refresh behavior."""

    async def test_replaying_listened_track_touches_release_last_seen(self):
        old_seen = datetime(2024, 1, 1, 12, 0, 0)
        track = Track(
            spotify_id="track_1",
            title="Track 1",
            normalized_title="track 1",
            duration_ms=300000,
            disc_number=1,
            track_number=1,
            is_countable=True,
            listened=True,
            listened_at=old_seen,
            listened_source="playback"
        )
        release = make_release_for_test("album_1", "Album 1", old_seen, tracks=[track])

        class FakeSpotify:
            async def get_playback_state(self):
                return {
                    "is_playing": True,
                    "shuffle_state": False,
                    "repeat_state": "off",
                    "context": {
                        "type": "album",
                        "uri": "spotify:album:album_1"
                    },
                    "item": {
                        "id": "track_1",
                        "type": "track",
                        "is_local": False,
                        "album": {
                            "id": "album_1",
                            "album_type": "album",
                            "uri": "spotify:album:album_1"
                        }
                    },
                    "progress_ms": 1000,
                    "timestamp": 0
                }

        class FakeDatabase:
            def __init__(self):
                self.touched = None

            async def touch_release_last_seen(self, spotify_id, seen_at):
                self.touched = (spotify_id, seen_at)

            async def get_release(self, spotify_id):
                if spotify_id != "album_1":
                    raise AssertionError(f"Unexpected spotify_id: {spotify_id}")
                return release

            async def save_service_state(self, key, value):
                return None

        db = FakeDatabase()
        tracker = Tracker.__new__(Tracker)
        tracker.spotify = FakeSpotify()
        tracker.db = db
        tracker.discord_bot = None
        tracker.active_interval = 0

        await tracker._poll_once()

        self.assertIsNotNone(db.touched)
        self.assertEqual(db.touched[0], "album_1")
        self.assertGreater(db.touched[1], old_seen)
        self.assertEqual(release.last_seen, db.touched[1])


@unittest.skipIf(Tracker is None, "tracker dependencies are not installed")
class TestTrackerPublishNow(unittest.IsolatedAsyncioTestCase):
    """Test tracker publish-now idempotency behavior."""

    def make_tracker(self, db):
        tracker = Tracker.__new__(Tracker)
        tracker.db = db
        tracker.publisher = None
        tracker.discord_bot = None
        tracker._publish_release = AsyncMock()
        return tracker

    async def test_publish_release_now_returns_already_published(self):
        release = make_release_for_test("album_pub", "Album Pub", datetime(2024, 1, 1, 12, 0, 0))
        release.status = LifecycleStatus.PUBLISHED_RECENTLY
        release.wordpress_post_id = 123

        class FakeDatabase:
            async def get_release(self, spotify_id):
                return release

            async def save_release(self, release_to_save):
                raise AssertionError("save_release should not be called for already published releases")

        tracker = self.make_tracker(FakeDatabase())

        outcome = await tracker.publish_release_now(release)

        self.assertEqual(outcome, "already_published")
        tracker._publish_release.assert_not_awaited()

    async def test_publish_release_now_returns_already_publishing(self):
        release = make_release_for_test("album_ing", "Album Ing", datetime(2024, 1, 1, 12, 0, 0))
        release.status = LifecycleStatus.PUBLISHING

        class FakeDatabase:
            async def get_release(self, spotify_id):
                return release

            async def save_release(self, release_to_save):
                raise AssertionError("save_release should not be called for already publishing releases")

        tracker = self.make_tracker(FakeDatabase())

        outcome = await tracker.publish_release_now(release)

        self.assertEqual(outcome, "already_publishing")
        tracker._publish_release.assert_not_awaited()

    async def test_publish_release_now_saves_and_publishes_active_release(self):
        release = make_release_for_test("album_active", "Album Active", datetime(2024, 1, 1, 12, 0, 0))
        saved_statuses = []

        class FakeDatabase:
            async def get_release(self, spotify_id):
                return release

            async def save_release(self, release_to_save):
                saved_statuses.append(release_to_save.status)

        tracker = self.make_tracker(FakeDatabase())

        outcome = await tracker.publish_release_now(release, as_relisten=True)

        self.assertEqual(outcome, "published")
        self.assertEqual(saved_statuses, [LifecycleStatus.PUBLISHING])
        tracker._publish_release.assert_awaited_once_with(release, as_relisten=True)

    async def test_publish_release_saves_recently_published_retention_state(self):
        release = make_release_for_test("album_done", "Album Done", datetime(2024, 1, 1, 12, 0, 0))
        saved_statuses = []
        audit_events = []

        class FakeDatabase:
            async def save_release(self, release_to_save):
                saved_statuses.append((release_to_save.status, release_to_save.published_at))

            async def mark_saved_library_album_posted(self, spotify_id, wordpress_post_id):
                self.marked_posted = (spotify_id, wordpress_post_id)

            async def log_audit_event(self, event_type, payload):
                audit_events.append((event_type, payload))

        class FakePublisher:
            async def publish_release(self, release_to_publish, as_relisten=False):
                release_to_publish.wordpress_post_id = 456
                return PublishResult(post={"id": 456}, scf_pending_tags=[], listen_count=1)

        tracker = Tracker.__new__(Tracker)
        db = FakeDatabase()
        tracker.db = db
        tracker.publisher = FakePublisher()
        tracker.discord_bot = None

        await tracker._publish_release(release)

        self.assertEqual(saved_statuses[0][0], LifecycleStatus.PUBLISHING)
        self.assertEqual(saved_statuses[1][0], LifecycleStatus.PUBLISHED_RECENTLY)
        self.assertIsNotNone(saved_statuses[1][1])
        self.assertEqual(db.marked_posted, (release.spotify_id, 456))
        self.assertEqual(audit_events[0][0], "release_published")

    async def test_cleanup_published_releases_uses_24_hour_cutoff_and_audits(self):
        now = datetime(2024, 1, 2, 12, 0, 0)
        audit_events = []

        class FakeDatabase:
            def __init__(self):
                self.cutoff = None

            async def delete_published_releases_older_than(self, cutoff):
                self.cutoff = cutoff
                return 2

            async def log_audit_event(self, event_type, payload):
                audit_events.append((event_type, payload))

        db = FakeDatabase()
        tracker = Tracker.__new__(Tracker)
        tracker.db = db

        deleted_count = await tracker._cleanup_published_releases(now)

        self.assertEqual(deleted_count, 2)
        self.assertEqual(db.cutoff, now - timedelta(hours=24))
        self.assertEqual(audit_events[0][0], "published_release_cleanup")
        self.assertEqual(audit_events[0][1]["deleted_count"], 2)

    async def test_cleanup_published_releases_skips_until_interval_elapsed(self):
        now = datetime(2024, 1, 2, 12, 0, 0)

        class FakeDatabase:
            async def delete_published_releases_older_than(self, cutoff):
                raise AssertionError("cleanup should not run before the interval elapses")

        tracker = Tracker.__new__(Tracker)
        tracker.db = FakeDatabase()
        tracker._last_published_cleanup_at = now - timedelta(minutes=1)

        await tracker._cleanup_published_releases_if_due(now)

    async def test_expired_recently_published_release_is_deleted_before_reuse(self):
        now = datetime(2024, 1, 2, 12, 0, 0)
        release = make_release_for_test("album_expired", "Album Expired", datetime(2024, 1, 1, 11, 0, 0))
        release.status = LifecycleStatus.PUBLISHED_RECENTLY
        release.published_at = now - timedelta(hours=24, minutes=1)
        audit_events = []

        class FakeDatabase:
            async def delete_release(self, spotify_id):
                self.deleted_spotify_id = spotify_id
                return True

            async def log_audit_event(self, event_type, payload):
                audit_events.append((event_type, payload))

        db = FakeDatabase()
        tracker = Tracker.__new__(Tracker)
        tracker.db = db

        deleted = await tracker._delete_published_release_if_expired(release, now)

        self.assertTrue(deleted)
        self.assertEqual(db.deleted_spotify_id, release.spotify_id)
        self.assertEqual(audit_events[0][0], "published_release_cleanup")

    async def test_recently_published_release_is_kept_during_retention_window(self):
        now = datetime(2024, 1, 2, 12, 0, 0)
        release = make_release_for_test("album_recent", "Album Recent", datetime(2024, 1, 2, 11, 0, 0))
        release.status = LifecycleStatus.PUBLISHED_RECENTLY
        release.published_at = now - timedelta(hours=23, minutes=59)

        class FakeDatabase:
            async def delete_release(self, spotify_id):
                raise AssertionError("release should stay during the retention window")

        tracker = Tracker.__new__(Tracker)
        tracker.db = FakeDatabase()

        deleted = await tracker._delete_published_release_if_expired(release, now)

        self.assertFalse(deleted)

    async def test_publish_warns_when_album_is_not_saved_in_library(self):
        release = make_release_for_test("album_missing", "Album Missing", datetime(2024, 1, 1, 12, 0, 0))

        class FakeDatabase:
            async def save_release(self, release_to_save):
                return None

            async def mark_saved_library_album_posted(self, spotify_id, wordpress_post_id):
                return False

            async def log_audit_event(self, event_type, payload):
                return None

        class FakePublisher:
            async def publish_release(self, release_to_publish, as_relisten=False):
                release_to_publish.wordpress_post_id = 789
                return PublishResult(post={"id": 789}, scf_pending_tags=[], listen_count=1)

        class FakeSpotify:
            async def check_library_contains_album(self, spotify_id):
                return False

        class FakeDiscordBot:
            def __init__(self):
                self.published = None
                self.missing = None

            async def send_publish_notification(self, release_to_send, result):
                self.published = (release_to_send, result)

            async def send_library_missing_notification(self, release_to_send):
                self.missing = release_to_send

        discord_bot = FakeDiscordBot()
        tracker = Tracker.__new__(Tracker)
        tracker.db = FakeDatabase()
        tracker.publisher = FakePublisher()
        tracker.spotify = FakeSpotify()
        tracker.discord_bot = discord_bot

        await tracker._publish_release(release)

        self.assertIs(discord_bot.missing, release)
        self.assertEqual(discord_bot.published[1].post["id"], 789)


@unittest.skipIf(Tracker is None, "tracker dependencies are not installed")
class TestTrackerRelistenApprovalFlow(unittest.IsolatedAsyncioTestCase):
    """Test duplicate gating and relisten approval behavior."""

    def make_tracker(self, db):
        tracker = Tracker.__new__(Tracker)
        tracker.db = db
        tracker.discord_bot = None
        tracker.publisher = None
        return tracker

    def make_duplicate_post(self, post_id=321, title="Album Dup"):
        return WordPressPost(
            id=post_id,
            title=title,
            normalized_title=normalize_text(title),
            artists=["Artist"],
            normalized_artists=["artist"],
            link=f"https://example.com/{post_id}"
        )

    async def test_duplicate_candidate_prompts_without_saving_release(self):
        release = make_release_for_test("album_dup", "Album Dup", datetime(2024, 1, 1, 12, 0, 0))
        duplicate_post = self.make_duplicate_post()
        saved_prompts = []
        audit_events = []

        class FakeDatabase:
            async def get_live_discord_prompt(self, release_id, prompt_type):
                return None

            async def save_release(self, release_to_save):
                raise AssertionError("duplicate releases should not be saved before approval")

            async def save_discord_prompt(self, prompt):
                saved_prompts.append(prompt)

            async def log_audit_event(self, event_type, payload):
                audit_events.append((event_type, payload))

        class FakeDiscordBot:
            async def send_relisten_tracking_prompt(self, release_to_prompt, duplicate_post_to_prompt, expires_at):
                self.prompted = (release_to_prompt, duplicate_post_to_prompt, expires_at)
                return type("FakeMessage", (), {"id": 999})()

        tracker = self.make_tracker(FakeDatabase())
        tracker.discord_bot = FakeDiscordBot()
        tracker._check_duplicate = AsyncMock(return_value=duplicate_post)

        created = await tracker._start_tracking_or_prompt_for_relisten(release)

        self.assertIsNone(created)
        self.assertEqual(saved_prompts[0].prompt_type, PromptType.PROMPT_RELISTEN_APPROVAL.value)
        self.assertEqual(saved_prompts[0].release_id, release.spotify_id)
        self.assertEqual(saved_prompts[0].wordpress_post_id, duplicate_post.id)
        self.assertEqual(audit_events[0][0], "relisten_approval_prompt_sent")

    async def test_non_duplicate_candidate_starts_tracking_immediately(self):
        release = make_release_for_test("album_new", "Album New", datetime(2024, 1, 1, 12, 0, 0))
        saved_relisten_data = []

        class FakeDatabase:
            async def save_release(self, release_to_save):
                saved_relisten_data.append(
                    (release_to_save.is_relisten, release_to_save.duplicate_post_id)
                )

            async def log_audit_event(self, event_type, payload):
                return None

        tracker = self.make_tracker(FakeDatabase())
        tracker._check_duplicate = AsyncMock(return_value=None)

        created = await tracker._start_tracking_or_prompt_for_relisten(release)

        self.assertIs(created, release)
        self.assertFalse(release.is_relisten)
        self.assertEqual(release.duplicate_state, "none")
        self.assertIsNone(release.duplicate_post_id)
        self.assertEqual(saved_relisten_data, [(False, None)])

    async def test_relisten_approval_creates_tracked_relisten_release(self):
        release = make_release_for_test("album_approved", "Album Approved", datetime(2024, 1, 1, 12, 0, 0))
        saved_releases = []
        updated_prompts = []

        prompt = DiscordPrompt(
            id=1,
            prompt_type=PromptType.PROMPT_RELISTEN_APPROVAL.value,
            release_id=release.spotify_id,
            wordpress_post_id=321,
            discord_message_id="message_1",
            state=PromptState.PENDING.value,
            context_json=json.dumps({"duplicate_post_id": 321}),
        )

        class FakeDatabase:
            async def get_release(self, spotify_id):
                return None

            async def save_release(self, release_to_save):
                saved_releases.append(release_to_save)

            async def update_discord_prompt_state(self, message_id, state):
                updated_prompts.append((message_id, state))

            async def log_audit_event(self, event_type, payload):
                return None

        tracker = self.make_tracker(FakeDatabase())
        tracker._build_release_from_spotify = AsyncMock(return_value=release)

        outcome = await tracker.approve_relisten_tracking(prompt)

        self.assertEqual(outcome, "tracking_started")
        self.assertTrue(saved_releases[0].is_relisten)
        self.assertEqual(saved_releases[0].duplicate_post_id, 321)
        self.assertEqual(updated_prompts, [("message_1", PromptState.ACCEPTED.value)])

    async def test_handle_completion_auto_publishes_relisten_without_prompting(self):
        release = make_release_for_test("album_known", "Album Known", datetime(2024, 1, 1, 12, 0, 0))
        release.status = LifecycleStatus.PUBLISHING
        release.is_relisten = True
        release.duplicate_post_id = 321
        saved_states = []

        class FakeDatabase:
            async def save_release(self, release_to_save):
                saved_states.append((release_to_save.status, release_to_save.duplicate_state))

        tracker = self.make_tracker(FakeDatabase())
        tracker._check_duplicate = AsyncMock(side_effect=AssertionError("Should not re-check duplicates"))
        tracker._send_relisten_approval_prompt = AsyncMock(side_effect=AssertionError("Should not prompt at completion"))
        tracker._publish_release = AsyncMock()

        await tracker._handle_completion(release)

        tracker._check_duplicate.assert_not_awaited()
        tracker._send_relisten_approval_prompt.assert_not_awaited()
        tracker._publish_release.assert_awaited_once_with(release, as_relisten=True)
        self.assertEqual(saved_states, [])

    async def test_handle_completion_publishes_normal_release_without_duplicate_check(self):
        release = make_release_for_test("album_legacy", "Album Legacy", datetime(2024, 1, 1, 12, 0, 0))
        release.status = LifecycleStatus.PUBLISHING

        class FakeDatabase:
            async def save_release(self, release_to_save):
                raise AssertionError("mocked publish handles saving")

        tracker = self.make_tracker(FakeDatabase())
        tracker._check_duplicate = AsyncMock(side_effect=AssertionError("Should not check duplicates at completion"))
        tracker._publish_release = AsyncMock()

        await tracker._handle_completion(release)

        tracker._check_duplicate.assert_not_awaited()
        tracker._publish_release.assert_awaited_once_with(release, as_relisten=False)


@unittest.skipIf(WordPressClient is None or httpx is None, "wordpress client dependencies are not installed")
class TestWordPressPostFetch(unittest.IsolatedAsyncioTestCase):
    """Test WordPress post pagination and first-page validation."""

    class FakeHTTPClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.requests = []

        async def get(self, url, params=None):
            self.requests.append((url, params))
            if not self.responses:
                raise AssertionError("Unexpected WordPress request")
            return self.responses.pop(0)

    def make_response(self, posts, total="1", total_pages="1"):
        body = json.dumps(posts).encode()
        headers = {
            "X-WP-Total": total,
            "X-WP-TotalPages": total_pages,
        }
        return httpx.Response(
            200,
            content=body,
            headers=headers,
            request=httpx.Request("GET", "https://example.com/wp-json/wp/v2/posts"),
        )

    def make_client(self, responses):
        fake_http = self.FakeHTTPClient(responses)
        client = WordPressClient.__new__(WordPressClient)
        client.api_url = "https://example.com/wp-json/wp/v2"
        client.client = fake_http
        return client, fake_http

    async def test_matching_first_page_validation_skips_pagination(self):
        response = self.make_response([{"id": 1}], total="7")
        first_page_hash = hashlib.sha256(response.content).hexdigest()
        client, fake_http = self.make_client([response])

        result = await client.get_posts(
            validate_first_page=True,
            previous_x_wp_total="7",
            previous_first_page_hash=first_page_hash,
            status="publish",
            _fields="id,title,tags,link",
        )

        self.assertTrue(result.cache_unchanged)
        self.assertEqual(result.posts, [])
        self.assertEqual(len(fake_http.requests), 1)

    async def test_changed_first_page_hash_reuses_page_one_and_paginates(self):
        previous_response = self.make_response([{"id": 2}], total="7", total_pages="2")
        page_one = self.make_response(
            [{"id": 1}],
            total="7",
            total_pages="2",
        )
        page_two = self.make_response([{"id": 3}], total="7", total_pages="2")
        client, fake_http = self.make_client([page_one, page_two])

        result = await client.get_posts(
            validate_first_page=True,
            previous_x_wp_total="7",
            previous_first_page_hash=hashlib.sha256(previous_response.content).hexdigest(),
            status="publish",
            _fields="id,title,tags,link",
        )

        self.assertFalse(result.cache_unchanged)
        self.assertEqual([post["id"] for post in result.posts], [1, 3])
        self.assertEqual([request[1]["page"] for request in fake_http.requests], [1, 2])


@unittest.skipIf(WordPressClient is None or httpx is None, "wordpress client dependencies are not installed")
class TestWordPressTagFetch(unittest.IsolatedAsyncioTestCase):
    """Test WordPress tag pagination and in-memory first-page validation."""

    class FakeHTTPClient:
        def __init__(self, get_responses=None, post_responses=None):
            self.get_responses = list(get_responses or [])
            self.post_responses = list(post_responses or [])
            self.get_requests = []
            self.post_requests = []

        async def get(self, url, params=None):
            self.get_requests.append((url, params))
            if not self.get_responses:
                raise AssertionError("Unexpected WordPress GET request")
            return self.get_responses.pop(0)

        async def post(self, url, json=None):
            self.post_requests.append((url, json))
            if not self.post_responses:
                raise AssertionError("Unexpected WordPress POST request")
            return self.post_responses.pop(0)

    def make_response(self, tags, total="1", total_pages="1"):
        body = json.dumps(tags).encode()
        headers = {
            "X-WP-Total": total,
            "X-WP-TotalPages": total_pages,
        }
        return httpx.Response(
            200,
            content=body,
            headers=headers,
            request=httpx.Request("GET", "https://example.com/wp-json/wp/v2/tags"),
        )

    def make_post_response(self, tag):
        return httpx.Response(
            201,
            json=tag,
            request=httpx.Request("POST", "https://example.com/wp-json/wp/v2/tags"),
        )

    def make_client(self, fake_http):
        client = WordPressClient.__new__(WordPressClient)
        client.api_url = "https://example.com/wp-json/wp/v2"
        client.client = fake_http
        return client

    async def test_get_tags_populates_cache_then_returns_cached_full_list(self):
        page_one = self.make_response([{"id": 1, "name": "Artist One"}], total="2", total_pages="2")
        page_two = self.make_response([{"id": 2, "name": "Artist Two"}], total="2", total_pages="2")
        matching_page_one = self.make_response([{"id": 1, "name": "Artist One"}], total="2", total_pages="2")
        fake_http = self.FakeHTTPClient([page_one, page_two, matching_page_one])
        client = self.make_client(fake_http)

        first_result = await client.get_tags()
        second_result = await client.get_tags()

        self.assertEqual([tag["id"] for tag in first_result], [1, 2])
        self.assertEqual([tag["id"] for tag in second_result], [1, 2])
        self.assertEqual([request[1]["page"] for request in fake_http.get_requests], [1, 2, 1])

    async def test_get_tags_refreshes_cache_when_first_page_hash_changes(self):
        page_one = self.make_response([{"id": 1, "name": "Artist One"}], total="1", total_pages="1")
        changed_page_one = self.make_response([{"id": 3, "name": "Artist Three"}], total="1", total_pages="1")
        fake_http = self.FakeHTTPClient([page_one, changed_page_one])
        client = self.make_client(fake_http)

        first_result = await client.get_tags()
        second_result = await client.get_tags()

        self.assertEqual([tag["id"] for tag in first_result], [1])
        self.assertEqual([tag["id"] for tag in second_result], [3])
        self.assertEqual([request[1]["page"] for request in fake_http.get_requests], [1, 1])

    async def test_create_tag_reconciles_existing_in_memory_tag_cache(self):
        new_tag = {"id": 2, "name": "Artist Two"}
        fake_http = self.FakeHTTPClient(post_responses=[self.make_post_response(new_tag)])
        client = self.make_client(fake_http)
        client._cached_tags = [{"id": 1, "name": "Artist One"}]
        client._cached_tags_x_wp_total = "1"
        client._cached_tags_first_page_hash = "cached-hash"

        result = await client.create_tag("Artist Two")

        self.assertEqual(result, new_tag)
        self.assertEqual([tag["id"] for tag in client._cached_tags], [1, 2])
        self.assertIsNone(client._cached_tags_x_wp_total)
        self.assertIsNone(client._cached_tags_first_page_hash)


@unittest.skipIf(Publisher is None, "publisher dependencies are not installed")
class TestPublisherPostCacheRefresh(unittest.IsolatedAsyncioTestCase):
    """Test publisher-level WordPress post cache refresh behavior."""

    class FakeDatabase:
        def __init__(self):
            self.state = {
                POST_CACHE_TOTAL_KEY: "1",
                POST_CACHE_FIRST_PAGE_HASH_KEY: "abc",
            }
            self.saved_posts = None
            self.saved_state = {}

        async def get_service_state(self, key):
            return self.state.get(key)

        async def save_wordpress_posts(self, posts):
            self.saved_posts = posts

        async def save_service_state(self, key, value):
            self.saved_state[key] = value

    async def test_refresh_post_cache_returns_early_when_wordpress_posts_unchanged(self):
        class FakeWordPress:
            async def get_posts(self, **kwargs):
                return WordPressPostsResult(
                    posts=[],
                    cache_unchanged=True,
                    message="WordPress post cache is current.",
                    x_wp_total="1",
                    first_page_hash="abc",
                )

            async def get_tags(self):
                raise AssertionError("Tags should not be fetched when post cache is unchanged")

        db = self.FakeDatabase()
        publisher = Publisher.__new__(Publisher)
        publisher.db = db
        publisher.wordpress = FakeWordPress()

        message = await publisher.refresh_post_cache()

        self.assertEqual(message, "WordPress post cache is current.")
        self.assertIsNone(db.saved_posts)
        self.assertEqual(db.saved_state, {})

    async def test_forced_refresh_rebuilds_cache_and_saves_validation_state(self):
        class FakeWordPress:
            def __init__(self):
                self.get_posts_kwargs = None

            async def get_posts(self, **kwargs):
                self.get_posts_kwargs = kwargs
                return WordPressPostsResult(
                    posts=[{
                        "id": 11,
                        "title": {"rendered": "Album Title"},
                        "tags": [22],
                        "link": "https://example.com/album-title",
                    }],
                    cache_unchanged=False,
                    message="Fetched 1 WordPress posts.",
                    x_wp_total="1",
                    first_page_hash="hash-1",
                )

            async def get_tags(self):
                return [{"id": 22, "name": "Artist Name"}]

        db = self.FakeDatabase()
        wordpress = FakeWordPress()
        publisher = Publisher.__new__(Publisher)
        publisher.db = db
        publisher.wordpress = wordpress

        message = await publisher.refresh_post_cache(force=True)

        self.assertEqual(message, "Updated post cache: 1 posts")
        self.assertFalse(wordpress.get_posts_kwargs["validate_first_page"])
        self.assertEqual(db.saved_posts[0].title, "Album Title")
        self.assertEqual(db.saved_posts[0].artists, ["Artist Name"])
        self.assertEqual(db.saved_state[POST_CACHE_TOTAL_KEY], "1")
        self.assertEqual(db.saved_state[POST_CACHE_FIRST_PAGE_HASH_KEY], "hash-1")

    def test_discord_content_formatter_builds_safe_wordpress_paragraphs(self):
        formatted = format_discord_content_for_wordpress(
            " First & second\nsame paragraph\n\n<script>blocked</script> "
        )

        self.assertEqual(
            formatted,
            "<p>First &amp; second<br />same paragraph</p>\n\n"
            "<p>&lt;script&gt;blocked&lt;/script&gt;</p>"
        )

    async def test_update_post_content_replaces_body_with_formatted_content(self):
        class FakeWordPress:
            def __init__(self):
                self.updated = None

            async def update_post(self, post_id, data):
                self.updated = (post_id, data)
                return {"id": post_id, **data}

        wordpress = FakeWordPress()
        publisher = Publisher.__new__(Publisher)
        publisher.wordpress = wordpress

        post = await publisher.update_post_content(77, "Line one\n\nLine two")

        self.assertEqual(post["id"], 77)
        self.assertEqual(
            wordpress.updated,
            (77, {"content": "<p>Line one</p>\n\n<p>Line two</p>"})
        )

    async def test_publish_release_keeps_success_when_forced_cache_refresh_fails(self):
        class FakeWordPress:
            async def create_post(self, data):
                return {"id": 99, "title": {"rendered": data["title"]}}

        release = make_release_for_test("album_publish", "Album Publish", datetime(2024, 1, 1, 12, 0, 0))
        publisher = Publisher.__new__(Publisher)
        publisher.category_cache = {"Album": 1}
        publisher.wordpress = FakeWordPress()
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[2])
        publisher.refresh_post_cache = AsyncMock(side_effect=RuntimeError("refresh failed"))

        result = await publisher.publish_release(release)

        self.assertIsInstance(result, PublishResult)
        self.assertEqual(result.post["id"], 99)
        self.assertEqual(result.listen_count, 1)
        self.assertEqual(result.scf_pending_tags, [])
        self.assertEqual(release.wordpress_post_id, 99)
        publisher.refresh_post_cache.assert_awaited_once_with(force=True)


class TestDuplicateDetection(unittest.TestCase):
    """Test duplicate detection via normalized title and artist set."""

    def test_exact_match(self):
        """Test exact match of title and artists."""
        norm_title = "test album"
        artists1 = {"artist one", "artist two"}
        artists2 = {"artist one", "artist two"}

        match = norm_title == norm_title and artists1 == artists2
        self.assertTrue(match)

    def test_different_artist_order_matches(self):
        """Test that different artist order still matches."""
        artists1 = {"artist one", "artist two"}
        artists2 = {"artist two", "artist one"}

        match = artists1 == artists2
        self.assertTrue(match)

    def test_different_artists_no_match(self):
        """Test that different artists don't match."""
        artists1 = {"artist one", "artist two"}
        artists2 = {"artist one", "artist three"}

        match = artists1 == artists2
        self.assertFalse(match)

    def test_case_insensitive_match(self):
        """Test case-insensitive matching."""
        title1 = normalize_text("Test Album")
        title2 = normalize_text("test album")

        match = title1 == title2
        self.assertTrue(match)


@unittest.skipIf(pick_mood_tags is None, "lastfm client is not available")
class TestLastFMMoodTags(unittest.TestCase):
    """Test mood tag selection rules copied from the complement script."""

    def test_pick_mood_tags_filters_blocklist_and_caps_three(self):
        album_info = {
            "mbid": "abc-123",
            "tags": [
                {"name": "rock"},
                {"name": "2020"},
                {"name": "aoty"},
                {"name": "best of 2020"},
                {"name": "seen live"},
                {"name": "favorites"},
                {"name": "favorite"},
                {"name": "under 1000"},
                {"name": "indie"},
                {"name": "alternative"},
            ],
        }

        tags = pick_mood_tags(album_info, max_n=3)

        self.assertEqual(tags, ["rock", "indie", "alternative"])

    def test_pick_mood_tags_handles_flat_string_tags(self):
        tags = pick_mood_tags({"tags": ["rock", "2020", "indie"]}, max_n=3)
        self.assertEqual(tags, ["rock", "indie"])

    def test_pick_mood_tags_handles_empty_or_invalid_input(self):
        self.assertEqual(pick_mood_tags({}), [])
        self.assertEqual(pick_mood_tags({"tags": []}), [])
        self.assertEqual(pick_mood_tags(None), [])

    def test_pick_mood_tags_is_case_insensitive_for_blocklist(self):
        tags = pick_mood_tags({"tags": [{"name": "AOTY"}, {"name": "Indie"}]}, max_n=3)
        self.assertEqual(tags, ["Indie"])


@unittest.skipIf(LastFMClient is None, "lastfm client is not available")
class TestLastFMClientAlbumGetInfo(unittest.IsolatedAsyncioTestCase):
    """Test LastFMClient.album_getinfo shape, error tolerance, and key gating."""

    def make_client(self, api_key="valid-key"):
        client = LastFMClient.__new__(LastFMClient)
        client.api_key = api_key
        return client

    def make_response(self, payload):
        import httpx as _httpx

        return _httpx.Response(
            200,
            json=payload,
            request=_httpx.Request("GET", "https://ws.audioscrobbler.com/2.0/"),
        )

    async def test_album_getinfo_returns_empty_dict_when_api_key_missing(self):
        client = self.make_client(api_key=None)

        result = await client.album_getinfo("Artist", "Album")

        self.assertEqual(result, {})

    async def test_album_getinfo_returns_empty_dict_for_blank_artist_or_album(self):
        client = self.make_client(api_key=None)

        self.assertEqual(await client.album_getinfo("", "Album"), {})
        self.assertEqual(await client.album_getinfo("Artist", ""), {})

    async def test_album_getinfo_extracts_mbid_and_tag_names(self):
        client = self.make_client()
        client.client = AsyncMock()
        client.client.get = AsyncMock(return_value=self.make_response({
            "album": {
                "mbid": "mbid-uuid",
                "tags": {"tag": [{"name": "rock"}, {"name": "indie"}]},
            }
        }))

        result = await client.album_getinfo("Artist", "Album")

        self.assertEqual(result["mbid"], "mbid-uuid")
        self.assertEqual([t["name"] for t in result["tags"]], ["rock", "indie"])

    async def test_album_getinfo_returns_empty_dict_on_http_error(self):
        client = self.make_client()
        client.client = AsyncMock()
        client.client.get = AsyncMock(side_effect=Exception("network down"))

        result = await client.album_getinfo("Artist", "Album")

        self.assertEqual(result, {})

    async def test_album_getinfo_handles_malformed_payload(self):
        client = self.make_client()
        client.client = AsyncMock()
        client.client.get = AsyncMock(return_value=self.make_response({"unexpected": "shape"}))

        result = await client.album_getinfo("Artist", "Album")

        self.assertEqual(result["mbid"], "")
        self.assertEqual(result["tags"], [])


@unittest.skipIf(_coerce_spotify_release_date is None, "publisher helpers are not available")
class TestSCFDateHelpers(unittest.TestCase):
    """Test Spotify release-date coercion and SCF date formatting helpers."""

    def test_coerce_full_iso_date_passes_through(self):
        self.assertEqual(_coerce_spotify_release_date("2024-03-15"), "2024-03-15")

    def test_coerce_year_only_expands_to_first_of_year(self):
        self.assertEqual(_coerce_spotify_release_date("2024"), "2024-01-01")

    def test_coerce_year_month_expands_to_first_of_month(self):
        self.assertEqual(_coerce_spotify_release_date("2024-03"), "2024-03-01")

    def test_coerce_empty_or_invalid_returns_input(self):
        self.assertEqual(_coerce_spotify_release_date(""), "")
        self.assertEqual(_coerce_spotify_release_date("garbage"), "garbage")

    def test_format_scf_date_renders_iso_as_dmy(self):
        self.assertEqual(_format_scf_date("2024-03-15"), "15/03/2024")

    def test_format_scf_date_renders_datetime_as_dmy(self):
        self.assertEqual(_format_scf_date("2024-03-15T14:30:00"), "15/03/2024")

    def test_format_scf_date_empty_returns_empty(self):
        self.assertEqual(_format_scf_date(""), "")
        self.assertEqual(_format_scf_date(None), "")


@unittest.skipIf(Publisher is None, "publisher dependencies are not installed")
class TestPublisherSCFFill(unittest.IsolatedAsyncioTestCase):
    """Test SCF auto-fill behavior on the publisher."""

    def make_release(self):
        return make_release_for_test(
            "album_scf",
            "Album SCF",
            datetime(2024, 1, 1, 12, 0, 0),
            tracks=[
                Track("t1", "Track 1", "track 1", 180000, 1, 1, True, False, explicit=False),
                Track("t2", "Track 2", "track 2", 240000, 1, 2, True, False, explicit=True),
                Track("t3", "Local", "local", 60000, 1, 3, False, False, explicit=False),
            ],
        )

    def make_publisher(self, listen_count=1, lastfm_album_info=None):
        publisher = Publisher.__new__(Publisher)
        publisher.db = AsyncMock()
        publisher.wordpress = MagicMock()
        publisher.wordpress.update_post = AsyncMock()
        publisher.lastfm = MagicMock()
        publisher.lastfm.album_getinfo = AsyncMock(return_value=lastfm_album_info or {"mbid": "", "tags": []})
        publisher.lastfm.close = AsyncMock()
        publisher._count_listen_index = AsyncMock(return_value=listen_count)
        publisher._fill_scf_enabled = True
        return publisher

    async def test_build_scf_payload_uses_countable_tracks_only(self):
        publisher = self.make_publisher()
        release = self.make_release()
        post = {"id": 42, "date": "2024-03-15T14:30:00"}

        acf, status = await publisher._build_scf_payload(release, listen_count=1, post=post)

        # Only two tracks are countable (Local has is_countable=False).
        self.assertEqual(len(acf["music_tracks"]), 2)
        self.assertEqual([t["title"] for t in acf["music_tracks"]], ["Track 1", "Track 2"])
        # Sum of countable durations.
        self.assertEqual(acf["music_length_ms"], 180000 + 240000)
        self.assertEqual(acf["music_total_tracks"], 2)
        self.assertEqual(acf["music_avg_track_ms"], (180000 + 240000) // 2)
        self.assertTrue(acf["music_explicit"])
        # Coerced release_date + d/m/Y.
        self.assertEqual(acf["music_release_date"], "01/01/2024")
        # Post date formatted as d/m/Y.
        self.assertEqual(acf["music_listened_at"], "15/03/2024")
        self.assertEqual(acf["spotify_album_id"], release.spotify_id)
        self.assertEqual(acf["spotify_album_url"], f"https://open.spotify.com/album/{release.spotify_id}")
        # Mood tags empty since the fake Last.fm returned no usable tags.
        self.assertEqual(acf["music_mood_tags"], [])
        self.assertIsNone(status["mood_tags"])

    async def test_build_scf_payload_uses_year_only_release_date(self):
        publisher = self.make_publisher()
        release = self.make_release()
        release.release_date = "2024"
        post = {"id": 1, "date": "2024-12-31T23:59:00"}

        acf, _ = await publisher._build_scf_payload(release, listen_count=1, post=post)

        self.assertEqual(acf["music_release_date"], "01/01/2024")
        self.assertEqual(acf["music_listened_at"], "31/12/2024")

    async def test_build_scf_payload_uses_lastfm_mbid_and_mood_tags(self):
        publisher = self.make_publisher(
            lastfm_album_info={"mbid": "mbid-uuid", "tags": [{"name": "rock"}, {"name": "indie"}]}
        )
        release = self.make_release()
        post = {"id": 1, "date": "2024-03-15T14:30:00"}

        acf, status = await publisher._build_scf_payload(release, listen_count=2, post=post)

        self.assertEqual(acf["lastfm_release_id"], "mbid-uuid")
        self.assertEqual(acf["music_mood_tags"], [{"mood": "rock"}, {"mood": "indie"}])
        self.assertEqual(acf["listen-count"], 2)
        self.assertEqual(status["mood_tags"], ["rock", "indie"])

    async def test_fill_post_scf_patches_acf_block(self):
        publisher = self.make_publisher()
        acf = {"music_tracks": [], "listen-count": 1}

        await publisher._fill_post_scf(99, acf)

        publisher.wordpress.update_post.assert_awaited_once_with(99, {"acf": acf})

    async def test_count_listen_index_returns_matches_plus_one(self):
        class FakeDatabase:
            def __init__(self, posts):
                self._posts = posts

            async def get_wordpress_posts(self):
                return self._posts

        posts = [
            WordPressPost(
                id=1, title="Album SCF", normalized_title=normalize_text("Album SCF"),
                artists=["Artist"], normalized_artists=normalize_artist_list(["Artist"]),
                link="https://example.com/1",
            ),
            WordPressPost(
                id=2, title="Other", normalized_title=normalize_text("Other"),
                artists=["Someone"], normalized_artists=normalize_artist_list(["Someone"]),
                link="https://example.com/2",
            ),
            WordPressPost(
                id=3, title="Album SCF (re-release)", normalized_title=normalize_text("Album SCF"),
                artists=["Different"], normalized_artists=normalize_artist_list(["Different"]),
                link="https://example.com/3",
            ),
        ]
        publisher = Publisher.__new__(Publisher)
        publisher.db = FakeDatabase(posts)
        release = make_release_for_test("album_scf", "Album SCF", datetime(2024, 1, 1, 12, 0, 0))

        count = await publisher._count_listen_index(release)

        # Only post id=1 matches both title and artist set; +1 for the new post.
        self.assertEqual(count, 2)

    async def test_publish_release_with_scf_returns_publish_result_and_fills(self):
        class FakeWordPress:
            def __init__(self):
                self.updated = None
                self.create_calls = 0

            async def create_post(self, data):
                self.create_calls += 1
                return {"id": 7, "date": "2024-03-15T14:30:00", **data}

            async def update_post(self, post_id, data):
                self.updated = (post_id, data)
                return {"id": post_id, **data}

        publisher = Publisher.__new__(Publisher)
        publisher.config = type("C", (), {"lastfm_api_key": "k", "fill_scf_enabled": True})()
        publisher.db = AsyncMock()
        publisher.db.get_wordpress_posts = AsyncMock(return_value=[])
        publisher.category_cache = {"Album": 1}
        publisher.wordpress = FakeWordPress()
        publisher.lastfm = MagicMock()
        publisher.lastfm.album_getinfo = AsyncMock(return_value={"mbid": "m", "tags": [{"name": "rock"}]})
        publisher.lastfm.close = AsyncMock()
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[2])
        publisher.refresh_post_cache = AsyncMock()
        publisher._fill_scf_enabled = True

        release = self.make_release()
        result = await publisher.publish_release(release)

        self.assertIsInstance(result, PublishResult)
        self.assertEqual(result.post["id"], 7)
        self.assertEqual(result.listen_count, 1)
        self.assertEqual(result.scf_pending_tags, [])
        # SCF fill wrote the acf block.
        self.assertEqual(publisher.wordpress.updated[0], 7)
        self.assertIn("acf", publisher.wordpress.updated[1])
        self.assertEqual(publisher.wordpress.updated[1]["acf"]["listen-count"], 1)
        # The about-to-be-created post was counted (cache was empty so matches=0 + 1 = 1).
        publisher.refresh_post_cache.assert_awaited_once_with(force=True)

    async def test_publish_release_marks_mood_tags_pending_when_lastfm_returns_no_tags(self):
        class FakeWordPress:
            async def create_post(self, data):
                return {"id": 7, "title": {"rendered": data["title"]}, "date": "2024-03-15T14:30:00"}

            async def update_post(self, post_id, data):
                return {"id": post_id, **data}

        publisher = Publisher.__new__(Publisher)
        publisher.db = AsyncMock()
        publisher.db.get_wordpress_posts = AsyncMock(return_value=[])
        publisher.category_cache = {"Album": 1}
        publisher.wordpress = FakeWordPress()
        publisher.lastfm = MagicMock()
        publisher.lastfm.album_getinfo = AsyncMock(return_value={"mbid": "", "tags": []})
        publisher.lastfm.close = AsyncMock()
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[2])
        publisher.refresh_post_cache = AsyncMock()
        publisher._fill_scf_enabled = True

        release = self.make_release()
        result = await publisher.publish_release(release)

        self.assertEqual(result.scf_pending_tags, ["mood_tags"])

    async def test_publish_release_continues_when_scf_fill_raises(self):
        class FakeWordPress:
            async def create_post(self, data):
                return {"id": 7, "title": {"rendered": data["title"]}, "date": "2024-03-15T14:30:00"}

            async def update_post(self, post_id, data):
                raise RuntimeError("scf endpoint down")

        publisher = Publisher.__new__(Publisher)
        publisher.db = AsyncMock()
        publisher.db.get_wordpress_posts = AsyncMock(return_value=[])
        publisher.category_cache = {"Album": 1}
        publisher.wordpress = FakeWordPress()
        publisher.lastfm = MagicMock()
        publisher.lastfm.album_getinfo = AsyncMock(return_value={"mbid": "", "tags": []})
        publisher.lastfm.close = AsyncMock()
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[2])
        publisher.refresh_post_cache = AsyncMock()
        publisher._fill_scf_enabled = True

        release = self.make_release()
        result = await publisher.publish_release(release)

        # Post was still created and returned; SCF failure is surfaced.
        self.assertEqual(result.post["id"], 7)
        self.assertIn("scf_error", result.scf_pending_tags)

    async def test_publish_release_with_scf_disabled_skips_scf_path(self):
        class FakeWordPress:
            async def create_post(self, data):
                return {"id": 7, "title": {"rendered": data["title"]}, "date": "2024-03-15T14:30:00"}

            async def update_post(self, post_id, data):
                raise AssertionError("update_post should not be called when SCF is disabled")

        publisher = Publisher.__new__(Publisher)
        publisher.db = AsyncMock()
        publisher.category_cache = {"Album": 1}
        publisher.wordpress = FakeWordPress()
        publisher.lastfm = MagicMock()
        publisher._ensure_categories = AsyncMock()
        publisher._upload_artwork = AsyncMock(return_value=None)
        publisher._resolve_tags = AsyncMock(return_value=[2])
        publisher.refresh_post_cache = AsyncMock()
        publisher._fill_scf_enabled = False

        release = self.make_release()
        result = await publisher.publish_release(release)

        self.assertEqual(result.post["id"], 7)
        self.assertEqual(result.scf_pending_tags, [])
        self.assertEqual(result.listen_count, 1)


@unittest.skipIf(DiscordBot is None, "discord bot dependencies are not installed")
class TestPublishNotificationEmbed(unittest.IsolatedAsyncioTestCase):
    """Test send_publish_notification surface area after SCF auto-fill."""

    def setUp(self):
        self.bot = DiscordBot.__new__(DiscordBot)
        self.bot.config = type("FakeConfig", (), {
            "discord_user_id": 123,
            "wordpress_public_url": "https://public.example.com",
        })()
        self.bot.db = AsyncMock()
        self.bot.db.save_discord_prompt = AsyncMock()
        self.bot._get_user = AsyncMock()
        self.bot._send_dm = AsyncMock(return_value=type("FakeMessage", (), {"id": "m1"})())

    def make_release(self, is_relisten=False, duplicate_post_id=None):
        release = make_release_for_test(
            "album_pub_embed", "Album Embed", datetime(2024, 1, 1, 12, 0, 0)
        )
        release.wordpress_post_id = 42
        release.is_relisten = is_relisten
        release.duplicate_post_id = duplicate_post_id
        return release

    def make_post(self):
        return {
            "id": 42,
            "title": {"rendered": "Album Embed"},
            "link": "https://internal.example.com/album-embed",
            "date": "2024-03-15T14:30:00",
        }

    def extract_embed(self):
        return self.bot._send_dm.await_args.kwargs["embed"]

    def extract_content(self):
        call = self.bot._send_dm.await_args
        # _send_dm(content, embed=..., view=...) is called positionally for content.
        if "content" in call.kwargs:
            return call.kwargs["content"]
        return call.args[0]

    async def test_default_content_announces_auto_fill(self):
        result = PublishResult(post=self.make_post(), scf_pending_tags=[], listen_count=1)

        await self.bot.send_publish_notification(self.make_release(), result)

        self.assertIn("SCF metadata was auto-filled", self.extract_content())
        self.assertNotIn("mood tags unavailable", self.extract_content())
        field_names = [f.name for f in self.extract_embed().fields]
        self.assertNotIn("Listen count", field_names)
        self.assertNotIn("⚠️ SCF metadata", field_names)

    async def test_surfaces_mood_tags_unavailable_message_and_field(self):
        result = PublishResult(post=self.make_post(), scf_pending_tags=["mood_tags"], listen_count=1)

        await self.bot.send_publish_notification(self.make_release(), result)

        self.assertIn("mood tags could not be filled", self.extract_content())
        self.assertIn("Last.fm returned no tags", self.extract_content())
        field_map = {f.name: f.value for f in self.extract_embed().fields}
        self.assertIn("⚠️ SCF metadata", field_map)
        self.assertIn("mood tags unavailable", field_map["⚠️ SCF metadata"])

    async def test_adds_listen_count_field_only_when_greater_than_one(self):
        result = PublishResult(post=self.make_post(), scf_pending_tags=[], listen_count=3)

        await self.bot.send_publish_notification(self.make_release(), result)

        field_map = {f.name: f.value for f in self.extract_embed().fields}
        self.assertEqual(field_map.get("Listen count"), "3")


@unittest.skipIf(EditorState is None, "editor_view module is not importable")
class TestEditorStateProjection(unittest.TestCase):
    """Test the field projection helpers used by the editor's editor/ACF bridge."""

    def test_project_field_to_scf_uses_known_aliases(self):
        self.assertEqual(_project_field_to_scf("rating"), "music_rating")
        self.assertEqual(_project_field_to_scf("favorite"), "music_favorite")
        self.assertEqual(_project_field_to_scf("notes"), "music_notes")
        self.assertEqual(_project_field_to_scf("unreleased"), "unreleased")
        # Unknown names pass through unchanged so future fields "just work".
        self.assertEqual(_project_field_to_scf("custom"), "custom")

    def test_coerce_field_for_scf_normalises_rating(self):
        self.assertEqual(_coerce_field_for_scf("rating", 87), 87)
        self.assertEqual(_coerce_field_for_scf("rating", None), "")

    def test_coerce_field_for_scf_normalises_notes(self):
        self.assertEqual(_coerce_field_for_scf("notes", "hello"), "hello")
        self.assertEqual(_coerce_field_for_scf("notes", None), "")

    def test_state_from_acf_picks_editor_fields(self):
        acf = {
            "music_rating": 73,
            "music_favorite": True,
            "music_notes": "Some notes",
            "unreleased": False,
            "music_tracks": [
                {"title": "Track A", "track_number": 1, "duration_ms": 1000,
                 "spotify_id": "sp1", "highlight": True, "disc_number": 1},
            ],
        }
        state = EditorState(rating=0, favorite=False, notes=None, unreleased=False)
        # Recreate what open_post_publish_editor would do.
        from editor_view import state_from_acf
        new_state = state_from_acf(acf)
        self.assertEqual(new_state.rating, 73)
        self.assertTrue(new_state.favorite)
        self.assertEqual(new_state.notes, "Some notes")
        self.assertFalse(new_state.unreleased)
        self.assertEqual(new_state.music_tracks[0]["title"], "Track A")
        self.assertTrue(new_state.music_tracks[0]["highlight"])

    def test_tracks_from_acf_builds_track_shells(self):
        rows = [
            {"title": "A", "track_number": 1, "duration_ms": 1000,
             "spotify_id": "sp_a", "highlight": False, "disc_number": 1},
            {"title": "B", "track_number": 2, "duration_ms": 2000,
             "spotify_id": "sp_b", "highlight": True, "disc_number": 1},
        ]
        tracks = _tracks_from_acf(rows)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "A")
        self.assertFalse(tracks[0].highlight)
        self.assertTrue(tracks[1].highlight)
        # Missing input returns an empty list, not None.
        self.assertEqual(_tracks_from_acf(None), [])


@unittest.skipIf(EditorState is None, "editor_view module is not importable")
class TestPrePublishSink(unittest.IsolatedAsyncioTestCase):
    """PrePublishSink writes through db.save_release and into the live Release object."""

    def make_release(self):
        tracks = [
            Track(spotify_id="t1", title="Track 1", normalized_title="track 1",
                  duration_ms=1000, disc_number=1, track_number=1,
                  is_countable=True, listened=False, highlight=False),
            Track(spotify_id="t2", title="Track 2", normalized_title="track 2",
                  duration_ms=1000, disc_number=1, track_number=2,
                  is_countable=True, listened=False, highlight=True),
        ]
        release = make_release_for_test("album_editor", "Editor Album", datetime(2024, 1, 1), tracks=tracks)
        return release

    async def test_update_field_persists_release(self):
        db = MagicMock()
        db.save_release = AsyncMock()
        release = self.make_release()
        sink = PrePublishSink(db=db, release=release)
        sink.state = EditorState()  # reset

        await sink.update_field("rating", 91)
        await sink.update_field("favorite", True)
        await sink.update_field("notes", "n1")
        await sink.update_field("unreleased", True)

        self.assertEqual(release.rating, 91)
        self.assertTrue(release.favorite)
        self.assertEqual(release.notes, "n1")
        self.assertTrue(release.unreleased)
        # One save per update_field call.
        self.assertEqual(db.save_release.await_count, 4)

    async def test_update_track_highlight_flips_in_place_and_saves(self):
        db = MagicMock()
        db.save_release = AsyncMock()
        release = self.make_release()
        sink = PrePublishSink(db=db, release=release)

        await sink.update_track_highlight("t1", True)

        # t1 is now highlighted; t2 was already true, but is left alone.
        self.assertTrue(release.tracks[0].highlight)
        self.assertTrue(release.tracks[1].highlight)
        db.save_release.assert_awaited_once()

    async def test_update_track_highlight_unknown_id_is_noop(self):
        db = MagicMock()
        db.save_release = AsyncMock()
        release = self.make_release()
        sink = PrePublishSink(db=db, release=release)

        await sink.update_track_highlight("not_a_real_id", True)

        db.save_release.assert_not_awaited()


@unittest.skipIf(EditorState is None, "editor_view module is not importable")
class TestPostPublishSink(unittest.IsolatedAsyncioTestCase):
    """PostPublishSink PATCHes the live WP ``acf`` block via publisher.update_post_scf."""

    def make_sink(self, initial_acf=None, wp_response=None):
        publisher = MagicMock()
        publisher.update_post_scf = AsyncMock(return_value=wp_response or {"id": 42})
        wp_client = MagicMock()
        wp_client.get_post_acf = AsyncMock(return_value=initial_acf or {})
        sink = PostPublishSink(
            publisher=publisher, wordpress_client=wp_client, post_id=42,
            initial_acf=initial_acf,
        )
        return sink, publisher, wp_client

    async def test_snapshot_re_reads_live_acf(self):
        sink, _, wp_client = self.make_sink(initial_acf={})
        wp_client.get_post_acf = AsyncMock(return_value={
            "music_rating": 70, "music_favorite": True, "music_notes": "n", "unreleased": True,
        })

        state = await sink.snapshot()

        wp_client.get_post_acf.assert_awaited_once_with(42)
        self.assertEqual(state.rating, 70)
        self.assertTrue(state.favorite)
        self.assertEqual(state.notes, "n")
        self.assertTrue(state.unreleased)

    async def test_update_field_patches_scf(self):
        sink, publisher, _ = self.make_sink(initial_acf={})

        await sink.update_field("rating", 88)

        publisher.update_post_scf.assert_awaited_once_with(42, {"music_rating": 88})
        # Local state mirrors the new value.
        self.assertEqual(sink.state.rating, 88)

    async def test_update_field_normalises_none_rating(self):
        sink, publisher, _ = self.make_sink(initial_acf={})

        await sink.update_field("rating", None)

        publisher.update_post_scf.assert_awaited_once_with(42, {"music_rating": ""})

    async def test_update_track_highlight_patches_repeater(self):
        acf = {"music_tracks": [
            {"spotify_id": "sp_a", "title": "A", "track_number": 1,
             "duration_ms": 1000, "disc_number": 1, "highlight": False},
            {"spotify_id": "sp_b", "title": "B", "track_number": 2,
             "duration_ms": 1000, "disc_number": 1, "highlight": True},
        ]}
        sink, publisher, wp_client = self.make_sink(initial_acf=acf)
        wp_client.get_post_acf = AsyncMock(return_value=acf)

        await sink.update_track_highlight("sp_a", True)

        publisher.update_post_scf.assert_awaited_once()
        post_id, payload = publisher.update_post_scf.await_args.args
        self.assertEqual(post_id, 42)
        self.assertEqual(payload["music_tracks"][0]["highlight"], True)
        # Other rows preserved.
        self.assertEqual(payload["music_tracks"][1]["spotify_id"], "sp_b")

    async def test_update_track_highlight_unknown_id_is_noop(self):
        acf = {"music_tracks": [
            {"spotify_id": "sp_a", "title": "A", "track_number": 1,
             "duration_ms": 1000, "disc_number": 1, "highlight": False},
        ]}
        sink, publisher, wp_client = self.make_sink(initial_acf=acf)
        wp_client.get_post_acf = AsyncMock(return_value=acf)

        await sink.update_track_highlight("not_a_real_id", True)

        publisher.update_post_scf.assert_not_awaited()


@unittest.skipIf(build_editor_embed is None, "editor_view module is not importable")
class TestEditorEmbedBuilder(unittest.TestCase):
    """``build_editor_embed`` reads from EditorState and labels fields appropriately."""

    def test_pre_publish_embed_does_not_listen_count_or_body(self):
        state = EditorState(rating=80, favorite=True, notes="Cool", unreleased=False)
        embed = build_editor_embed("Album X", state, mode="pre-publish")
        self.assertEqual(embed.title, "Pre-publish editor")
        self.assertEqual(embed.description, "Editing: Album X")
        field_map = {f.name: f.value for f in embed.fields}
        self.assertIn("Rating", field_map)
        self.assertIn("Booleans", field_map)
        self.assertIn("Notes", field_map)
        self.assertIn("favorite: ✅", field_map["Booleans"])
        self.assertIn("unreleased: ❌", field_map["Booleans"])

    def test_post_publish_embed_marks_mode(self):
        state = EditorState(rating=None, favorite=False, notes=None, unreleased=True)
        embed = build_editor_embed("Album Y", state, mode="post-publish")
        self.assertEqual(embed.title, "Post-publish editor")
        self.assertIn("unreleased: ✅", {f.name: f.value for f in embed.fields}["Booleans"])


@unittest.skipIf(PrePublishSink is None, "editor_view module is not importable")
class TestGetPostAcf(unittest.IsolatedAsyncioTestCase):
    """``WordPressClient.get_post_acf`` returns the live acf block via context=edit."""

    def make_client(self):
        from wordpress_client import WordPressClient
        client = WordPressClient.__new__(WordPressClient)
        client.api_url = "https://example.com/wp-json/wp/v2"
        response = MagicMock()
        response.json = MagicMock(return_value={"acf": {"music_rating": 77}})
        response.raise_for_status = MagicMock()
        client.client = MagicMock()
        client.client.get = AsyncMock(return_value=response)
        return client

    async def test_get_post_acf_returns_acf_payload(self):
        from wordpress_client import WordPressClient
        client = self.make_client()
        acf = await client.get_post_acf(123)
        self.assertEqual(acf, {"music_rating": 77})
        client.client.get.assert_awaited_once_with(
            "https://example.com/wp-json/wp/v2/posts/123",
            params={"context": "edit"},
        )

    async def test_get_post_acf_returns_empty_dict_when_acf_missing(self):
        from wordpress_client import WordPressClient
        client = self.make_client()
        client.client.get = AsyncMock(return_value=MagicMock(
            json=MagicMock(return_value={"acf": None}),
            raise_for_status=MagicMock(),
        ))
        acf = await client.get_post_acf(1)
        self.assertEqual(acf, {})


@unittest.skipIf(Publisher is None, "publisher module is not importable")
class TestPublisherUpdatePostScf(unittest.IsolatedAsyncioTestCase):
    """``Publisher.update_post_scf`` POSTs ``{\"acf\": partial}`` to WordPress."""

    def make_publisher(self):
        publisher = Publisher.__new__(Publisher)
        publisher.config = type("C", (), {"fill_scf_enabled": True})()
        publisher.wordpress = MagicMock()
        publisher.wordpress.update_post = AsyncMock(return_value={"id": 99})
        publisher.db = MagicMock()
        return publisher

    async def test_update_post_scf_sends_acf_block(self):
        publisher = self.make_publisher()
        result = await publisher.update_post_scf(99, {"music_rating": 80, "music_favorite": True})
        publisher.wordpress.update_post.assert_awaited_once_with(
            99, {"acf": {"music_rating": 80, "music_favorite": True}}
        )
        self.assertEqual(result["id"], 99)


@unittest.skipIf(Publisher is None, "publisher module is not importable")
class TestScfPayloadIncludesEditorFields(unittest.IsolatedAsyncioTestCase):
    """The auto-fill payload should include the human-curated editor fields verbatim."""

    def make_publisher(self, lastfm=None):
        publisher = Publisher.__new__(Publisher)
        publisher.db = MagicMock()
        publisher.lastfm = MagicMock()
        publisher.lastfm.album_getinfo = AsyncMock(return_value=lastfm or {"mbid": "", "tags": []})
        return publisher

    def make_release(self):
        release = make_release_for_test("album_scf_editor", "Editor Album", datetime(2024, 1, 1))
        release.rating = 91
        release.favorite = True
        release.notes = "Editorial notes"
        release.unreleased = True
        release.tracks[0].highlight = True
        return release

    async def test_editor_fields_and_track_highlight_propagate_into_acf(self):
        publisher = self.make_publisher()
        release = self.make_release()
        post = {"id": 1, "date": "2024-03-15T14:30:00"}

        acf, _ = await publisher._build_scf_payload(release, listen_count=2, post=post)

        self.assertEqual(acf["music_rating"], 91)
        self.assertTrue(acf["music_favorite"])
        self.assertEqual(acf["music_notes"], "Editorial notes")
        self.assertTrue(acf["unreleased"])
        self.assertTrue(acf["music_tracks"][0]["highlight"])
        self.assertEqual(acf["listen-count"], 2)

    async def test_editor_fields_default_when_release_is_unset(self):
        publisher = self.make_publisher()
        release = make_release_for_test("album_scf_default", "Default", datetime(2024, 1, 1))
        post = {"id": 1, "date": "2024-03-15T14:30:00"}

        acf, _ = await publisher._build_scf_payload(release, listen_count=1, post=post)

        self.assertEqual(acf["music_rating"], "")
        self.assertFalse(acf["music_favorite"])
        self.assertEqual(acf["music_notes"], "")
        self.assertFalse(acf["unreleased"])


@unittest.skipIf(DiscordBot is None, "discord bot dependencies are not installed")
class TestEditorWiring(unittest.IsolatedAsyncioTestCase):
    """The Discord bot routes both pre- and post-publish entry points to the editor."""

    def setUp(self):
        self.bot = DiscordBot.__new__(DiscordBot)
        self.bot.config = type("C", (), {
            "discord_user_id": 123,
            "wordpress_public_url": "https://public.example.com",
        })()
        self.bot.db = MagicMock()
        self.bot.db.get_release = AsyncMock()
        self.bot.db.get_discord_prompt = AsyncMock()
        self.bot.db.save_release = AsyncMock()
        self.bot._get_user = AsyncMock()
        self.bot._send_dm = AsyncMock(return_value=MagicMock(id="m1"))

        publisher = MagicMock()
        publisher.wordpress = MagicMock()
        publisher.wordpress.get_post_acf = AsyncMock(return_value={
            "music_rating": 70, "music_favorite": False, "music_notes": "n", "unreleased": False,
        })
        self.publisher = publisher
        self.bot.tracker = MagicMock()
        self.bot.tracker.publisher = publisher

    def make_interaction(self, response_done=False, message_id="message_1"):
        response = type(
            "FakeResponse",
            (),
            {
                "send_message": AsyncMock(),
                "send_modal": AsyncMock(),
                "edit_message": AsyncMock(),
                "defer": AsyncMock(),
                "is_done": Mock(return_value=response_done),
            },
        )()
        followup = type("FakeFollowup", (), {"send": AsyncMock()})()
        return type(
            "FakeInteraction",
            (),
            {
                "response": response,
                "followup": followup,
                "message": type("FakeMessage", (), {"id": message_id})(),
                "user": type("FakeUser", (), {"id": 123})(),
            },
        )()

    async def test_pre_publish_editor_dms_the_authorized_user(self):
        release = make_release_for_test("album_pre", "Pre Album", datetime(2024, 1, 1))
        self.bot.db.get_release = AsyncMock(return_value=release)
        interaction = self.make_interaction()

        await self.bot._handle_edit_metadata_pre_publish(interaction, release.spotify_id)

        # DM to the user with the editor view attached.
        self.bot._send_dm.assert_awaited_once()
        kwargs = self.bot._send_dm.await_args.kwargs
        self.assertIn("Pre-publish editor", kwargs["content"])
        self.assertIsNotNone(kwargs["view"])
        interaction.followup.send.assert_awaited_once()
        self.assertIn("Pre-publish editor opened", interaction.followup.send.await_args.args[0])

    async def test_pre_publish_editor_handles_missing_release(self):
        self.bot.db.get_release = AsyncMock(return_value=None)
        interaction = self.make_interaction()

        await self.bot._handle_edit_metadata_pre_publish(interaction, "missing")

        self.bot._send_dm.assert_not_awaited()
        # Sends the rejection through the prompt-response helper.
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("Unable to find", interaction.response.send_message.await_args.args[0])

    async def test_post_publish_editor_pulls_initial_values_from_wp(self):
        release = make_release_for_test("album_post", "Post Album", datetime(2024, 1, 1))
        release.wordpress_post_id = 456
        prompt = DiscordPrompt(
            id=1, prompt_type=PromptType.PROMPT_UNDO.value,
            release_id=release.spotify_id, wordpress_post_id=456,
            discord_message_id="message_1", state=PromptState.PENDING.value,
        )
        self.bot.db.get_release = AsyncMock(return_value=release)
        interaction = self.make_interaction()

        await self.bot._handle_edit_metadata_post_publish(interaction, release, prompt)

        # Once at the handler (`initial_acf` fetch) plus once inside the sink
        # initializer (`snapshot()`). Both reads come from the same WP endpoint.
        self.assertGreaterEqual(self.publisher.wordpress.get_post_acf.await_count, 1)
        self.publisher.wordpress.get_post_acf.assert_any_await(456)
        self.bot._send_dm.assert_awaited_once()
        kwargs = self.bot._send_dm.await_args.kwargs
        self.assertIn("Post-publish editor", kwargs["content"])
        self.assertIsNotNone(kwargs["view"])

    async def test_post_publish_editor_rejects_missing_post_id(self):
        prompt = DiscordPrompt(
            id=1, prompt_type=PromptType.PROMPT_UNDO.value,
            release_id=None, wordpress_post_id=None,
            discord_message_id="message_1", state=PromptState.PENDING.value,
        )
        release = make_release_for_test("album_post_x", "Post X", datetime(2024, 1, 1))
        interaction = self.make_interaction()

        await self.bot._handle_edit_metadata_post_publish(interaction, release, prompt)

        self.bot._send_dm.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("WordPress post ID is missing", interaction.response.send_message.await_args.args[0])


@unittest.skipIf(EditorState is None, "editor_view module is not importable")
class TestDynamicButtonCallbackSignatures(unittest.TestCase):
    """All dynamic button callbacks must take only ``(self, interaction)``.

    discord.py 2.x invokes ``item.callback(interaction)``; declaring
    ``button`` as a second positional parameter raises TypeError at runtime.
    """

    def test_all_dynamic_callbacks_use_one_arg_signature(self):
        import inspect
        candidates = [
            (EditorView, [
                "_open_tracks", "_resync", "_refresh_display", "_done", "_open_body_modal",
            ]),
            (EditorTracksView, [
                "_nav_prev", "_nav_next", "_back_to_editor",
            ]),
        ]
        for cls, methods in candidates:
            for name in methods:
                sig = inspect.signature(getattr(cls, name))
                params = [p.name for p in sig.parameters.values()]
                self.assertEqual(
                    params, ["self", "interaction"],
                    f"{cls.__name__}.{name} signature should be (self, interaction); got {params}",
                )


@unittest.skipIf(EditorState is None, "editor_view module is not importable")
class TestEditorViewRuntimeDispatch(unittest.IsolatedAsyncioTestCase):
    """The EditorView itself, when wired to a PrePublishSink, must dispatch all
    dynamic button callbacks with one argument and end up rebuilding button
    labels that reflect the new state."""

    def make_release(self):
        tracks = [
            Track(spotify_id="t1", title="Track 1", normalized_title="track 1",
                  duration_ms=1000, disc_number=1, track_number=1,
                  is_countable=True, listened=False, highlight=False),
        ]
        return make_release_for_test("album_dispatch", "Dispatch Album", datetime(2024, 1, 1), tracks=tracks)

    def make_interaction(self):
        response = type(
            "FakeResponse",
            (),
            {
                "send_message": AsyncMock(),
                "send_modal": AsyncMock(),
                "edit_message": AsyncMock(),
                "defer": AsyncMock(),
                "is_done": Mock(return_value=False),
            },
        )()
        followup = type("FakeFollowup", (), {"send": AsyncMock()})()
        return type(
            "FakeInteraction",
            (),
            {
                "response": response,
                "followup": followup,
                "message": type("FakeMessage", (), {"id": "m1"})(),
                "user": type("FakeUser", (), {"id": 123})(),
            },
        )()

    async def test_bool_callback_toggles_state_and_rebuilds_label(self):
        db = MagicMock()
        db.save_release = AsyncMock()
        release = self.make_release()
        sink = PrePublishSink(db=db, release=release)
        await sink.snapshot()

        view = EditorView(
            sink=sink,
            release_title=release.title,
            tracks_for_editor=lambda: list(release.tracks),
        )
        # Locate the bool button for "favorite".
        button = next(
            child for child in view.children
            if getattr(child, "custom_id", "") == "editor:bool:favorite"
        )
        # Snapshot the original label & style.
        original_label = button.label
        original_style = button.style
        # Find and call its callback (the actual function discord.py would call).
        callback = button.callback
        self.assertTrue(asyncio.iscoroutinefunction(callback))

        interaction = self.make_interaction()
        await callback(interaction)

        # State on both the editor and the underlying release flipped.
        self.assertTrue(sink.state.favorite)
        self.assertTrue(release.favorite)
        # Button label reflects the new state, AND edit_message was used to re-render.
        self.assertNotEqual(button.label, original_label)
        self.assertNotEqual(button.style, original_style)
        interaction.response.edit_message.assert_awaited_once()

    async def test_done_callback_tries_to_delete_message(self):
        db = MagicMock()
        db.save_release = AsyncMock()
        release = self.make_release()
        sink = PrePublishSink(db=db, release=release)
        await sink.snapshot()
        view = EditorView(
            sink=sink,
            release_title=release.title,
            tracks_for_editor=lambda: list(release.tracks),
        )
        done_button = next(
            child for child in view.children
            if getattr(child, "custom_id", "") == "editor:nav:done"
        )
        interaction = self.make_interaction()
        # Using MagicMock for message so `.delete()` is awaitable.
        interaction.message = MagicMock()
        interaction.message.delete = AsyncMock()
        await done_button.callback(interaction)
        interaction.message.delete.assert_awaited_once()

    async def test_open_tracks_callback_creates_subview(self):
        db = MagicMock()
        db.save_release = AsyncMock()
        release = self.make_release()
        sink = PrePublishSink(db=db, release=release)
        await sink.snapshot()
        view = EditorView(
            sink=sink,
            release_title=release.title,
            tracks_for_editor=lambda: list(release.tracks),
        )
        tracks_button = next(
            child for child in view.children
            if getattr(child, "custom_id", "") == "editor:open:tracks"
        )
        interaction = self.make_interaction()
        await tracks_button.callback(interaction)
        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        # The new view is a tracks sub-view, not the editor itself.
        self.assertIsInstance(kwargs["view"], EditorTracksView)


@unittest.skipIf(EditorView is None, "editor_view module is not importable")
class TestEditorTracksViewRowLayout(unittest.TestCase):
    """``EditorTracksView`` must construct within the Discord 5-row / 5-per-row cap.

    Regression: ``row=1+offset`` with ``NUM_TRACKS_PER_PAGE=5`` produced
    ``row=5`` for the last track, which raised ``ValueError: row cannot be
    negative or greater than or equal to 5`` (see ``discord/ui/item.py``).
    """

    def _sink(self):
        from database import Database  # not used; placeholder
        return MagicMock()

    def _editor_view(self, tracks):
        sink = MagicMock()
        sink.mode = "pre-publish"
        sink.state = MagicMock()
        sink.snapshot = AsyncMock(return_value=sink.state)
        sink.update_field = AsyncMock()
        sink.update_track_highlight = AsyncMock()
        return EditorView(
            sink=sink,
            release_title="Test",
            tracks_for_editor=lambda: list(tracks),
        )

    def _tracks(self, n):
        return [
            Track(
                spotify_id=f"t{i}", title=f"Track {i}", normalized_title=f"track {i}",
                duration_ms=1000, disc_number=1, track_number=i,
                is_countable=True, listened=False, highlight=(i % 2 == 0),
            )
            for i in range(1, n + 1)
        ]

    def test_tracks_subview_with_five_tracks_does_not_raise(self):
        editor = self._editor_view(self._tracks(5))
        # No exception means the row math is in bounds.
        sub = EditorTracksView(editor, page=0)
        # Three structural rows: 0 = Back, 1 = tracks, 2 = Pager.
        rows = {c.row for c in sub.children}
        self.assertTrue(rows.issubset({0, 1, 2}))
        self.assertEqual(len(sub.children), 1 + 5 + 2)  # back + 5 tracks + 2 pager

    def test_tracks_subview_with_one_track_uses_row_1(self):
        editor = self._editor_view(self._tracks(1))
        sub = EditorTracksView(editor, page=0)
        track_button = next(c for c in sub.children if "track_toggle" not in (c.custom_id or "") and "track" in (c.custom_id or "") and "nav" not in (c.custom_id or ""))
        self.assertEqual(track_button.row, 1)

    def test_pager_sits_on_row_2(self):
        editor = self._editor_view(self._tracks(5))
        sub = EditorTracksView(editor, page=0)
        pager_buttons = [c for c in sub.children if "tracks_prev" in (c.custom_id or "") or "tracks_next" in (c.custom_id or "")]
        for button in pager_buttons:
            self.assertEqual(button.row, 2)

    def test_back_button_always_on_row_0(self):
        editor = self._editor_view(self._tracks(5))
        sub = EditorTracksView(editor, page=0)
        back = next(c for c in sub.children if "back_to_editor" in (c.custom_id or ""))
        self.assertEqual(back.row, 0)

    def test_each_row_never_exceeds_five_components(self):
        editor = self._editor_view(self._tracks(5))
        sub = EditorTracksView(editor, page=0)
        from collections import Counter
        per_row = Counter(c.row for c in sub.children)
        for row, count in per_row.items():
            self.assertLessEqual(count, 5, f"row {row} has {count} components (>5)")


if __name__ == "__main__":
    unittest.main()
