"""Canonical WordPress SCF field ownership and taxonomy contract."""

from pathlib import Path

AUTO_FILLABLE_FIELDS = (
    "spotify_title",
    "music_tracks",
    "music_length_ms",
    "spotify_album_id",
    "spotify_album_url",
    "music_release_date",
    "music_listened_at",
    "lastfm_url",
    "mbid",
    "music_total_tracks",
    "music_avg_track_ms",
    "music_explicit",
    "listen_count",
)

EDITOR_OWNED_ACF_FIELDS = frozenset({
    "music_rating",
    "music_favorite",
    "music_notes",
})

REMOVED_ACF_FIELDS = frozenset({
    "lastfm_release_id",
    "music_mood_tags",
    "unreleased",
    "listen-count",
})

TRACK_KEYS = frozenset({
    "disc_number",
    "track_number",
    "title",
    "duration_ms",
    "spotify_id",
    "highlight",
    "explicit",
})

APPROVED_ACF_TYPES = {
    "spotify_title": str,
    "music_tracks": list,
    "music_length_ms": int,
    "spotify_album_id": str,
    "spotify_album_url": str,
    "music_release_date": str,
    "music_listened_at": str,
    "lastfm_url": str,
    "mbid": str,
    "music_total_tracks": int,
    "music_avg_track_ms": int,
    "music_explicit": bool,
    "listen_count": int,
}

WRITE_FILL_ONLY = "fill_only"
WRITE_OVERWRITE_MANAGED = "overwrite_managed"

CATEGORY_MAP = {
    "Album": 6,
    "EP": 7,
    "Single": 5,
    "Compilation": 98,
}

TAXONOMIES = ("artist", "genre", "release_type")
RELEASE_TYPES = frozenset(CATEGORY_MAP)

LFM_BLOCKLIST = (
    r"^\d{4}$",
    r"^aoty$",
    r"^best of \d{4}$",
    r"^seen live$",
    r"^favorites?$",
    r"^under \d+$",
)

PLAN_SCHEMA_VERSION = 2
UNRESOLVED_SCHEMA_VERSION = 3
TAG_CACHE_PATH = Path("out/tag-cache.json")
TAG_CACHE_MAX_AGE = 24 * 60 * 60

DIAGNOSTIC_CODES = frozenset({
    "spotify_missing_artist",
    "spotify_no_results",
    "spotify_low_confidence",
    "spotify_catalog_unavailable",
    "spotify_ambiguous",
    "spotify_provider_error",
    "lastfm_no_results",
    "lastfm_catalog_unavailable",
    "lastfm_low_confidence",
    "lastfm_ambiguous",
    "lastfm_provider_error",
    "lastfm_identity_mismatch",
    "lastfm_no_mbid",
    "lastfm_track_mismatch",
    "lastfm_no_tags",
    "lastfm_lookup_fallback",
    "lastfm_collaboration_lookup",
    "lastfm_stale_tracks",
    "lastfm_transliteration_alignment",
})

PROVIDER_FAILURE_KINDS = frozenset({
    "http_status",
    "timeout",
    "network",
    "malformed_response",
    "api_error",
    "circuit_open",
    "unexpected",
})
