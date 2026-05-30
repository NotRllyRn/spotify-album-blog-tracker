"""
Main tracking service.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from config import Config
from database import Database
from spotify_client import SpotifyClient
from models import PlaybackState, Release, Track, Artist, ReleaseType, LifecycleStatus, PromptType, PromptState, DiscordPrompt
from utils import normalize_text, normalize_artist_list, compute_release_type

logger = logging.getLogger(__name__)

RELISTEN_APPROVAL_TTL = timedelta(hours=24)
PUBLISHED_RELEASE_RETENTION = timedelta(hours=24)
PUBLISHED_RELEASE_CLEANUP_INTERVAL = timedelta(minutes=5)

class Tracker:
    def __init__(self, config: Config, db: Database, publisher=None, discord_bot=None):
        self.config = config
        self.db = db
        self.spotify = SpotifyClient(config)
        self.publisher = publisher
        self.discord_bot = discord_bot
        self.running = False

        # Polling intervals (seconds)
        self.active_interval = 3
        self.paused_interval = 8
        self.idle_interval = 15
        self.backoff_interval = 60
        self._last_published_cleanup_at: Optional[datetime] = None

    def set_discord_bot(self, discord_bot):
        self.discord_bot = discord_bot

    async def run(self):
        """Main tracking loop."""
        self.running = True
        logger.info("Starting tracker...")

        while self.running:
            try:
                await self._cleanup_published_releases_if_due()
                await self._poll_once()
            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)
                await asyncio.sleep(self.backoff_interval)

    async def stop(self):
        """Stop the tracker."""
        self.running = False
        await self.spotify.close()

    async def _poll_once(self):
        """Single poll iteration."""
        state_data = await self.spotify.get_playback_state()

        if state_data is None:
            # No active playback
            await self._handle_idle()
            return

        state = self._parse_playback_state(state_data)

        if not self._qualifies_for_tracking(state):
            await self._handle_non_qualifying(state)
            return

        album_id = state.item["album"]["id"]
        seen_at = datetime.now()
        release = await self.db.get_release(album_id)
        if await self._delete_published_release_if_expired(release, seen_at):
            release = None

        if release is None:
            candidate = await self._build_release_from_spotify(album_id)

            if candidate.release_type == ReleaseType.SINGLE:
                # Skip singles for auto-tracking
                await self._handle_non_qualifying(state)
                return

            release = await self._start_tracking_or_prompt_for_relisten(candidate)
            if release is None:
                await self._update_current_listening(state)
                await asyncio.sleep(self.active_interval)
                return

        release.last_seen = seen_at

        # Match and mark track
        track = self._match_track_to_release(release, state.item)

        if track and not track.listened:
            await self._mark_track_listened(track, "playback")
            await self._recompute_release_progress(release)

            # Check for prompts
            if release.progress >= 0.75 and not await self._has_75_prompt(release):
                await self._send_75_prompt(release)

            if release.progress >= 1.0:
                await self._handle_completion(release)
        else:
            await self.db.touch_release_last_seen(release.spotify_id, seen_at)

        await self._update_current_listening(state)
        await asyncio.sleep(self.active_interval)

    def _parse_playback_state(self, data: Dict[str, Any]) -> PlaybackState:
        """Parse Spotify playback state response."""
        return PlaybackState(
            is_playing=data.get("is_playing", False),
            shuffle_state=data.get("shuffle_state", False),
            repeat_state=data.get("repeat_state", "off"),
            context=data.get("context"),
            item=data.get("item"),
            progress_ms=data.get("progress_ms", 0),
            timestamp=data.get("timestamp", 0)
        )

    def _qualifies_for_tracking(self, state: PlaybackState) -> bool:
        """Check if playback qualifies for album tracking."""
        if not state.is_playing:
            return False

        if state.item is None:
            return False

        if state.item.get("type") != "track":
            return False
        
        if state.item.get("album") is None:
            return False
        
        if state.item["album"].get("album_type") != "album":
            return False

        if state.item.get("is_local", False):
            return False

        if state.context is None:
            return False

        if state.context.get("type") != "album":
            return False
        
        if state.context.get("uri") != state.item["album"].get("uri"):
            return False

        if state.shuffle_state:
            return False

        return True

    async def _get_or_create_release(self, album_id: str) -> Release:
        """Get an existing release or create one for explicit manual actions."""
        release = await self.db.get_release(album_id)
        if release:
            return release

        release = await self._build_release_from_spotify(album_id)
        return await self._create_tracked_release(release)

    async def _create_tracked_release(
        self,
        release: Release,
        is_relisten: bool = False,
        duplicate_post_id: Optional[int] = None,
    ) -> Release:
        """Persist a release once tracking has been allowed."""
        release.is_relisten = is_relisten
        release.duplicate_post_id = duplicate_post_id
        release.duplicate_state = "found" if is_relisten else "none"
        await self.db.save_release(release)
        await self.db.log_audit_event("release_created", {
            "spotify_id": release.spotify_id,
            "is_relisten": is_relisten,
            "duplicate_post_id": duplicate_post_id,
        })
        return release

    async def _start_tracking_or_prompt_for_relisten(self, release: Release) -> Optional[Release]:
        """Create a release unless it needs relisten approval first."""
        duplicate_post = await self._check_duplicate(release)
        if duplicate_post is None:
            return await self._create_tracked_release(release)

        live_prompt = await self.db.get_live_discord_prompt(
            release.spotify_id,
            PromptType.PROMPT_RELISTEN_APPROVAL.value,
        )
        if live_prompt is None:
            await self._send_relisten_approval_prompt(release, duplicate_post)

        return None

    async def _build_release_from_spotify(self, album_id: str) -> Release:
        """Build a release from Spotify without saving it."""
        # Fetch from Spotify
        album_data = await self.spotify.get_album(album_id)
        tracks_data = await self.spotify.get_album_tracks(album_id)

        # Parse artists
        artists = [
            Artist(
                spotify_id=a["id"],
                name=a["name"],
                normalized_name=normalize_text(a["name"].replace(",", ""))
            )
            for a in album_data["artists"]
        ]

        # Parse tracks
        tracks = []
        for i, t in enumerate(tracks_data):
            tracks.append(Track(
                spotify_id=t["id"],
                title=t["name"],
                normalized_title=normalize_text(t["name"]),
                duration_ms=t["duration_ms"],
                disc_number=t.get("disc_number", 1),
                track_number=t.get("track_number", i + 1),
                is_countable=not t.get("is_local", False) and t.get("is_playable", True),
                listened=False
            ))

        # Compute type
        release_type_str = compute_release_type(tracks_data, album_data["album_type"])
        release_type = ReleaseType(release_type_str)

        # Countable tracks only
        countable_tracks = [t for t in tracks if t.is_countable]
        total_duration = sum(t.duration_ms for t in countable_tracks)

        now = datetime.now()
        release = Release(
            spotify_id=album_id,
            title=album_data["name"],
            normalized_title=normalize_text(album_data["name"]),
            artists=artists,
            release_type=release_type,
            raw_spotify_type=album_data["album_type"],
            cover_url=album_data["images"][0]["url"] if album_data["images"] else "",
            release_date=album_data.get("release_date", ""),
            total_tracks=len(countable_tracks),
            total_duration_ms=total_duration,
            tracks=tracks,
            progress=0.0,
            status=LifecycleStatus.ACTIVE,
            first_seen=now,
            last_seen=now,
        )

        return release

    def _match_track_to_release(self, release: Release, item: Dict[str, Any]) -> Optional[Track]:
        """Match currently playing track to release track."""
        track_id = item["id"]
        for track in release.tracks:
            if track.spotify_id == track_id:
                return track
        return None

    async def _mark_track_listened(self, track: Track, source: str):
        """Mark a track as listened."""
        track.listened = True
        track.listened_at = datetime.now()
        track.listened_source = source

        # Update in DB - we'll save the whole release
        # For efficiency, could update just the track, but keeping simple

    async def _recompute_release_progress(self, release: Release):
        """Recompute release progress."""
        listened_count = sum(1 for t in release.tracks if t.is_countable and t.listened)
        countable_count = sum(1 for t in release.tracks if t.is_countable)

        if countable_count > 0:
            release.progress = listened_count / countable_count
        else:
            release.progress = 0.0

        if release.progress >= 1.0 and release.status == LifecycleStatus.ACTIVE:
            release.status = LifecycleStatus.PUBLISHING
            release.completed_at = datetime.now()

        await self.db.save_release(release)

    async def _has_75_prompt(self, release: Release) -> bool:
        """Check if 75% prompt has been sent."""
        if release.status == LifecycleStatus.AWAITING_75_DECISION:
            return True
        return await self.db.has_discord_prompt(release.spotify_id, PromptType.PROMPT_75_PERCENT.value)

    async def _send_75_prompt(self, release: Release):
        """Send 75% completion prompt."""
        release.status = LifecycleStatus.AWAITING_75_DECISION
        await self.db.save_release(release)
        await self.db.log_audit_event("75_percent_prompt_sent", {
            "spotify_id": release.spotify_id,
            "release_title": release.title
        })

        if self.discord_bot:
            message = await self.discord_bot.send_75_percent_prompt(release)
            if message:
                prompt = DiscordPrompt(
                    id=0,
                    prompt_type=PromptType.PROMPT_75_PERCENT.value,
                    release_id=release.spotify_id,
                    wordpress_post_id=None,
                    discord_message_id=str(message.id),
                    state=PromptState.PENDING.value
                )
                await self.db.save_discord_prompt(prompt)

        logger.info(f"75% prompt sent for {release.title}")

    async def _handle_completion(self, release: Release):
        """Handle release completion."""
        if release.status == LifecycleStatus.PUBLISHED_RECENTLY:
            return

        release.completed_at = datetime.now()
        await self._publish_release(release, as_relisten=release.is_relisten)

    async def _initialize_duplicate_state(self, release: Release) -> Optional[Any]:
        """Legacy helper for manual duplicate previews."""
        if release.duplicate_state is not None:
            return await self._get_cached_wordpress_post(release.duplicate_post_id)

        duplicate_post = await self._check_duplicate(release)
        if duplicate_post:
            release.duplicate_state = "found"
            release.duplicate_post_id = duplicate_post.id
            return duplicate_post

        release.duplicate_state = "none"
        release.duplicate_post_id = None
        return None

    async def _check_duplicate(self, release: Release) -> Optional[Any]:
        """Check if release is duplicate using normalized title and artist set."""
        from utils import normalize_artist_list
        
        # Get cached WordPress posts
        posts = await self.db.get_wordpress_posts()
        
        # Normalize release title and artists
        release_norm_title = release.normalized_title
        release_artists_set = set(normalize_artist_list([a.name for a in release.artists]))
        
        for post in posts:
            if post.normalized_title == release_norm_title:
                post_artists_set = set(post.normalized_artists)
                if post_artists_set == release_artists_set:
                    logger.info(f"Duplicate found: {post.title} (ID: {post.id})")
                    return post
        
        return None

    async def _get_cached_wordpress_post(self, post_id: Optional[int]) -> Optional[Any]:
        """Get a cached WordPress post by ID."""
        if post_id is None:
            return None

        posts = await self.db.get_wordpress_posts()
        for post in posts:
            if post.id == post_id:
                return post

        return None

    async def _send_relisten_approval_prompt(self, release: Release, duplicate_post: Any):
        """Ask the user before a duplicate album starts automatic tracking."""
        now = datetime.now()
        expires_at = now + RELISTEN_APPROVAL_TTL
        await self.db.log_audit_event("relisten_approval_prompt_sent", {
            "spotify_id": release.spotify_id,
            "release_title": release.title,
            "duplicate_post_id": duplicate_post.id,
        })

        if self.discord_bot:
            message = await self.discord_bot.send_relisten_tracking_prompt(
                release,
                duplicate_post,
                expires_at,
            )
            if message:
                prompt = DiscordPrompt(
                    id=0,
                    prompt_type=PromptType.PROMPT_RELISTEN_APPROVAL.value,
                    release_id=release.spotify_id,
                    wordpress_post_id=duplicate_post.id,
                    discord_message_id=str(message.id),
                    state=PromptState.PENDING.value,
                    created_at=now,
                    expires_at=expires_at,
                    context_json=json.dumps({
                        "duplicate_post_id": duplicate_post.id,
                        "duplicate_post_title": duplicate_post.title,
                    }),
                )
                await self.db.save_discord_prompt(prompt)

        logger.info(f"Relisten approval prompt sent for {release.title}")

    async def approve_relisten_tracking(self, prompt: DiscordPrompt) -> str:
        """Create or update tracking after the user approves relisten tracking."""
        if prompt.state != PromptState.PENDING.value:
            return "unavailable"

        now = datetime.now()
        if prompt.expires_at and prompt.expires_at <= now:
            await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.EXPIRED.value)
            return "expired"

        if not prompt.release_id:
            return "unavailable"

        context = json.loads(prompt.context_json or "{}")
        duplicate_post_id = prompt.wordpress_post_id or context.get("duplicate_post_id")
        release = await self.db.get_release(prompt.release_id)
        if release is None:
            release = await self._build_release_from_spotify(prompt.release_id)
            if release.release_type == ReleaseType.SINGLE:
                await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.DECLINED.value)
                return "not_trackable"
            await self._create_tracked_release(
                release,
                is_relisten=True,
                duplicate_post_id=duplicate_post_id,
            )
        else:
            release.is_relisten = True
            release.duplicate_state = "found"
            release.duplicate_post_id = duplicate_post_id
            await self.db.save_release(release)

        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.ACCEPTED.value)
        await self.db.log_audit_event("relisten_tracking_approved", {
            "spotify_id": release.spotify_id,
            "duplicate_post_id": duplicate_post_id,
        })
        return "tracking_started"

    async def publish_release_now(self, release: Release, as_relisten: bool = False) -> str:
        """Publish a release immediately from a Discord prompt action."""
        latest_release = await self.db.get_release(release.spotify_id)
        release_to_publish = latest_release or release
        as_relisten = as_relisten or release_to_publish.is_relisten
        if as_relisten and not release_to_publish.duplicate_post_id:
            release_to_publish.duplicate_post_id = release.duplicate_post_id

        if release_to_publish.status == LifecycleStatus.PUBLISHED_RECENTLY:
            return "already_published"

        if release_to_publish.status == LifecycleStatus.PUBLISHING:
            return "already_publishing"

        release_to_publish.status = LifecycleStatus.PUBLISHING
        if as_relisten:
            release_to_publish.is_relisten = True
            release_to_publish.duplicate_state = "found"
        await self.db.save_release(release_to_publish)
        await self._publish_release(release_to_publish, as_relisten=as_relisten)
        return "published"

    async def _publish_release(self, release: Release, as_relisten: bool = False):
        """Publish release to WordPress."""
        try:
            release.status = LifecycleStatus.PUBLISHING
            if as_relisten:
                release.is_relisten = True
                release.duplicate_state = "found"
            await self.db.save_release(release)

            # Publish via publisher
            post = await self.publisher.publish_release(release, as_relisten=as_relisten)

            release.status = LifecycleStatus.PUBLISHED_RECENTLY
            release.published_at = datetime.now()
            await self.db.save_release(release)
            await self.db.mark_saved_library_album_posted(release.spotify_id, post.get("id"))

            if self.discord_bot:
                await self.discord_bot.send_publish_notification(release, post)
                await self._warn_if_published_album_not_saved(release)

            await self.db.log_audit_event("release_published", {
                "spotify_id": release.spotify_id,
                "release_title": release.title,
                "wordpress_post_id": post["id"]
            })
            logger.info(f"Published {release.title} to WordPress post {post['id']}")

        except Exception as e:
            logger.error(f"Error publishing release {release.title}: {e}")
            release.status = LifecycleStatus.ACTIVE  # Reset status on failure
            await self.db.save_release(release)
            raise

    async def _warn_if_published_album_not_saved(self, release: Release):
        """Warn through Discord when a published album is not saved in Spotify library."""
        try:
            is_saved = await self.spotify.check_library_contains_album(release.spotify_id)
        except Exception as e:
            logger.error(f"Error checking saved-library state for {release.spotify_id}: {e}")
            return

        if not is_saved and self.discord_bot:
            await self.discord_bot.send_library_missing_notification(release)

    async def _cleanup_published_releases_if_due(self, now: Optional[datetime] = None):
        """Run post-publish retention cleanup at a bounded interval."""
        checked_at = now or datetime.now()
        last_cleanup = getattr(self, "_last_published_cleanup_at", None)
        if (
            last_cleanup is not None
            and checked_at - last_cleanup < PUBLISHED_RELEASE_CLEANUP_INTERVAL
        ):
            return

        await self._cleanup_published_releases(checked_at)
        self._last_published_cleanup_at = checked_at

    async def _cleanup_published_releases(self, now: Optional[datetime] = None) -> int:
        """Delete releases retained only for the post-publish grace window."""
        checked_at = now or datetime.now()
        cutoff = checked_at - PUBLISHED_RELEASE_RETENTION
        deleted_count = await self.db.delete_published_releases_older_than(cutoff)
        if deleted_count:
            await self.db.log_audit_event("published_release_cleanup", {
                "deleted_count": deleted_count,
                "cutoff": cutoff.isoformat(),
            })
            logger.info(f"Cleaned up {deleted_count} published release(s) older than {cutoff.isoformat()}")
        return deleted_count

    async def _delete_published_release_if_expired(
        self,
        release: Optional[Release],
        now: Optional[datetime] = None,
    ) -> bool:
        """Drop an expired retained release before treating it as current tracking state."""
        if release is None or release.status != LifecycleStatus.PUBLISHED_RECENTLY:
            return False

        checked_at = now or datetime.now()
        cutoff = checked_at - PUBLISHED_RELEASE_RETENTION
        if release.published_at is None or release.published_at > cutoff:
            return False

        deleted = await self.db.delete_release(release.spotify_id)
        if deleted:
            await self.db.log_audit_event("published_release_cleanup", {
                "deleted_count": 1,
                "spotify_id": release.spotify_id,
                "cutoff": cutoff.isoformat(),
            })
            logger.info(f"Cleaned up expired published release {release.spotify_id}")
        return True

    async def _handle_idle(self):
        """Handle idle state."""
        logger.debug("No active playback")
        if self.discord_bot:
            try:
                await self.discord_bot.update_presence(None)
            except Exception as e:
                logger.debug(f"Unable to update Discord presence for idle state: {e}")
        await asyncio.sleep(self.idle_interval)

    async def _handle_non_qualifying(self, state: PlaybackState):
        """Handle non-qualifying playback."""
        logger.debug("Non-qualifying playback state")
        if self.discord_bot:
            try:
                await self.discord_bot.update_presence(state)
            except Exception as e:
                logger.debug(f"Unable to update Discord presence for paused/non-qualifying state: {e}")
        await asyncio.sleep(self.paused_interval)

    async def _update_current_listening(self, state: PlaybackState):
        """Update current listening cache."""
        # Store for /current command access
        await self.db.save_service_state("current_playback_state", str(state))

        if self.discord_bot:
            try:
                await self.discord_bot.update_presence(state)
            except Exception as e:
                logger.debug(f"Unable to update Discord presence: {e}")
