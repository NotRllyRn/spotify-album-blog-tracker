"""
Database layer using aiosqlite.
"""

import aiosqlite
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Sequence
from datetime import datetime

from config import Config
from models import (
    Release,
    Artist,
    Track,
    WordPressPost,
    DiscordPrompt,
    ReleaseType,
    LifecycleStatus,
    SavedLibraryAlbum,
    SavedLibrarySnapshotItem,
    SavedLibraryStats,
    CachedAlbumMetadata,
    RandomAlbumSelection,
)
from album_metadata_cache import METADATA_RESOLVER_VERSION

logger = logging.getLogger(__name__)


def popularity_pool_size(rankable_total: int, popularity_focus: int) -> int:
    """Return the nonlinear top-ranked candidate count for a popularity focus."""
    if not 0 <= popularity_focus <= 100:
        raise ValueError("Popularity focus must be between 0 and 100")
    if rankable_total <= 10 or popularity_focus == 0:
        return max(0, rankable_total)
    size = round(rankable_total * (10 / rankable_total) ** (popularity_focus / 100))
    return min(rankable_total, max(10, size))


def _load_json_list(value: str) -> list:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Database contains malformed JSON") from exc
    if not isinstance(parsed, list):
        raise RuntimeError("Database JSON value is not a list")
    return parsed


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Database contains invalid {field}") from exc


class Database:
    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.db_path
        self._connection: Optional[aiosqlite.Connection] = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not initialized")
        return self._connection

    @connection.setter
    def connection(self, value: Optional[aiosqlite.Connection]) -> None:
        self._connection = value

    async def initialize(self):
        """Initialize database and run migrations."""
        self.connection = await aiosqlite.connect(self.db_path)
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")

        # Run migrations
        await self._run_migrations()

    async def close(self):
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _run_migrations(self):
        """Run database migrations."""
        migrations_dir = self.config.project_root / "migrations"
        if not migrations_dir.exists():
            return

        # Get current version
        version = await self._get_schema_version()

        # Run pending migrations
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            try:
                migration_version = int(migration_file.stem.split("_")[0])
                sql = migration_file.read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Invalid migration: {migration_file.name}") from exc
            if migration_version > version:
                logger.info(f"Running migration {migration_file.name}")
                await self.connection.executescript(sql)
                await self._set_schema_version(migration_version)

    async def _get_schema_version(self) -> int:
        """Get current schema version."""
        try:
            cursor = await self.connection.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = await cursor.fetchone()
            return row[0] if row else 0
        except aiosqlite.OperationalError as exc:
            logger.debug("Schema version is not initialized: %s", exc)
            return 0

    async def _set_schema_version(self, version: int):
        """Set schema version."""
        await self.connection.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (version,))
        await self.connection.commit()

    # Release operations
    async def get_release(self, spotify_id: str) -> Optional[Release]:
        """Get release by Spotify ID."""
        cursor = await self.connection.execute("""
            SELECT * FROM release_lifecycle WHERE spotify_id = ?
        """, (spotify_id,))
        row = await cursor.fetchone()
        if not row:
            return None

        # Load artists
        artists = await self._get_release_artists(row[0])

        # Load tracks
        tracks = await self._get_release_tracks(row[0])

        return self._row_to_release(row, artists, tracks)

    async def save_release(self, release: Release):
        """Save or update release."""
        data = (
            release.spotify_id,
            release.title,
            release.normalized_title,
            release.release_type.value,
            release.raw_spotify_type,
            release.cover_url,
            release.release_date,
            release.total_tracks,
            release.total_duration_ms,
            release.progress,
            release.status.value,
            release.first_seen.isoformat(),
            release.last_seen.isoformat(),
            release.completed_at.isoformat() if release.completed_at else None,
            release.published_at.isoformat() if release.published_at else None,
            release.wordpress_post_id,
            release.wordpress_media_id,
            release.is_relisten,
            release.duplicate_state,
            release.duplicate_post_id,
            release.rating,
            release.favorite,
            release.notes,
            release.unreleased,
            release.body_content,
        )

        # pi-lens-ignore: python-sql-injection
        cursor = await self.connection.execute("""
            INSERT OR REPLACE INTO release_lifecycle
            (spotify_id, title, normalized_title, release_type, raw_spotify_type,
             cover_url, release_date, total_tracks, total_duration_ms, progress, status,
             first_seen, last_seen, completed_at, published_at, wordpress_post_id,
             wordpress_media_id, is_relisten, duplicate_state, duplicate_post_id,
             rating, favorite, notes, unreleased, body_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)

        release_id = cursor.lastrowid
        if release_id is None:
            raise RuntimeError("Database did not return a release ID")

        # Save artists
        await self._save_release_artists(release_id, release.artists)

        # Save tracks
        await self._save_release_tracks(release_id, release.tracks)

        await self.connection.commit()
        return release_id

    async def update_track_highlight(
        self, release_spotify_id: str, track_spotify_id: str, highlight: bool
    ) -> None:
        """Persist one editor-owned track flag without rewriting the release."""
        cursor = await self.connection.execute("""
            UPDATE release_track SET highlight = ?
            WHERE spotify_id = ? AND release_id = (
                SELECT id FROM release_lifecycle WHERE spotify_id = ?
            )
        """, (highlight, track_spotify_id, release_spotify_id))
        if cursor.rowcount != 1:
            raise RuntimeError("Track highlight target was not found")
        await self.connection.commit()

    async def update_release_editor_field(
        self, spotify_id: str, name: str, value: Any
    ) -> None:
        """Persist one release editor field without rewriting related rows."""
        column = {
            "rating": "rating", "favorite": "favorite", "notes": "notes",
            "unreleased": "unreleased", "body_content": "body_content",
        }.get(name)
        if column is None:
            raise ValueError(f"Unknown editor field: {name}")
        cursor = await self.connection.execute(
            f"UPDATE release_lifecycle SET {column} = ? WHERE spotify_id = ?",  # noqa: S608
            (value, spotify_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Release editor target was not found")
        await self.connection.commit()

    async def claim_release_for_publish(self, spotify_id: str) -> bool:
        """Atomically prevent duplicate publish workers for one release."""
        cursor = await self.connection.execute("""
            UPDATE release_lifecycle SET status = ?
            WHERE spotify_id = ? AND status NOT IN (?, ?)
        """, (
            LifecycleStatus.PUBLISHING.value,
            spotify_id,
            LifecycleStatus.PUBLISHING.value,
            LifecycleStatus.PUBLISHED_RECENTLY.value,
        ))
        await self.connection.commit()
        return cursor.rowcount == 1

    async def delete_release(self, spotify_id: str) -> bool:
        """Delete a release and its associated data by Spotify ID."""
        cursor = await self.connection.execute(
            "SELECT id FROM release_lifecycle WHERE spotify_id = ?",
            (spotify_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False

        release_id = row[0]
        await self.connection.execute("DELETE FROM discord_prompt WHERE release_id = ?", (spotify_id,))
        await self.connection.execute("DELETE FROM release_lifecycle WHERE id = ?", (release_id,))
        await self.connection.commit()
        return True

    async def delete_published_releases_older_than(self, cutoff: datetime) -> int:
        """Delete recently published releases once their retention window has elapsed."""
        cursor = await self.connection.execute("""
            SELECT spotify_id FROM release_lifecycle
            WHERE status = ?
              AND published_at IS NOT NULL
              AND published_at <= ?
        """, (LifecycleStatus.PUBLISHED_RECENTLY.value, cutoff.isoformat()))
        rows = await cursor.fetchall()
        spotify_ids = [row[0] for row in rows]
        if not spotify_ids:
            return 0

        rows_to_delete = [(spotify_id,) for spotify_id in spotify_ids]
        # pi-lens-ignore: python-sql-injection
        await self.connection.executemany(
            "DELETE FROM discord_prompt WHERE release_id = ?", rows_to_delete)
        # pi-lens-ignore: python-sql-injection
        await self.connection.executemany(
            "DELETE FROM release_lifecycle WHERE spotify_id = ?", rows_to_delete)
        await self.connection.commit()
        return len(spotify_ids)

    async def touch_release_last_seen(self, spotify_id: str, seen_at: datetime):
        """Update the last-seen timestamp for a tracked release."""
        await self.connection.execute("""
            UPDATE release_lifecycle
            SET last_seen = ?
            WHERE spotify_id = ?
        """, (seen_at.isoformat(), spotify_id))
        await self.connection.commit()

    async def get_active_releases(self) -> List[Release]:
        """Get all active releases."""
        cursor = await self.connection.execute("""
            SELECT * FROM release_lifecycle
            WHERE status IN ('active', 'awaiting_75_decision', 'publishing')
            ORDER BY last_seen DESC
        """)
        rows = await cursor.fetchall()

        releases = []
        for row in rows:
            artists = await self._get_release_artists(row[0])
            tracks = await self._get_release_tracks(row[0])
            releases.append(self._row_to_release(row, artists, tracks))

        return releases

    async def _get_release_artists(self, release_id: int) -> List[Artist]:
        """Get artists for a release."""
        cursor = await self.connection.execute("""
            SELECT spotify_id, name, normalized_name FROM release_artist
            WHERE release_id = ? ORDER BY name
        """, (release_id,))
        rows = await cursor.fetchall()
        return [Artist(row[0], row[1], row[2]) for row in rows]

    async def _get_release_tracks(self, release_id: int) -> List[Track]:
        """Get tracks for a release."""
        cursor = await self.connection.execute("""
            SELECT spotify_id, title, normalized_title, duration_ms, disc_number, track_number,
                   is_countable, listened, listened_at, listened_source, highlight, explicit
            FROM release_track WHERE release_id = ? ORDER BY disc_number, track_number
        """, (release_id,))
        rows = await cursor.fetchall()
        return [Track(
            spotify_id=row[0],
            title=row[1],
            normalized_title=row[2],
            duration_ms=row[3],
            disc_number=row[4],
            track_number=row[5],
            is_countable=bool(row[6]),
            listened=bool(row[7]),
            listened_at=datetime.fromisoformat(row[8]) if row[8] else None,
            listened_source=row[9],
            highlight=bool(row[10]) if len(row) > 10 else False,
            explicit=bool(row[11]) if len(row) > 11 else False,
        ) for row in rows]

    async def _save_release_artists(self, release_id: int, artists: List[Artist]):
        """Save artists for a release."""
        await self.connection.execute("DELETE FROM release_artist WHERE release_id = ?", (release_id,))
        for artist in artists:
            await self.connection.execute("""
                INSERT INTO release_artist (release_id, spotify_id, name, normalized_name)
                VALUES (?, ?, ?, ?)
            """, (release_id, artist.spotify_id, artist.name, artist.normalized_name))

    async def _save_release_tracks(self, release_id: int, tracks: List[Track]):
        """Save tracks for a release."""
        await self.connection.execute("DELETE FROM release_track WHERE release_id = ?", (release_id,))
        for track in tracks:
            await self.connection.execute("""
                INSERT INTO release_track
                (release_id, spotify_id, title, normalized_title, duration_ms, disc_number,
                 track_number, is_countable, listened, listened_at, listened_source, highlight,
                 explicit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                release_id, track.spotify_id, track.title, track.normalized_title,
                track.duration_ms, track.disc_number, track.track_number,
                track.is_countable, track.listened,
                track.listened_at.isoformat() if track.listened_at else None,
                track.listened_source,
                track.highlight,
                track.explicit,
            ))

    def _row_to_release(self, row: Sequence[Any], artists: List[Artist], tracks: List[Track]) -> Release:
        """Convert database row to Release object."""
        return Release(
            spotify_id=row[1],
            title=row[2],
            normalized_title=row[3],
            artists=artists,
            release_type=ReleaseType(row[4]),
            raw_spotify_type=row[5],
            cover_url=row[6],
            release_date=row[7],
            total_tracks=row[8],
            total_duration_ms=row[9],
            tracks=tracks,
            progress=row[10],
            status=LifecycleStatus(row[11]),
            first_seen=datetime.fromisoformat(row[12]),
            last_seen=datetime.fromisoformat(row[13]),
            completed_at=datetime.fromisoformat(row[14]) if row[14] else None,
            published_at=datetime.fromisoformat(row[15]) if row[15] else None,
            wordpress_post_id=row[16],
            wordpress_media_id=row[17],
            is_relisten=bool(row[20]) if len(row) > 20 else row[18] == "found",
            duplicate_state=row[18],
            duplicate_post_id=row[19],
            rating=row[21] if len(row) > 21 else None,
            favorite=bool(row[22]) if len(row) > 22 else False,
            notes=row[23] if len(row) > 23 else None,
            unreleased=bool(row[24]) if len(row) > 24 else False,
            body_content=row[25] if len(row) > 25 else "",
        )

    # WordPress operations
    async def get_wordpress_posts(self) -> List[WordPressPost]:
        """Get cached WordPress posts."""
        cursor = await self.connection.execute("""
            SELECT id, title, normalized_title, artists_json, normalized_artists_json, link
            FROM wordpress_post_cache
        """)
        rows = await cursor.fetchall()
        
        return [WordPressPost(
            id=row[0],
            title=row[1],
            normalized_title=row[2],
            artists=_load_json_list(row[3]),
            normalized_artists=_load_json_list(row[4]),
            link=row[5]
        ) for row in rows]

    async def save_wordpress_posts(self, posts: List[WordPressPost]):
        """Save WordPress posts cache."""
        await self.connection.execute("DELETE FROM wordpress_post_cache")
        for post in posts:
            await self.connection.execute("""
                INSERT INTO wordpress_post_cache
                (id, title, normalized_title, artists_json, normalized_artists_json, link)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                post.id, post.title, post.normalized_title,
                json.dumps(post.artists), json.dumps(post.normalized_artists),
                post.link
            ))

        await self.connection.commit()

    async def upsert_wordpress_post(self, post: WordPressPost) -> None:
        """Update the duplicate cache from a just-created post."""
        await self.connection.execute("""
            INSERT OR REPLACE INTO wordpress_post_cache
            (id, title, normalized_title, artists_json, normalized_artists_json, link)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            post.id, post.title, post.normalized_title,
            json.dumps(post.artists), json.dumps(post.normalized_artists), post.link,
        ))
        await self.connection.commit()

    # Saved Spotify library operations
    async def get_saved_library_album(self, spotify_id: str) -> Optional[SavedLibraryAlbum]:
        """Get one saved Spotify library album by ID."""
        cursor = await self.connection.execute("""
            SELECT spotify_id, spotify_uri, spotify_url, title, normalized_title,
                   artists_json, normalized_artists_json, album_type, release_type,
                   cover_url, added_at, is_posted_listened, wordpress_post_id,
                   created_at, updated_at
            FROM saved_library_album
            WHERE spotify_id = ?
        """, (spotify_id,))
        row = await cursor.fetchone()
        return self._row_to_saved_library_album(row) if row else None

    async def get_saved_library_album_ids(self) -> Set[str]:
        """Get all stored saved-library Spotify album IDs."""
        cursor = await self.connection.execute("SELECT spotify_id FROM saved_library_album")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def get_saved_library_albums_by_id(self) -> Dict[str, SavedLibraryAlbum]:
        """Get all stored saved-library albums keyed by Spotify ID."""
        cursor = await self.connection.execute("""
            SELECT spotify_id, spotify_uri, spotify_url, title, normalized_title,
                   artists_json, normalized_artists_json, album_type, release_type,
                   cover_url, added_at, is_posted_listened, wordpress_post_id,
                   created_at, updated_at
            FROM saved_library_album
        """)
        rows = await cursor.fetchall()
        albums = [self._row_to_saved_library_album(row) for row in rows]
        return {album.spotify_id: album for album in albums}

    async def get_saved_library_snapshot_items(self) -> List[SavedLibrarySnapshotItem]:
        """Get the complete saved-library identity snapshot in Spotify order."""
        cursor = await self.connection.execute("""
            SELECT spotify_id, spotify_uri, added_at, position, last_seen_at
            FROM saved_library_snapshot_item
            ORDER BY position ASC
        """)
        rows = await cursor.fetchall()
        return [
            SavedLibrarySnapshotItem(
                spotify_id=row[0],
                spotify_uri=row[1],
                added_at=datetime.fromisoformat(row[2]),
                position=_as_int(row[3], "snapshot position"),
                last_seen_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        ]

    async def replace_saved_library_snapshot(self, items: List[SavedLibrarySnapshotItem]):
        """Replace the complete saved-library identity snapshot."""
        await self.connection.execute("DELETE FROM saved_library_snapshot_item")
        for item in items:
            await self.connection.execute("""
                INSERT INTO saved_library_snapshot_item
                (spotify_id, spotify_uri, added_at, position, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                item.spotify_id,
                item.spotify_uri,
                item.added_at.isoformat(),
                item.position,
                item.last_seen_at.isoformat(),
            ))
        await self.connection.commit()

    async def upsert_saved_library_album(self, album: SavedLibraryAlbum):
        """Insert or update a saved-library album."""
        now = datetime.now()
        created_at = album.created_at or now
        updated_at = album.updated_at or now

        await self.connection.execute("""
            INSERT INTO saved_library_album
            (spotify_id, spotify_uri, spotify_url, title, normalized_title,
             artists_json, normalized_artists_json, album_type, release_type,
             cover_url, added_at, is_posted_listened, wordpress_post_id,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spotify_id) DO UPDATE SET
                spotify_uri = excluded.spotify_uri,
                spotify_url = excluded.spotify_url,
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                artists_json = excluded.artists_json,
                normalized_artists_json = excluded.normalized_artists_json,
                album_type = excluded.album_type,
                release_type = excluded.release_type,
                cover_url = excluded.cover_url,
                added_at = excluded.added_at,
                is_posted_listened = excluded.is_posted_listened,
                wordpress_post_id = excluded.wordpress_post_id,
                updated_at = excluded.updated_at
        """, (
            album.spotify_id,
            album.spotify_uri,
            album.spotify_url,
            album.title,
            album.normalized_title,
            json.dumps(album.artists),
            json.dumps(album.normalized_artists),
            album.album_type,
            album.release_type.value,
            album.cover_url,
            album.added_at.isoformat(),
            album.is_posted_listened,
            album.wordpress_post_id,
            created_at.isoformat(),
            updated_at.isoformat(),
        ))
        await self.connection.commit()

    async def delete_saved_library_albums(self, spotify_ids: List[str]) -> int:
        """Delete saved-library albums by Spotify ID."""
        if not spotify_ids:
            return 0

        # pi-lens-ignore: python-sql-injection
        cursor = await self.connection.executemany(
            "DELETE FROM saved_library_album WHERE spotify_id = ?",
            [(spotify_id,) for spotify_id in spotify_ids],
        )
        await self.connection.commit()
        return cursor.rowcount

    async def mark_saved_library_album_posted(
        self,
        spotify_id: str,
        wordpress_post_id: Optional[int],
    ) -> bool:
        """Mark a saved-library album as posted/listened if it exists."""
        cursor = await self.connection.execute("""
            UPDATE saved_library_album
            SET is_posted_listened = 1,
                wordpress_post_id = ?,
                updated_at = ?
            WHERE spotify_id = ?
        """, (wordpress_post_id, datetime.now().isoformat(), spotify_id))
        await self.connection.commit()
        return cursor.rowcount > 0

    async def mark_saved_library_album_unposted(self, spotify_id: str) -> bool:
        """Clear the posted/listened state for a saved-library album if it exists."""
        cursor = await self.connection.execute("""
            UPDATE saved_library_album
            SET is_posted_listened = 0,
                wordpress_post_id = NULL,
                updated_at = ?
            WHERE spotify_id = ?
        """, (datetime.now().isoformat(), spotify_id))
        await self.connection.commit()
        return cursor.rowcount > 0

    async def get_random_unposted_saved_library_album(
        self, popularity_focus: int = 0
    ) -> Optional[RandomAlbumSelection]:
        """Pick every unposted album at focus zero or from a listener-ranked pool."""
        if not 0 <= popularity_focus <= 100:
            raise ValueError("Popularity focus must be between 0 and 100")
        if popularity_focus == 0:
            cursor = await self.connection.execute("""
            SELECT spotify_id, spotify_uri, spotify_url, title, normalized_title,
                   artists_json, normalized_artists_json, album_type, release_type,
                   cover_url, added_at, is_posted_listened, wordpress_post_id,
                   created_at, updated_at
            FROM saved_library_album
            WHERE is_posted_listened = 0
            ORDER BY RANDOM()
            LIMIT 1
            """)
            row = await cursor.fetchone()
            return (RandomAlbumSelection(
                album=self._row_to_saved_library_album(row), popularity_focus=0)
                if row else None)

        cursor = await self.connection.execute("""
            SELECT COUNT(*)
            FROM saved_library_album AS saved
            JOIN album_metadata_cache AS metadata ON metadata.spotify_id = saved.spotify_id
            WHERE saved.is_posted_listened = 0
              AND metadata.status = 'ready'
              AND metadata.resolver_version = ?
              AND metadata.lastfm_listeners IS NOT NULL
        """, (METADATA_RESOLVER_VERSION,))
        row = await cursor.fetchone()
        rankable_total = _as_int(row[0], "rankable album total") if row else 0
        pool_size = popularity_pool_size(rankable_total, popularity_focus)
        if pool_size == 0:
            return None

        cursor = await self.connection.execute("""
            WITH ranked AS (
                SELECT saved.*, metadata.lastfm_listeners,
                       ROW_NUMBER() OVER (
                           ORDER BY metadata.lastfm_listeners DESC, saved.spotify_id ASC
                       ) AS popularity_rank
                FROM saved_library_album AS saved
                JOIN album_metadata_cache AS metadata
                  ON metadata.spotify_id = saved.spotify_id
                WHERE saved.is_posted_listened = 0
                  AND metadata.status = 'ready'
                  AND metadata.resolver_version = ?
                  AND metadata.lastfm_listeners IS NOT NULL
            )
            SELECT * FROM ranked
            WHERE popularity_rank <= ?
            ORDER BY RANDOM()
            LIMIT 1
        """, (METADATA_RESOLVER_VERSION, pool_size))
        row = await cursor.fetchone()
        return (RandomAlbumSelection(
            album=self._row_to_saved_library_album(row),
            popularity_focus=popularity_focus,
            listener_count=row[15],
            popularity_rank=row[16],
            rankable_total=rankable_total,
            candidate_pool_size=pool_size,
        ) if row else None)

    async def get_saved_library_stats(self) -> SavedLibraryStats:
        """Return total and posted/listened saved-library counts."""
        cursor = await self.connection.execute("""
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_posted_listened THEN 1 ELSE 0 END), 0)
            FROM saved_library_album
        """)
        row = await cursor.fetchone()
        total = _as_int(row[0], "saved-library total") if row else 0
        posted_listened = _as_int(row[1], "saved-library posted count") if row else 0
        percent = (posted_listened / total) if total else 0.0
        return SavedLibraryStats(total=total, posted_listened=posted_listened, percent=percent)

    def _row_to_saved_library_album(self, row: Sequence[Any]) -> SavedLibraryAlbum:
        """Convert a saved-library database row to a model."""
        return SavedLibraryAlbum(
            spotify_id=row[0],
            spotify_uri=row[1],
            spotify_url=row[2],
            title=row[3],
            normalized_title=row[4],
            artists=_load_json_list(row[5]),
            normalized_artists=_load_json_list(row[6]),
            album_type=row[7],
            release_type=ReleaseType(row[8]),
            cover_url=row[9],
            added_at=datetime.fromisoformat(row[10]),
            is_posted_listened=bool(row[11]),
            wordpress_post_id=row[12],
            created_at=datetime.fromisoformat(row[13]) if row[13] else None,
            updated_at=datetime.fromisoformat(row[14]) if row[14] else None,
        )

    # Static provider metadata cache
    async def get_album_metadata_cache(
        self, spotify_id: str, resolver_version: Optional[int] = None
    ) -> Optional[CachedAlbumMetadata]:
        """Return a cached resolution, optionally requiring a current resolver version."""
        query = """
            SELECT spotify_id, status, lastfm_url, lastfm_mbid, genres_json,
                   lastfm_listeners, diagnostic_code, resolver_version, updated_at
            FROM album_metadata_cache
            WHERE spotify_id = ?
        """
        values: tuple[Any, ...] = (spotify_id,)
        if resolver_version is not None:
            query += " AND resolver_version = ?"
            values += (resolver_version,)
        cursor = await self.connection.execute(query, values)
        row = await cursor.fetchone()
        if not row:
            return None
        return CachedAlbumMetadata(
            spotify_id=row[0],
            status=row[1],
            lastfm_url=row[2],
            lastfm_mbid=row[3],
            genres=_load_json_list(row[4]),
            lastfm_listeners=row[5],
            diagnostic_code=row[6],
            resolver_version=row[7],
            updated_at=datetime.fromisoformat(row[8]),
        )

    async def save_album_metadata_cache(self, metadata: CachedAlbumMetadata) -> None:
        """Persist one completed provider resolution independently of other domains."""
        await self.connection.execute("""
            INSERT INTO album_metadata_cache
            (spotify_id, status, lastfm_url, lastfm_mbid, genres_json,
             lastfm_listeners, diagnostic_code, resolver_version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spotify_id) DO UPDATE SET
                status = excluded.status,
                lastfm_url = excluded.lastfm_url,
                lastfm_mbid = excluded.lastfm_mbid,
                genres_json = excluded.genres_json,
                lastfm_listeners = excluded.lastfm_listeners,
                diagnostic_code = excluded.diagnostic_code,
                resolver_version = excluded.resolver_version,
                updated_at = excluded.updated_at
        """, (
            metadata.spotify_id,
            metadata.status,
            metadata.lastfm_url,
            metadata.lastfm_mbid,
            json.dumps(metadata.genres),
            metadata.lastfm_listeners,
            metadata.diagnostic_code,
            metadata.resolver_version,
            metadata.updated_at.isoformat(),
        ))
        await self.connection.commit()

    async def get_unposted_saved_library_ids_missing_metadata(
        self, resolver_version: int
    ) -> List[str]:
        """Return unposted saved releases without a completed current resolution."""
        cursor = await self.connection.execute("""
            SELECT saved.spotify_id
            FROM saved_library_album AS saved
            LEFT JOIN album_metadata_cache AS metadata
              ON metadata.spotify_id = saved.spotify_id
             AND metadata.resolver_version = ?
            WHERE saved.is_posted_listened = 0
              AND metadata.spotify_id IS NULL
            ORDER BY saved.added_at DESC
        """, (resolver_version,))
        return [row[0] for row in await cursor.fetchall()]

    # Discord operations
    async def save_discord_prompt(self, prompt: DiscordPrompt):
        """Save Discord prompt."""
        created_at = prompt.created_at or datetime.now()
        data = (
            prompt.prompt_type,
            prompt.release_id,
            prompt.wordpress_post_id,
            prompt.discord_message_id,
            prompt.state,
            created_at.isoformat(),
            prompt.expires_at.isoformat() if prompt.expires_at else None,
            prompt.context_json,
        )
        # pi-lens-ignore: python-sql-injection
        await self.connection.execute("""
            INSERT INTO discord_prompt
            (prompt_type, release_id, wordpress_post_id, discord_message_id, state,
             created_at, expires_at, context_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, data)
        await self.connection.commit()

    async def get_discord_prompt(self, message_id: str) -> Optional[DiscordPrompt]:
        """Get Discord prompt by message ID."""
        cursor = await self.connection.execute("""
            SELECT id, prompt_type, release_id, wordpress_post_id, discord_message_id, state,
                   created_at, expires_at, context_json
            FROM discord_prompt WHERE discord_message_id = ?
        """, (message_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return DiscordPrompt(
            id=row[0],
            prompt_type=row[1],
            release_id=row[2],
            wordpress_post_id=row[3],
            discord_message_id=row[4],
            state=row[5],
            created_at=datetime.fromisoformat(row[6]) if row[6] else None,
            expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            context_json=row[8],
        )

    async def has_discord_prompt(self, release_id: str, prompt_type: str) -> bool:
        """Check whether a Discord prompt already exists for a release."""
        cursor = await self.connection.execute("""
            SELECT 1 FROM discord_prompt
            WHERE release_id = ? AND prompt_type = ?
            LIMIT 1
        """, (release_id, prompt_type))
        return await cursor.fetchone() is not None

    async def expire_stale_discord_prompts(
        self,
        release_id: str,
        prompt_type: str,
        now: Optional[datetime] = None,
    ):
        """Mark pending prompts as expired once their expiration timestamp has passed."""
        checked_at = now or datetime.now()
        await self.connection.execute("""
            UPDATE discord_prompt
            SET state = ?
            WHERE release_id = ?
              AND prompt_type = ?
              AND state = ?
              AND expires_at IS NOT NULL
              AND expires_at <= ?
        """, (
            "expired",
            release_id,
            prompt_type,
            "pending",
            checked_at.isoformat(),
        ))
        await self.connection.commit()

    async def get_live_discord_prompt(
        self,
        release_id: str,
        prompt_type: str,
        now: Optional[datetime] = None,
    ) -> Optional[DiscordPrompt]:
        """Return the newest pending prompt that has not expired."""
        checked_at = now or datetime.now()
        await self.expire_stale_discord_prompts(release_id, prompt_type, checked_at)

        cursor = await self.connection.execute("""
            SELECT id, prompt_type, release_id, wordpress_post_id, discord_message_id, state,
                   created_at, expires_at, context_json
            FROM discord_prompt
            WHERE release_id = ?
              AND prompt_type = ?
              AND state = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY id DESC LIMIT 1
        """, (release_id, prompt_type, "pending", checked_at.isoformat()))
        row = await cursor.fetchone()
        if not row:
            return None
        return DiscordPrompt(
            id=row[0],
            prompt_type=row[1],
            release_id=row[2],
            wordpress_post_id=row[3],
            discord_message_id=row[4],
            state=row[5],
            created_at=datetime.fromisoformat(row[6]) if row[6] else None,
            expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            context_json=row[8],
        )

    async def get_discord_prompt_by_release_and_type(self, release_id: str, prompt_type: str) -> Optional[DiscordPrompt]:
        """Get the latest Discord prompt for a release and prompt type."""
        cursor = await self.connection.execute("""
            SELECT id, prompt_type, release_id, wordpress_post_id, discord_message_id, state,
                   created_at, expires_at, context_json
            FROM discord_prompt
            WHERE release_id = ? AND prompt_type = ?
            ORDER BY id DESC LIMIT 1
        """, (release_id, prompt_type))
        row = await cursor.fetchone()
        if not row:
            return None
        return DiscordPrompt(
            id=row[0],
            prompt_type=row[1],
            release_id=row[2],
            wordpress_post_id=row[3],
            discord_message_id=row[4],
            state=row[5],
            created_at=datetime.fromisoformat(row[6]) if row[6] else None,
            expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            context_json=row[8],
        )

    async def update_discord_prompt_state(self, message_id: str, state: str):
        """Update Discord prompt state."""
        await self.connection.execute("""
            UPDATE discord_prompt SET state = ? WHERE discord_message_id = ?
        """, (state, message_id))
        await self.connection.commit()

    # Audit events
    async def log_audit_event(self, event_type: str, data: Dict[str, Any]):
        """Log audit event."""
        import json
        await self.connection.execute("""
            INSERT INTO audit_event (event_type, data_json, timestamp)
            VALUES (?, ?, ?)
        """, (event_type, json.dumps(data), datetime.now().isoformat()))
        await self.connection.commit()

    # Service state
    async def save_service_state(self, key: str, value: str):
        """Save service state."""
        await self.connection.execute("""
            INSERT OR REPLACE INTO service_state (key, value)
            VALUES (?, ?)
        """, (key, value))
        await self.connection.commit()

    async def get_service_state(self, key: str) -> Optional[str]:
        """Get service state."""
        cursor = await self.connection.execute("""
            SELECT value FROM service_state WHERE key = ?
        """, (key,))
        row = await cursor.fetchone()
        return row[0] if row else None
        await self.connection.commit()
