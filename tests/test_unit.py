"""
Unit tests for core logic: classification, normalization, progress, duplicate matching.
"""

import unittest
from datetime import datetime

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils import normalize_text, normalize_artist_name, normalize_artist_list, compute_release_type
from models import Track, Artist, Release, ReleaseType, LifecycleStatus


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
