import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from database import Database
from album_metadata.lastfm import parse_lastfm_listeners
from models import CachedAlbumMetadata, LifecycleStatus, Release, ReleaseType, SavedLibraryAlbum, Track


class AlbumMetadataCacheDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db = Database(SimpleNamespace(db_path=root / "test.db", project_root=Path(__file__).parents[1]))
        await self.db.initialize()

    async def asyncTearDown(self):
        await self.db.close()
        self.tempdir.cleanup()

    def metadata(self, spotify_id="album-a", *, version=1, status="ready"):
        return CachedAlbumMetadata(
            spotify_id=spotify_id,
            status=status,
            lastfm_url="https://www.last.fm/music/Artist/Album" if status == "ready" else None,
            lastfm_mbid="12345678-1234-4123-8123-123456789abc" if status == "ready" else None,
            genres=["Rock", "Dream Pop"] if status == "ready" else [],
            lastfm_listeners=123456 if status == "ready" else None,
            diagnostic_code=None if status == "ready" else "lastfm_ambiguous",
            resolver_version=version,
            updated_at=datetime(2026, 9, 14, 12, 0),
        )

    def saved_album(self, spotify_id, *, posted=False):
        return SavedLibraryAlbum(
            spotify_id=spotify_id,
            spotify_uri=f"spotify:album:{spotify_id}",
            spotify_url=f"https://open.spotify.com/album/{spotify_id}",
            title=spotify_id,
            normalized_title=spotify_id,
            artists=["Artist"],
            normalized_artists=["artist"],
            album_type="album",
            release_type=ReleaseType.ALBUM,
            cover_url="",
            added_at=datetime(2026, 9, 14),
            is_posted_listened=posted,
        )

    async def test_migration_and_cache_round_trip(self):
        await self.db.save_album_metadata_cache(self.metadata())
        self.assertEqual(await self.db.get_album_metadata_cache("album-a"), self.metadata())
        cursor = await self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_album_metadata_cache_listeners'"
        )
        self.assertIsNotNone(await cursor.fetchone())

    async def test_missing_query_uses_unposted_and_current_version(self):
        for spotify_id, posted in (("missing", False), ("posted", True), ("current", False),
                                   ("old", False), ("unresolved", False)):
            await self.db.upsert_saved_library_album(self.saved_album(spotify_id, posted=posted))
        await self.db.save_album_metadata_cache(self.metadata("current"))
        await self.db.save_album_metadata_cache(self.metadata("old", version=0))
        await self.db.save_album_metadata_cache(self.metadata("unresolved", status="unresolved"))

        self.assertEqual(
            await self.db.get_unposted_saved_library_ids_missing_metadata(1),
            ["missing", "old"],
        )

    async def test_cache_survives_saved_library_and_release_deletion(self):
        await self.db.save_album_metadata_cache(self.metadata())
        await self.db.upsert_saved_library_album(self.saved_album("album-a"))
        await self.db.delete_saved_library_albums(["album-a"])
        self.assertIsNotNone(await self.db.get_album_metadata_cache("album-a"))

        now = datetime.now()
        await self.db.save_release(Release(
            spotify_id="album-a", title="Album", normalized_title="album", artists=[],
            release_type=ReleaseType.ALBUM, raw_spotify_type="album", cover_url="",
            release_date="2026", total_tracks=0, total_duration_ms=0, tracks=[], progress=0,
            status=LifecycleStatus.ACTIVE, first_seen=now, last_seen=now,
        ))
        await self.db.delete_release("album-a")
        self.assertIsNotNone(await self.db.get_album_metadata_cache("album-a"))

    async def test_release_track_explicitness_survives_round_trip(self):
        now = datetime.now()
        track = Track("track-a", "Track", "track", 1000, 1, 1, True, False, explicit=True)
        await self.db.save_release(Release(
            spotify_id="album-explicit", title="Album", normalized_title="album", artists=[],
            release_type=ReleaseType.ALBUM, raw_spotify_type="album", cover_url="",
            release_date="2026", total_tracks=1, total_duration_ms=1000, tracks=[track], progress=0,
            status=LifecycleStatus.ACTIVE, first_seen=now, last_seen=now,
        ))

        stored = await self.db.get_release("album-explicit")
        self.assertTrue(stored.tracks[0].explicit)

class ListenerParsingTests(unittest.TestCase):
    def test_parses_only_non_negative_listener_counts(self):
        self.assertEqual(parse_lastfm_listeners({"listeners": "123456"}), 123456)
        self.assertEqual(parse_lastfm_listeners({"listeners": 0}), 0)
        for value in (None, "", "many", -1):
            with self.subTest(value=value):
                self.assertIsNone(parse_lastfm_listeners({"listeners": value}))
        self.assertIsNone(parse_lastfm_listeners({}))

if __name__ == "__main__":
    unittest.main()
