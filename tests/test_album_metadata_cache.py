import tempfile
import unittest
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from database import Database, popularity_pool_size
from album_metadata.lastfm import parse_lastfm_listeners
from album_metadata_cache import AlbumMetadataCacheService, MetadataCacheProviderError
from models import Artist, CachedAlbumMetadata, LifecycleStatus, Release, ReleaseType, SavedLibraryAlbum, Track


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

    async def test_editor_fields_use_targeted_updates_and_body_round_trips(self):
        now = datetime.now()
        track = Track("track-editor", "Track", "track", 1000, 1, 1, True, False)
        release = Release(
            spotify_id="album-editor", title="Album", normalized_title="album",
            artists=[], release_type=ReleaseType.ALBUM, raw_spotify_type="album",
            cover_url="", release_date="2026", total_tracks=1,
            total_duration_ms=1000, tracks=[track], progress=0,
            status=LifecycleStatus.ACTIVE, first_seen=now, last_seen=now,
        )
        await self.db.save_release(release)

        await self.db.update_release_editor_field(
            release.spotify_id, "body_content", "Draft body")
        await self.db.update_track_highlight(
            release.spotify_id, track.spotify_id, True)

        stored = await self.db.get_release(release.spotify_id)
        self.assertEqual(stored.body_content, "Draft body")
        self.assertTrue(stored.tracks[0].highlight)

    async def test_publish_claim_is_atomic(self):
        now = datetime.now()
        release = Release(
            spotify_id="album-claim", title="Album", normalized_title="album",
            artists=[], release_type=ReleaseType.ALBUM, raw_spotify_type="album",
            cover_url="", release_date="2026", total_tracks=0,
            total_duration_ms=0, tracks=[], progress=0,
            status=LifecycleStatus.ACTIVE, first_seen=now, last_seen=now,
        )
        await self.db.save_release(release)

        self.assertTrue(await self.db.claim_release_for_publish(release.spotify_id))
        self.assertFalse(await self.db.claim_release_for_publish(release.spotify_id))

    async def test_random_zero_includes_album_without_metadata(self):
        await self.db.upsert_saved_library_album(self.saved_album("unknown"))

        selection = await self.db.get_random_unposted_saved_library_album(0)

        self.assertEqual(selection.album.spotify_id, "unknown")
        self.assertEqual(selection.popularity_focus, 0)

    async def test_focused_random_uses_only_top_ten_ranked_listener_counts(self):
        for index in range(20):
            spotify_id = f"album-{index:02}"
            await self.db.upsert_saved_library_album(self.saved_album(spotify_id))
            metadata = self.metadata(spotify_id)
            metadata.lastfm_listeners = 20 - index
            await self.db.save_album_metadata_cache(metadata)
        await self.db.upsert_saved_library_album(self.saved_album("unknown"))

        for _ in range(20):
            selection = await self.db.get_random_unposted_saved_library_album(100)
            self.assertLessEqual(selection.popularity_rank, 10)
            self.assertEqual(selection.rankable_total, 20)
            self.assertEqual(selection.candidate_pool_size, 10)
            self.assertNotEqual(selection.album.spotify_id, "unknown")

class ListenerParsingTests(unittest.TestCase):
    def test_parses_only_non_negative_listener_counts(self):
        self.assertEqual(parse_lastfm_listeners({"listeners": "123456"}), 123456)
        self.assertEqual(parse_lastfm_listeners({"listeners": 0}), 0)
        for value in (None, "", "many", -1):
            with self.subTest(value=value):
                self.assertIsNone(parse_lastfm_listeners({"listeners": value}))
        self.assertIsNone(parse_lastfm_listeners({}))

    def test_popularity_pool_formula_boundaries(self):
        self.assertEqual(popularity_pool_size(0, 100), 0)
        self.assertEqual(popularity_pool_size(9, 100), 9)
        self.assertEqual(popularity_pool_size(10, 100), 10)
        self.assertEqual(popularity_pool_size(600, 0), 600)
        self.assertEqual(popularity_pool_size(600, 50), 77)
        self.assertEqual(popularity_pool_size(600, 100), 10)
        with self.assertRaises(ValueError):
            popularity_pool_size(100, 101)


class FakeMetadataSpotify:
    def __init__(self):
        self.album_calls = 0

    def album_with_tracks(self, spotify_id):
        self.album_calls += 1
        return metadata_evidence(spotify_id)


class FakeMetadataLastFM:
    def __init__(self, *, empty=False, failure=False):
        self.empty = empty
        self.failure = failure
        self.search_calls = 0
        self.info_calls = 0

    def album_search(self, query, limit=10):
        self.search_calls += 1
        if self.failure:
            raise RuntimeError("provider unavailable")
        return [] if self.empty else [{
            "name": "Album", "artist": "Artist", "url": "https://last.fm/album"}]

    def album_getinfo(self, **kwargs):
        self.info_calls += 1
        return {
            "name": "Album",
            "artist": "Artist",
            "url": "https://last.fm/album",
            "listeners": "9876",
            "tracks": {"track": [{"name": "Track"}]},
            "tags": {"tag": [{"name": "Rock"}]},
        }


def metadata_evidence(spotify_id="album-a"):
    return ({
        "id": spotify_id,
        "name": "Album",
        "artists": [{"id": "artist-a", "name": "Artist"}],
        "album_type": "album",
        "release_date": "2026-01-01",
        "total_tracks": 1,
    }, [{
        "id": "track-a", "name": "Track", "duration_ms": 1000,
        "disc_number": 1, "track_number": 1, "explicit": False,
    }])


def metadata_release(spotify_id="album-a"):
    now = datetime.now()
    return Release(
        spotify_id=spotify_id, title="Album", normalized_title="album",
        artists=[Artist("artist-a", "Artist", "artist")],
        release_type=ReleaseType.ALBUM, raw_spotify_type="album", cover_url="",
        release_date="2026-01-01", total_tracks=1, total_duration_ms=1000,
        tracks=[Track("track-a", "Track", "track", 1000, 1, 1, True, False)],
        progress=0, status=LifecycleStatus.ACTIVE, first_seen=now, last_seen=now,
    )


class AlbumMetadataCacheServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db = Database(SimpleNamespace(
            db_path=root / "test.db", project_root=Path(__file__).parents[1]))
        await self.db.initialize()

    async def asyncTearDown(self):
        await self.db.close()
        self.tempdir.cleanup()

    def service(self, spotify, lastfm):
        return AlbumMetadataCacheService(
            SimpleNamespace(spotify_client_id="id", spotify_client_secret="secret", lastfm_api_key="key"),
            self.db,
            provider_factory=lambda: (spotify, lastfm),
        )

    async def test_release_resolution_uses_local_spotify_evidence_and_caches_once(self):
        spotify, lastfm = FakeMetadataSpotify(), FakeMetadataLastFM()
        service = self.service(spotify, lastfm)
        first, second = await asyncio.gather(
            service.ensure_for_release(metadata_release()),
            service.ensure_for_release(metadata_release()),
        )
        self.assertEqual((first.status, first.lastfm_listeners, first.genres),
                         ("ready", 9876, ["Rock"]))
        self.assertEqual(first, second)
        self.assertEqual(spotify.album_calls, 0)
        self.assertEqual(lastfm.info_calls, 1)

    async def test_deterministic_unresolved_is_cached_but_provider_failure_is_not(self):
        spotify, lastfm = FakeMetadataSpotify(), FakeMetadataLastFM(empty=True)
        service = self.service(spotify, lastfm)
        unresolved = await service.ensure_for_release(metadata_release())
        self.assertEqual((unresolved.status, unresolved.diagnostic_code),
                         ("unresolved", "lastfm_catalog_unavailable"))
        calls = lastfm.search_calls
        await service.ensure_for_release(metadata_release())
        self.assertEqual(lastfm.search_calls, calls)

        failing = self.service(FakeMetadataSpotify(), FakeMetadataLastFM(failure=True))
        with self.assertRaises(MetadataCacheProviderError):
            await failing.ensure_for_release(metadata_release("album-b"))
        self.assertIsNone(await self.db.get_album_metadata_cache("album-b"))

    async def test_backfill_commits_progress_and_skips_completed_rows(self):
        for spotify_id in ("album-a", "album-b"):
            await self.db.upsert_saved_library_album(
                AlbumMetadataCacheDatabaseTests.saved_album(self, spotify_id))
        spotify, lastfm = FakeMetadataSpotify(), FakeMetadataLastFM()
        service = self.service(spotify, lastfm)

        service.trigger_saved_library_backfill()
        await service._backfill_task
        self.assertEqual(spotify.album_calls, 2)
        service.trigger_saved_library_backfill()
        await service._backfill_task
        self.assertEqual(spotify.album_calls, 2)

if __name__ == "__main__":
    unittest.main()
