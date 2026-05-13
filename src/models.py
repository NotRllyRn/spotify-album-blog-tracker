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
    PUBLISHED = "published"
    DELETED = "deleted"
    IGNORED_SINGLE = "ignored_single"
    TRASHED_POST = "trashed_post"

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
