"""
Unit tests for core logic: classification, normalization, progress, duplicate matching.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils import normalize_text, normalize_artist_name, normalize_artist_list, compute_release_type
from models import Track, Artist, Release, ReleaseType, LifecycleStatus, PlaybackState, WordPressPost
from inprogress import build_inprogress_page, INPROGRESS_PAGE_SIZE, get_next_unlistened_track

try:
    from tracker import Tracker
except ModuleNotFoundError:
    Tracker = None

try:
    from discord_bot import DiscordBot, CurrentPlaybackActionView, CurrentPostContext
except ModuleNotFoundError:
    DiscordBot = None
    CurrentPlaybackActionView = None
    CurrentPostContext = None


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


@unittest.skipIf(DiscordBot is None, "discord bot dependencies are not installed")
class TestDiscordBotEmbeds(unittest.IsolatedAsyncioTestCase):
    """Test embed formatting for Discord bot views."""

    def setUp(self):
        self.bot = DiscordBot.__new__(DiscordBot)
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

        self.assertIn("Relisten", field_map)
        self.assertIn("post 123", field_map["Relisten"])

    def test_current_preview_omits_relisten_field_without_duplicate(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))

        embed = self.bot._build_current_preview_embed(state, self.make_current_context(release))
        field_names = [field.name for field in embed.fields]

        self.assertNotIn("Relisten", field_names)

    async def test_current_confirm_publishes_as_relisten_when_duplicate_exists(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))

        class FakeDatabase:
            async def get_release(self, album_id):
                return release

        class FakeTracker:
            def __init__(self):
                self.published = None

            async def _check_duplicate(self, release_to_check):
                return WordPressPost(
                    id=123,
                    title="Album 5",
                    normalized_title="album 5",
                    artists=["Artist"],
                    normalized_artists=["artist"],
                    link="https://example.com/album-5"
                )

            async def _get_or_create_release(self, album_id):
                return release

            async def publish_release_now(self, release_to_publish, as_relisten=False):
                self.published = (release_to_publish, as_relisten)

        class FakeInteraction:
            response = type(
                "FakeResponse",
                (),
                {"send_message": AsyncMock()}
            )()

        tracker = FakeTracker()
        self.bot.db = FakeDatabase()
        self.bot.tracker = tracker

        await self.bot._handle_current_post_confirm(FakeInteraction(), state)

        self.assertEqual(tracker.published, (release, True))

    async def test_inprogress_publish_uses_stored_duplicate_state_for_relisten(self):
        release = make_release_for_test("album_7", "Album 7", datetime(2024, 1, 1, 12, 0, 0))
        release.duplicate_state = "found"

        class FakeDatabase:
            async def get_release(self, release_id):
                return release

        class FakeTracker:
            def __init__(self):
                self.published = None

            async def publish_release_now(self, release_to_publish, as_relisten=False):
                self.published = (release_to_publish, as_relisten)

        class FakeInteraction:
            response = type(
                "FakeResponse",
                (),
                {"send_message": AsyncMock()}
            )()

        tracker = FakeTracker()
        self.bot.db = FakeDatabase()
        self.bot.tracker = tracker

        await self.bot._handle_publish_release(FakeInteraction(), release.spotify_id)

        self.assertEqual(tracker.published, (release, True))

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

            async def save_service_state(self, key, value):
                return None

        async def get_or_create_release(album_id):
            self.assertEqual(album_id, "album_1")
            return release

        db = FakeDatabase()
        tracker = Tracker.__new__(Tracker)
        tracker.spotify = FakeSpotify()
        tracker.db = db
        tracker.discord_bot = None
        tracker.active_interval = 0
        tracker._get_or_create_release = get_or_create_release

        await tracker._poll_once()

        self.assertIsNotNone(db.touched)
        self.assertEqual(db.touched[0], "album_1")
        self.assertGreater(db.touched[1], old_seen)
        self.assertEqual(release.last_seen, db.touched[1])


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


if __name__ == "__main__":
    unittest.main()
