"""Small provider-independent helpers shared by both metadata interfaces."""

import difflib
import html
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_COMPARISON_PUNCTUATION = str.maketrans({
    "‘": "'",
    "’": "'",
    "‐": "-",
    "‑": "-",
    "–": "-",
    "—": "-",
})


def raw_query(value: str) -> str:
    """Prepare a provider query without erasing release identity."""
    return html.unescape(value or "").strip()


def match_key(value: str) -> str:
    """Normalize only for comparison, never for queries or stored values."""
    normalized = unicodedata.normalize("NFC", html.unescape(value or ""))
    return " ".join(normalized.translate(_COMPARISON_PUNCTUATION).casefold().split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, match_key(a), match_key(b)).ratio()


def _norm_title(value: str) -> str:
    """Compatibility alias for the old CLI debug helper."""
    return match_key(value)


def _norm_artist(value: str) -> str:
    """Compatibility alias for the old CLI debug helper."""
    return match_key(value)


def post_dmy(date_iso: str) -> str:
    """Convert WordPress/Spotify date precision variants to ``dd/mm/YYYY``."""
    if not date_iso:
        return ""
    value = date_iso.strip()
    try:
        if len(value) == 4 and value.isdigit():
            parsed = datetime(int(value), 1, 1)
        elif len(value) == 7 and re.fullmatch(r"\d{4}-\d{2}", value):
            parsed = datetime.strptime(value + "-01", "%Y-%m-%d")
        else:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00").replace("T", " ") if "T" in value else value
            )
        return parsed.strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_error(exc: BaseException) -> str:
    """Keep operational diagnostics concise and avoid response/credential dumps."""
    text = " ".join(str(exc).split())
    return (text[:197] + "...") if len(text) > 200 else text


def write_json_atomic(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def compute_release_type(tracks: list[dict], raw_spotify_type: str) -> str:
    """Classify a Spotify release using the shared WordPress policy."""
    if (raw_spotify_type or "").lower() == "compilation":
        return "Compilation"
    count = len(tracks)
    total_ms = sum(track.get("duration_ms", 0) for track in tracks)
    longest_ms = max((track.get("duration_ms", 0) for track in tracks), default=0)
    if count >= 7 or total_ms >= 1_800_000:
        return "Album"
    if (4 <= count <= 6 and total_ms < 1_800_000) or (
        1 <= count <= 3 and longest_ms >= 600_000
    ):
        return "EP"
    if 1 <= count <= 3 and total_ms < 1_800_000 and longest_ms < 600_000:
        return "Single"
    return "Album"
