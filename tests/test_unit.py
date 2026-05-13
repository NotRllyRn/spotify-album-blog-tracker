"""
Unit tests for core logic: classification, normalization, progress, duplicate matching.
"""

import hashlib
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

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
    DiscordPrompt,
    PromptState,
    PromptType,
)
from inprogress import build_inprogress_page, INPROGRESS_PAGE_SIZE, get_next_unlistened_track

try:
    from tracker import Tracker
except ModuleNotFoundError:
    Tracker = None

try:
    from discord_bot import DiscordBot, CurrentPlaybackActionView, CurrentPostContext, PublishedPostActionView, RelistenApprovalPromptView
except ModuleNotFoundError:
    DiscordBot = None
    CurrentPlaybackActionView = None
    CurrentPostContext = None
    PublishedPostActionView = None
    RelistenApprovalPromptView = None

try:
    from publisher import (
        Publisher,
        POST_CACHE_FIRST_PAGE_HASH_KEY,
        POST_CACHE_TOTAL_KEY,
        format_discord_content_for_wordpress,
    )
    from wordpress_client import WordPressClient, WordPressPostsResult
except ModuleNotFoundError:
    Publisher = None
    WordPressClient = None
    WordPressPostsResult = None
    POST_CACHE_FIRST_PAGE_HASH_KEY = None
    POST_CACHE_TOTAL_KEY = None
    format_discord_content_for_wordpress = None


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

    def make_interaction(self, response_done=False, message_id="message_1"):
        response = type(
            "FakeResponse",
            (),
            {
                "send_message": AsyncMock(),
                "send_modal": AsyncMock(),
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
            }
        )()

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

    def test_published_post_action_view_includes_add_content(self):
        labels = [child.label for child in PublishedPostActionView(self.bot).children]

        self.assertEqual(labels, ["Add content", "Undo post", "Keep post"])

    def test_relisten_approval_view_has_single_yes_action(self):
        labels = [child.label for child in RelistenApprovalPromptView(self.bot).children]

        self.assertEqual(labels, ["Yes, track as relisten"])

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
        publisher = type("FakePublisher", (), {})()
        publisher.trash_post = AsyncMock(return_value=True)
        self.bot.db = db
        self.bot.tracker = type("FakeTracker", (), {"publisher": publisher})()
        interaction = self.make_interaction()

        await self.bot._handle_undo_post(interaction, release, prompt)

        db.update_discord_prompt_state.assert_awaited_once_with("message_1", PromptState.ACCEPTED.value)
        publisher.trash_post.assert_awaited_once_with(321)
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

        self.assertIn("Relisten", field_map)
        self.assertIn("post 123", field_map["Relisten"])

    def test_current_preview_omits_relisten_field_without_duplicate(self):
        state = self.make_playback_state()
        release = make_release_for_test("album_5", "Album 5", datetime(2024, 1, 1, 12, 0, 0))

        embed = self.bot._build_current_preview_embed(state, self.make_current_context(release))
        field_names = [field.name for field in embed.fields]

        self.assertNotIn("Relisten", field_names)

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

            async def log_audit_event(self, event_type, payload):
                audit_events.append((event_type, payload))

        class FakePublisher:
            async def publish_release(self, release_to_publish, as_relisten=False):
                release_to_publish.wordpress_post_id = 456
                return {"id": 456}

        tracker = Tracker.__new__(Tracker)
        tracker.db = FakeDatabase()
        tracker.publisher = FakePublisher()
        tracker.discord_bot = None

        await tracker._publish_release(release)

        self.assertEqual(saved_statuses[0][0], LifecycleStatus.PUBLISHING)
        self.assertEqual(saved_statuses[1][0], LifecycleStatus.PUBLISHED_RECENTLY)
        self.assertIsNotNone(saved_statuses[1][1])
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

        post = await publisher.publish_release(release)

        self.assertEqual(post["id"], 99)
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


if __name__ == "__main__":
    unittest.main()
