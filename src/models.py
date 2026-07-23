"""
Data models for the application.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum

class ReleaseType(Enum):
    ALBUM = "Album"
    EP = "EP"
    SINGLE = "Single"
    COMPILATION = "Compilation"

class LifecycleStatus(Enum):
    ACTIVE = "active"
    AWAITING_75_DECISION = "awaiting_75_decision"
    AWAITING_RELISTEN_DECISION = "awaiting_relisten_decision"  # Legacy value; no new code should enter it.
    PUBLISHING = "publishing"
    PUBLISHED_RECENTLY = "published_recently"

class PromptType(Enum):
    PROMPT_75_PERCENT = "75_percent"
    PROMPT_RELISTEN_APPROVAL = "relisten"
    PROMPT_UNDO = "undo"

class PromptState(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    USED = "used"
    EXPIRED = "expired"

@dataclass
class Artist:
    spotify_id: str
    name: str
    normalized_name: str

@dataclass
class Track:
    spotify_id: str
    title: str
    normalized_title: str
    duration_ms: int
    disc_number: int
    track_number: int
    is_countable: bool
    listened: bool
    listened_at: Optional[datetime] = None
    listened_source: Optional[str] = None
    explicit: bool = False
    highlight: bool = False

@dataclass
class Release:
    spotify_id: str
    title: str
    normalized_title: str
    artists: List[Artist]
    release_type: ReleaseType
    raw_spotify_type: str
    cover_url: str
    release_date: str
    total_tracks: int
    total_duration_ms: int
    tracks: List[Track]
    progress: float
    status: LifecycleStatus
    first_seen: datetime
    last_seen: datetime
    completed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    wordpress_post_id: Optional[int] = None
    wordpress_media_id: Optional[int] = None
    is_relisten: bool = False
    duplicate_state: Optional[str] = None
    duplicate_post_id: Optional[int] = None
    rating: Optional[int] = None
    favorite: bool = False
    notes: Optional[str] = None
    unreleased: bool = False

@dataclass
class PlaybackState:
    is_playing: bool
    shuffle_state: bool
    repeat_state: str
    context: Optional[Dict[str, Any]]
    item: Optional[Dict[str, Any]]
    progress_ms: int
    timestamp: int

@dataclass
class WordPressPost:
    id: int
    title: str
    normalized_title: str
    artists: List[str]
    normalized_artists: List[str]
    link: str

@dataclass
class SavedLibraryAlbum:
    spotify_id: str
    spotify_uri: str
    spotify_url: str
    title: str
    normalized_title: str
    artists: List[str]
    normalized_artists: List[str]
    album_type: str
    release_type: ReleaseType
    cover_url: str
    added_at: datetime
    is_posted_listened: bool = False
    wordpress_post_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

@dataclass
class SavedLibrarySnapshotItem:
    spotify_id: str
    spotify_uri: str
    added_at: datetime
    position: int
    last_seen_at: datetime

@dataclass
class SavedLibraryStats:
    total: int
    posted_listened: int
    percent: float

@dataclass
class SavedLibrarySyncResult:
    skipped: bool
    total_seen: int
    stored_total: int
    added_or_updated: int = 0
    removed: int = 0
    message: str = ""

@dataclass
class DiscordPrompt:
    id: int
    prompt_type: str
    discord_message_id: str
    state: str
    release_id: Optional[str] = None
    wordpress_post_id: Optional[int] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    context_json: Optional[str] = None


@dataclass
class PublishResult:
    post: Dict[str, Any]
    scf_pending_tags: List[str]
    listen_count: int
    scf_attempted: bool = True
