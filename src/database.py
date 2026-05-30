"""
Database layer using aiosqlite.
"""

import aiosqlite
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any, Set
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
    SavedLibraryStats,
)

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.db_path
        self.connection: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Initialize database and run migrations."""
        self.connection = await aiosqlite.connect(self.db_path)
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")

        # Run migrations
        await self._run_migrations()

    async def close(self):
        """Close database connection."""
        if self.connection:
            await self.connection.close()

    async def _run_migrations(self):
        """Run database migrations."""
        migrations_dir = self.config.project_root / "migrations"
        if not migrations_dir.exists():
            return

        # Get current version
        version = await self._get_schema_version()

        # Run pending migrations
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            migration_version = int(migration_file.stem.split("_")[0])
            if migration_version > version:
                logger.info(f"Running migration {migration_file.name}")
                sql = migration_file.read_text()
                await self.connection.executescript(sql)
                await self._set_schema_version(migration_version)

    async def _get_schema_version(self) -> int:
        """Get current schema version."""
        try:
            cursor = await self.connection.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = await cursor.fetchone()
            return row[0] if row else 0
        except aiosqlite.OperationalError:
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
        )

        cursor = await self.connection.execute("""
            INSERT OR REPLACE INTO release_lifecycle
            (spotify_id, title, normalized_title, release_type, raw_spotify_type,
             cover_url, release_date, total_tracks, total_duration_ms, progress, status,
             first_seen, last_seen, completed_at, published_at, wordpress_post_id,
             wordpress_media_id, is_relisten, duplicate_state, duplicate_post_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)

        release_id = cursor.lastrowid

        # Save artists
        await self._save_release_artists(release_id, release.artists)

        # Save tracks
        await self._save_release_tracks(release_id, release.tracks)

        await self.connection.commit()
        return release_id

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

        placeholders = ", ".join("?" for _ in spotify_ids)
        await self.connection.execute(
            f"DELETE FROM discord_prompt WHERE release_id IN ({placeholders})",
            spotify_ids,
        )
        await self.connection.execute(
            f"DELETE FROM release_lifecycle WHERE spotify_id IN ({placeholders})",
            spotify_ids,
        )
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
                   is_countable, listened, listened_at, listened_source
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
            listened_source=row[9]
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
                 track_number, is_countable, listened, listened_at, listened_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                release_id, track.spotify_id, track.title, track.normalized_title,
                track.duration_ms, track.disc_number, track.track_number,
                track.is_countable, track.listened,
                track.listened_at.isoformat() if track.listened_at else None,
                track.listened_source
            ))

    def _row_to_release(self, row: tuple, artists: List[Artist], tracks: List[Track]) -> Release:
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
            artists=json.loads(row[3]),
            normalized_artists=json.loads(row[4]),
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

        placeholders = ", ".join("?" for _ in spotify_ids)
        cursor = await self.connection.execute(
            f"DELETE FROM saved_library_album WHERE spotify_id IN ({placeholders})",
            spotify_ids,
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

    async def get_random_unposted_saved_library_album(self) -> Optional[SavedLibraryAlbum]:
        """Return a random saved-library album that has not been posted/listened."""
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
        return self._row_to_saved_library_album(row) if row else None

    async def get_saved_library_stats(self) -> SavedLibraryStats:
        """Return total and posted/listened saved-library counts."""
        cursor = await self.connection.execute("""
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_posted_listened THEN 1 ELSE 0 END), 0)
            FROM saved_library_album
        """)
        row = await cursor.fetchone()
        total = int(row[0] or 0)
        posted_listened = int(row[1] or 0)
        percent = (posted_listened / total) if total else 0.0
        return SavedLibraryStats(total=total, posted_listened=posted_listened, percent=percent)

    def _row_to_saved_library_album(self, row: tuple) -> SavedLibraryAlbum:
        """Convert a saved-library database row to a model."""
        return SavedLibraryAlbum(
            spotify_id=row[0],
            spotify_uri=row[1],
            spotify_url=row[2],
            title=row[3],
            normalized_title=row[4],
            artists=json.loads(row[5]),
            normalized_artists=json.loads(row[6]),
            album_type=row[7],
            release_type=ReleaseType(row[8]),
            cover_url=row[9],
            added_at=datetime.fromisoformat(row[10]),
            is_posted_listened=bool(row[11]),
            wordpress_post_id=row[12],
            created_at=datetime.fromisoformat(row[13]) if row[13] else None,
            updated_at=datetime.fromisoformat(row[14]) if row[14] else None,
        )

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
