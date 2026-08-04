"""Validated metadata plans and deterministic WordPress body materialization."""

import math
from datetime import datetime
from typing import Any

from album_metadata.common import match_key, safe_error as _safe_error
from album_metadata.lastfm import _UUID_RE, _is_http_url
from album_metadata.schema import (
    APPROVED_ACF_TYPES, DIAGNOSTIC_CODES, PLAN_SCHEMA_VERSION,
    PROVIDER_FAILURE_KINDS, RELEASE_TYPES, TAXONOMIES, TRACK_KEYS,
    UNRESOLVED_SCHEMA_VERSION, WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED,
)

def _plan_error(path: str, message: str) -> None:
    raise ValueError(f"Invalid plan at {path}: {message}")


def _exact(
    obj: Any,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str],
    path: str,
) -> None:
    if not isinstance(obj, dict) or set(obj) - required - optional or required - set(obj):
        _plan_error(path, "unsupported or missing keys")


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _score_value(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_plan(plan: Any) -> dict:
    """Recursively validate the complete artifact before slicing or writes."""
    # Identify old/unknown artifacts before enforcing the current root shape so
    # callers get an actionable error even when an older root lacks new keys.
    if (isinstance(plan, dict) and type(plan.get("schema_version")) is int and
            plan["schema_version"] != PLAN_SCHEMA_VERSION):
        raise ValueError(
            f"Unsupported plan schema version {plan['schema_version']}. "
            "Regenerate the plan with the current CLI before applying it.")
    _exact(plan, {"schema_version", "generated_at", "write_policy", "patches"}, set(), "root")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != PLAN_SCHEMA_VERSION:
        _plan_error("schema_version", "unsupported version")
    if plan["write_policy"] not in (WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED):
        _plan_error("write_policy", "unsupported policy")
    if not isinstance(plan["generated_at"], str) or not plan["generated_at"]:
        _plan_error("generated_at", "must be nonempty")
    if not isinstance(plan["patches"], list): _plan_error("patches", "must be list")
    seen: set[int] = set()
    for index, patch in enumerate(plan["patches"]):
        path = f"patches[{index}]"
        _exact(patch, {"post_id", "post_title", "matches", "write", "diagnostics"}, {"source_modified"}, path)
        if not _positive_int(patch["post_id"]) or patch["post_id"] in seen:
            _plan_error(path + ".post_id", "must be a unique positive integer")
        seen.add(patch["post_id"])
        if not isinstance(patch["post_title"], str): _plan_error(path + ".post_title", "must be string")
        if "source_modified" in patch and patch["source_modified"] is not None and not isinstance(patch["source_modified"], str): _plan_error(path + ".source_modified", "must be string or null")
        _exact(patch["matches"], {"spotify", "lastfm"}, set(), path + ".matches")
        spotify = patch["matches"]["spotify"]
        _exact(spotify, {"id", "title", "artists", "score"}, set(), path + ".matches.spotify")
        if (not all(isinstance(spotify[k], str) and spotify[k] for k in ("id", "title")) or
                not isinstance(spotify["artists"], list) or not spotify["artists"] or
                not all(isinstance(x, str) and x for x in spotify["artists"]) or
                not _score_value(spotify["score"])): _plan_error(path + ".matches.spotify", "invalid evidence")
        lastfm = patch["matches"]["lastfm"]
        _exact(lastfm, {"title", "artist", "score"}, {"url", "mbid", "track_overlap"}, path + ".matches.lastfm")
        if (not all(isinstance(lastfm[k], str) and lastfm[k] for k in ("title", "artist")) or
                not _score_value(lastfm["score"])): _plan_error(path + ".matches.lastfm", "invalid evidence")
        if "track_overlap" in lastfm and not _score_value(lastfm["track_overlap"]): _plan_error(path + ".matches.lastfm.track_overlap", "invalid score")
        if "url" in lastfm and (not isinstance(lastfm["url"], str) or not _is_http_url(lastfm["url"])): _plan_error(path + ".matches.lastfm.url", "must be an HTTP(S) URL")
        if "mbid" in lastfm and (not isinstance(lastfm["mbid"], str) or not _UUID_RE.fullmatch(lastfm["mbid"])): _plan_error(path + ".matches.lastfm.mbid", "must be a valid MBID")
        write = patch["write"]
        _exact(write, set(), {"acf", "categories", "taxonomies"}, path + ".write")
        if not write: _plan_error(path + ".write", "must be nonempty")
        if "acf" in write:
            if not isinstance(write["acf"], dict) or not write["acf"]: _plan_error(path + ".write.acf", "must be nonempty object")
            for key, value in write["acf"].items():
                if key not in APPROVED_ACF_TYPES or type(value) is not APPROVED_ACF_TYPES[key]: _plan_error(path + ".write.acf." + key, "unknown or wrong type")
                if isinstance(value, str) and not value: _plan_error(path + ".write.acf." + key, "must be nonempty")
                if key == "lastfm_url" and not _is_http_url(value):
                    _plan_error(path + ".write.acf.lastfm_url", "must be an HTTP(S) URL")
                if key == "mbid" and not _UUID_RE.fullmatch(value):
                    _plan_error(path + ".write.acf.mbid", "must be a valid MBID")
                if key in ("music_release_date", "music_listened_at"):
                    try:
                        canonical_date = datetime.strptime(value, "%d/%m/%Y").strftime("%d/%m/%Y")
                    except ValueError:
                        canonical_date = ""
                    if canonical_date != value:
                        _plan_error(path + ".write.acf." + key, "must be a valid dd/mm/YYYY date")
            if "music_tracks" in write["acf"] and not write["acf"]["music_tracks"]:
                # Repeater replacement with an empty list would clear existing tracks.
                _plan_error(path + ".write.acf.music_tracks", "must be nonempty")
            for row in write["acf"].get("music_tracks", []):
                _exact(row, TRACK_KEYS, set(), path + ".write.acf.music_tracks[]")
                if (not all(_positive_int(row[k]) for k in ("disc_number", "track_number", "duration_ms")) or
                    type(row["highlight"]) is not bool or type(row["explicit"]) is not bool or
                    not all(isinstance(row[k], str) and row[k] for k in ("title", "spotify_id"))): _plan_error(path + ".write.acf.music_tracks[]", "invalid row")
        if "categories" in write:
            values = write["categories"]
            if not isinstance(values, list) or not values or not all(_positive_int(x) for x in values) or len(values) != len(set(values)): _plan_error(path + ".write.categories", "invalid replacement")
        if "taxonomies" in write:
            taxes = write["taxonomies"]
            if not isinstance(taxes, dict) or not taxes or set(taxes) - set(TAXONOMIES): _plan_error(path + ".write.taxonomies", "invalid object")
            for tax, names in taxes.items():
                if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names) or len(names) != len({match_key(n) for n in names}): _plan_error(path + ".write.taxonomies." + tax, "invalid names")
            if "release_type" in taxes and (len(taxes["release_type"]) != 1 or taxes["release_type"][0] not in RELEASE_TYPES): _plan_error(path + ".write.taxonomies.release_type", "invalid value")
        if not isinstance(patch["diagnostics"], list): _plan_error(path + ".diagnostics", "must be list")
        for diagnostic in patch["diagnostics"]:
            _exact(diagnostic, {"code", "message"}, set(), path + ".diagnostics[]")
            if diagnostic["code"] not in DIAGNOSTIC_CODES or not isinstance(diagnostic["message"], str) or not diagnostic["message"]: _plan_error(path + ".diagnostics[]", "invalid diagnostic")
    return plan


def _validate_provider_details(details: Any, code: str, path: str) -> None:
    required = {"provider", "operation", "failure_kind", "retryable",
                "attempts", "circuit_state"}
    _exact(details, required, {"http_status"}, path)
    provider = "spotify" if code == "spotify_provider_error" else "lastfm"
    if details["provider"] != provider:
        _plan_error(path + ".provider", "must agree with diagnostic code")
    if not isinstance(details["operation"], str) or not details["operation"]:
        _plan_error(path + ".operation", "must be nonempty")
    if details["failure_kind"] not in PROVIDER_FAILURE_KINDS:
        _plan_error(path + ".failure_kind", "unsupported value")
    if type(details["retryable"]) is not bool:
        _plan_error(path + ".retryable", "must be boolean")
    if type(details["attempts"]) is not int or details["attempts"] < 0:
        _plan_error(path + ".attempts", "must be a nonnegative integer")
    is_circuit = details["failure_kind"] == "circuit_open"
    if is_circuit != (details["attempts"] == 0):
        _plan_error(path + ".attempts", "zero is reserved for an open circuit")
    if details["circuit_state"] not in {"closed", "open"}:
        _plan_error(path + ".circuit_state", "unsupported value")
    if is_circuit and details["circuit_state"] != "open":
        _plan_error(path + ".circuit_state", "open circuit must report open")
    has_status = "http_status" in details
    if has_status != (details["failure_kind"] == "http_status"):
        _plan_error(path + ".http_status", "required only for HTTP status failures")
    if has_status and (type(details["http_status"]) is not int or
                       not 100 <= details["http_status"] <= 599):
        _plan_error(path + ".http_status", "must be an HTTP status integer")
    if has_status:
        expected_retryable = (details["http_status"] == 429 or
                              500 <= details["http_status"] <= 599)
        if details["retryable"] is not expected_retryable:
            _plan_error(path + ".retryable", "must agree with HTTP status")


def validate_ignored(value: Any) -> dict:
    _exact(value, {"schema_version", "ignored"}, set(), "root")
    validate_unresolved({"schema_version": value["schema_version"],
                         "unresolved": value["ignored"]})
    return value


def validate_unresolved(value: Any) -> dict:
    _exact(value, {"schema_version", "unresolved"}, set(), "root")
    if (type(value["schema_version"]) is not int or
            value["schema_version"] != UNRESOLVED_SCHEMA_VERSION):
        _plan_error("schema_version", "unsupported version")
    if not isinstance(value["unresolved"], list):
        _plan_error("unresolved", "must be list")
    seen = set()
    provider_codes = {"spotify_provider_error", "lastfm_provider_error"}
    for index, row in enumerate(value["unresolved"]):
        path = f"unresolved[{index}]"
        _exact(row, {"post_id", "post_title", "diagnostics"}, set(), path)
        if not _positive_int(row["post_id"]) or row["post_id"] in seen:
            _plan_error(path + ".post_id", "must be a unique positive integer")
        seen.add(row["post_id"])
        if not isinstance(row["post_title"], str):
            _plan_error(path + ".post_title", "must be string")
        if not isinstance(row["diagnostics"], list) or not row["diagnostics"]:
            _plan_error(path + ".diagnostics", "must be nonempty list")
        for diagnostic in row["diagnostics"]:
            code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
            required = {"code", "message", "details"} if code in provider_codes else {
                "code", "message"}
            _exact(diagnostic, required, set(), path + ".diagnostics[]")
            if (code not in DIAGNOSTIC_CODES or
                    not isinstance(diagnostic["message"], str) or not diagnostic["message"]):
                _plan_error(path + ".diagnostics[]", "invalid diagnostic")
            if code in provider_codes:
                _validate_provider_details(
                    diagnostic["details"], code, path + ".diagnostics[].details")
    return value


def slice_items(items: list, offset: int, limit: int | None) -> list:
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("offset and limit must be non-negative")
    return items[offset:] if limit is None else items[offset:offset + limit]


def materialize_body(write: dict, term_ids: dict[str, dict[str, int]]) -> dict:
    """Convert reviewed values to WordPress REST storage shapes."""
    body = {}
    if "acf" in write:
        body["acf"] = dict(write["acf"])
        for key in ("music_release_date", "music_listened_at"):
            if key in body["acf"]:
                body["acf"][key] = datetime.strptime(
                    body["acf"][key], "%d/%m/%Y").strftime("%Y%m%d")
    if "categories" in write: body["categories"] = list(write["categories"])
    for tax, names in write.get("taxonomies", {}).items():
        body[tax] = [term_ids[tax][match_key(name)] for name in names]
    return body
