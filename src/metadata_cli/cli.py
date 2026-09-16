"""Manual batch interface for the shared album metadata library."""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from album_metadata.common import (
    match_key, now_iso as _now_iso, raw_query, safe_error as _safe_error,
    write_json_atomic,
)
from album_metadata.enrichment import enrich, is_field_present  # pyright: ignore[reportMissingImports]
from album_metadata.lastfm import LastFM  # pyright: ignore[reportMissingImports]
from album_metadata.plans import (  # pyright: ignore[reportMissingImports]
    materialize_body, slice_items, validate_ignored, validate_plan,
    validate_unresolved,
)
from album_metadata.providers import ProviderCircuit, ProviderError
from album_metadata.schema import (
    AUTO_FILLABLE_FIELDS, PLAN_SCHEMA_VERSION, TAXONOMIES,
    UNRESOLVED_SCHEMA_VERSION, WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED,
)
from album_metadata.spotify import (
    Spotify, _is_true, choose_spotify_candidate, search_ladder,
    spotify_candidate_score,
)
from metadata_cli.wordpress import WordPress  # pyright: ignore[reportMissingImports]
from artist_images import (
    ARTIST_IMAGE_MAX_DIMENSION,
    ARTIST_IMAGE_PLAN_VERSION,
    artist_image_entry,
    artist_search_ladder,
    choose_artist_candidate,
    correction_row,
    image_is_missing,
    validate_artist_image_plan,
)

log = logging.getLogger("post_to_album")

def _ensure_term(wp: WordPress, cache: dict[str, dict[str, int]],
                 tax: str, name: str) -> int | None:
    if not name:
        return None
    cache.setdefault(tax, {})
    if name in cache[tax]:
        return cache[tax][name]
    # Probing by name: cached by a GET slug=neck won't work name→slug,
    # but we already did the broader pull. Try direct lookup on existing cache.
    if tax not in cache[tax]:
        cache[tax] = wp.list_tax_terms(tax)
    if name in cache[tax]:
        return cache[tax][name]
    new_id = wp.create_term(tax, name)
    if new_id:
        cache[tax][name] = new_id
    return new_id

def _resolve_terms(wp: Any, patches: list[dict]) -> dict[str, dict[str, int]]:
    wanted = {tax: {} for tax in TAXONOMIES}
    for patch in patches:
        for tax, names in patch["write"].get("taxonomies", {}).items():
            for name in names:
                wanted[tax].setdefault(match_key(name), name)
    resolved: dict[str, dict[str, int]] = {tax: {} for tax in TAXONOMIES}
    for tax in TAXONOMIES:
        existing = wp.list_tax_terms(tax)
        resolved[tax] = {match_key(name): term_id for name, term_id in existing.items()}
        for key, name in wanted[tax].items():
            if key not in resolved[tax]:
                term_id = wp.create_term(tax, name)
                if not term_id:
                    raise RuntimeError(f"Could not resolve taxonomy term {tax}/{name}")
                resolved[tax][key] = term_id
    return resolved


def apply_patches(wp: Any, patches: list[dict]) -> tuple[list[int], list[dict]]:
    term_ids = _resolve_terms(wp, patches)  # all resolution precedes the first post update
    succeeded, failed = [], []
    for patch in patches:
        try:
            wp.update_post(patch["post_id"], materialize_body(patch["write"], term_ids))
            succeeded.append(patch["post_id"])
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            failed.append({"post_id": patch["post_id"], "message": _safe_error(exc)})
    return succeeded, failed


def cmd_run(args, env) -> int:
    write_policy = (WRITE_OVERWRITE_MANAGED if getattr(args, "overwrite_managed", False)
                    else WRITE_FILL_ONLY)
    log.info("Planning write policy: %s", write_policy)
    if write_policy == WRITE_OVERWRITE_MANAGED:
        log.warning("Overwrite-managed mode: reviewed plans may replace existing managed data.")
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
    spt = Spotify(env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_CLIENT_SECRET"])
    lfm = LastFM(env["LASTFM_API_KEY"])
    tag_id_to_name: dict[int, str] = {}
    wp.list_tags(tag_id_to_name)
    release_type_terms = wp.list_tax_terms("release_type")
    planned, unresolved, ignored = [], [], []
    posts = slice_items(list(wp.list_posts(per_page=100)), args.offset, args.limit)
    for post in posts:
        result = enrich(post, spt, lfm, tag_id_to_name, write_policy,
                        release_type_terms=release_type_terms)
        if result is None:
            continue
        if "write" in result:
            planned.append(result)
        elif _is_true(result.get("ignored")):
            ignored.append({key: value for key, value in result.items() if key != "ignored"})
        else:
            unresolved.append(result)
    plan = {"schema_version": PLAN_SCHEMA_VERSION, "generated_at": _now_iso(),
            "write_policy": write_policy, "patches": planned}
    validate_plan(plan)
    out_dir = Path(args.out_dir)
    write_json_atomic(out_dir / "planned.json", plan)
    unresolved_file = validate_unresolved(
        {"schema_version": UNRESOLVED_SCHEMA_VERSION, "unresolved": unresolved})
    write_json_atomic(out_dir / "unresolved.json", unresolved_file)
    ignored_file = validate_ignored(
        {"schema_version": UNRESOLVED_SCHEMA_VERSION, "ignored": ignored})
    write_json_atomic(out_dir / "ignored.json", ignored_file)
    for client in (spt, lfm):
        circuit = getattr(client, "_circuit", None)
        if isinstance(circuit, ProviderCircuit) and circuit.request_counts:
            log.info("%s requests: %s", circuit.provider,
                     ", ".join(f"{name}={count}" for name, count in
                               sorted(circuit.request_counts.items())))
    circuit_states = [getattr(getattr(client, "_circuit", None), "is_open", False)
                      for client in (spt, lfm)]
    circuit_open = any(type(state) is bool and state for state in circuit_states)
    if args.apply and circuit_open:
        log.error("Provider circuit opened; refusing deprecated run --apply.")
        return 1
    if args.apply:
        log.warning("run --apply is deprecated; use apply-plan")
        succeeded, failed = apply_patches(wp, planned)
        write_json_atomic(out_dir / "applied.json", {
            "schema_version": PLAN_SCHEMA_VERSION, "plan": str(out_dir / "planned.json"),
            "applied_at": _now_iso(), "succeeded": succeeded, "failed": failed})
        return 1 if failed else 0
    return 1 if circuit_open else 0


def cmd_apply_plan(args, env) -> int:
    try:
        plan = validate_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read plan: {_safe_error(exc)}") from exc
    selected = slice_items(plan["patches"], args.offset, args.limit)
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
    succeeded, failed = apply_patches(wp, selected)
    out_dir = Path(args.out_dir) if args.out_dir else Path(args.plan).parent
    write_json_atomic(out_dir / "applied.json", {
        "schema_version": PLAN_SCHEMA_VERSION, "plan": str(args.plan), "applied_at": _now_iso(),
        "succeeded": succeeded, "failed": failed})
    return 1 if failed else 0


def cmd_stats(args, env) -> int:
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
    counts = {f: 0 for f in AUTO_FILLABLE_FIELDS}
    total_posts = 0
    fully_filled_posts = 0

    tax_term_present = {"artist": 0, "genre": 0, "release_type": 0}

    for post in wp.list_posts(per_page=100):
        total_posts += 1
        acf = post.get("acf") or {}
        post_filled = True
        for f in AUTO_FILLABLE_FIELDS:
            if is_field_present(f, acf.get(f)):
                counts[f] += 1
            else:
                post_filled = False
        if post_filled:
            fully_filled_posts += 1
        for tax in tax_term_present:
            if post.get(tax):
                tax_term_present[tax] += 1

    print(f"Total posts: {total_posts}")
    print(f"Fully filled: {fully_filled_posts}")
    print("Auto-fillable field fill count:")
    for f in AUTO_FILLABLE_FIELDS:
        print(f"  {f}: {counts[f]}")
    print("Posts with at least one term in each custom taxonomy:")
    for tax, n in tax_term_present.items():
        print(f"  {tax}: {n}")
    return 0


def cmd_fuzzy(args, env) -> int:
    spt = Spotify(env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_CLIENT_SECRET"])
    q_title = raw_query(args.title)
    q_artists = [raw_query(a) for a in args.artists if raw_query(a)]
    print(f"q_title={q_title!r}  q_artists={q_artists!r}")
    cands = search_ladder(spt, q_title, q_artists)
    for candidate in cands:
        row = spotify_candidate_score(candidate, q_title, q_artists)
        print(f"  score={row['score']:.3f} title={row['title_score']:.3f} "
              f"artist={row['artist_score']:.3f}  {candidate['id']}  "
              f"{candidate['name']!r}  by {[a['name'] for a in candidate.get('artists', [])]}")
    result = choose_spotify_candidate(cands, q_title, q_artists)
    print(f"\nResult: {result['reason']}; top pick: {result.get('candidate') or 'no winner'}")
    return 0


def cmd_artist_images(args, env) -> int:
    """Plan image writes for missing artist taxonomy fields only."""
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"],
                   env["WORDPRESS_APP_PASSWORD"])
    spotify = Spotify(env["SPOTIFY_CLIENT_ID"], env["SPOTIFY_CLIENT_SECRET"])
    missing = [
        term for term in wp.list_taxonomy_terms("artist")
        if image_is_missing(term)
    ]
    selected = slice_items(missing, args.offset, args.limit)
    images, corrections = [], []
    for term in selected:
        candidates: list[dict] = []
        try:
            candidates = artist_search_ladder(spotify, term["name"])
            match = choose_artist_candidate(candidates, term["name"])
            if match.get("candidate") is None:
                corrections.append(correction_row(term, match["reason"], candidates))
                continue
            entry = artist_image_entry(term["id"], term["name"], match)
            if entry is None:
                corrections.append(correction_row(
                    term, "spotify_artist_image_missing", candidates))
                continue
            images.append(entry)
        except ProviderError as exc:
            corrections.append({
                "term_id": term["id"],
                "term_name": term["name"],
                "diagnostic": exc.diagnostic("spotify_artist_provider_error"),
                "candidates": [],
            })

    plan = validate_artist_image_plan({
        "schema_version": ARTIST_IMAGE_PLAN_VERSION,
        "generated_at": _now_iso(),
        "max_dimension": ARTIST_IMAGE_MAX_DIMENSION,
        "images": images,
    })
    out_dir = Path(args.out_dir)
    write_json_atomic(out_dir / "artist-images-planned.json", plan)
    write_json_atomic(out_dir / "artist-images-corrections.json", {
        "schema_version": ARTIST_IMAGE_PLAN_VERSION,
        "generated_at": plan["generated_at"],
        "corrections": corrections,
    })
    print(
        f"Missing artist images: {len(missing)}; checked: {len(selected)}; "
        f"planned: {len(images)}; "
        f"needs correction: {len(corrections)}"
    )
    circuit = spotify._circuit
    if circuit.request_counts:
        log.info("Spotify requests: %s", ", ".join(
            f"{name}={count}" for name, count in sorted(circuit.request_counts.items())))
    return 1 if circuit.is_open else 0


def apply_artist_image_entries(
    wp: Any, entries: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Apply reviewed image entries without replacing images filled meanwhile."""
    succeeded, skipped, failed = [], [], []
    extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    for entry in entries:
        media_id = None
        try:
            current = wp.get_taxonomy_term("artist", entry["term_id"])
            if match_key(current.get("name", "")) != match_key(entry["term_name"]):
                raise ValueError("Artist term name changed after the plan was generated")
            if not image_is_missing(current):
                skipped.append({
                    "term_id": entry["term_id"],
                    "term_name": entry["term_name"],
                    "reason": "image_already_present",
                })
                continue
            content, content_type = wp.download_image(entry["image"]["url"])
            if content_type not in extensions:
                raise ValueError(f"Unsupported artist image content type: {content_type}")
            media = wp.upload_media_bytes(
                content,
                f"spotify-artist-{entry['spotify']['id']}.{extensions[content_type]}",
                content_type,
                f"{entry['term_name']} artist image",
            )
            media_id = media["id"]
            wp.update_taxonomy_term(
                "artist", entry["term_id"], {"acf": {"image": media_id}})
            persisted = wp.get_taxonomy_term("artist", entry["term_id"])
            if (persisted.get("acf") or {}).get("image") != media_id:
                raise RuntimeError("Artist image verification failed")
            succeeded.append({
                "term_id": entry["term_id"],
                "term_name": entry["term_name"],
                "spotify_artist_id": entry["spotify"]["id"],
                "media_id": media_id,
            })
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            if media_id is not None:
                try:
                    wp.delete_media(media_id)
                except (OSError, RuntimeError, ValueError, KeyError):
                    log.warning("Could not remove orphaned artist media %s", media_id)
            failed.append({
                "term_id": entry["term_id"],
                "term_name": entry["term_name"],
                "message": _safe_error(exc),
            })
    return succeeded, skipped, failed


def cmd_apply_artist_images(args, env) -> int:
    try:
        plan = validate_artist_image_plan(
            json.loads(Path(args.plan).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read artist image plan: {_safe_error(exc)}") from exc
    selected = slice_items(plan["images"], args.offset, args.limit)
    wp = WordPress(env["WORDPRESS_BASE_URL"], env["WORDPRESS_USERNAME"],
                   env["WORDPRESS_APP_PASSWORD"])
    succeeded, skipped, failed = apply_artist_image_entries(wp, selected)
    out_dir = Path(args.out_dir) if args.out_dir else Path(args.plan).parent
    write_json_atomic(out_dir / "artist-images-applied.json", {
        "schema_version": ARTIST_IMAGE_PLAN_VERSION,
        "plan": str(args.plan),
        "applied_at": _now_iso(),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    })
    print(
        f"Artist images applied: {len(succeeded)}; skipped: {len(skipped)}; "
        f"failed: {len(failed)}"
    )
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# .env loader
# --------------------------------------------------------------------------- #

def load_env(path: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if path:
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("WORDPRESS_BASE_URL", "WORDPRESS_URL", "WORDPRESS_USERNAME",
              "WORDPRESS_APP_PASSWORD", "LASTFM_API_KEY", "SPOTIFY_CLIENT_ID",
              "SPOTIFY_CLIENT_SECRET"):
        env.setdefault(k, os.environ.get(k, ""))
    env["WORDPRESS_BASE_URL"] = env["WORDPRESS_BASE_URL"] or env["WORDPRESS_URL"]
    return env


def require_env(env: dict[str, str], *names: str) -> dict[str, str]:
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    return env


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="post_to_album",
        description="Review and backfill WordPress album metadata from Spotify and Last.fm.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--env", default=".env", help="path to .env (default: .env)")
    base.add_argument("--quiet", action="store_true")
    base.add_argument("--verbose", "-v", action="store_true")

    run = sub.add_parser("run", parents=[base], help="process posts and dry-run or apply")
    run.add_argument("--all", action="store_true", help="(default) process all posts")
    run.add_argument("--limit", type=int, help="process at most N posts")
    run.add_argument("--offset", type=int, default=0, help="skip first M posts")
    run.add_argument("--dry-run", action="store_true", help="dump planned patches to ./out/ (default)")
    run.add_argument("--apply", action="store_true", help="write to WordPress")
    run.add_argument("--out-dir", default="out", help="directory for dry-run JSON")
    run.add_argument(
        "--overwrite-managed", action="store_true",
        help=("recompute and plan replacements for program-managed ACF fields and "
              "taxonomies even when populated; editor-owned rating, favorite, notes, "
              "and track highlights remain protected, and missing provider values "
              "never clear existing data"))

    apply_plan = sub.add_parser("apply-plan", parents=[base], help="validate and apply a saved plan")
    apply_plan.add_argument("plan")
    apply_plan.add_argument("--offset", type=int, default=0)
    apply_plan.add_argument("--limit", type=int)
    apply_plan.add_argument("--out-dir")

    stats = sub.add_parser("stats", parents=[base], help="report fill-rate before/after")

    fuzzy = sub.add_parser("fuzzy", parents=[base], help="debug-search Spotify for a (title, artists…) pair")
    fuzzy.add_argument("title")
    fuzzy.add_argument("artists", nargs="*")

    artist_images = sub.add_parser(
        "artist-images", parents=[base],
        help="plan images only for artist terms whose image field is missing")
    artist_images.add_argument("--limit", type=int, help="process at most N missing artist terms")
    artist_images.add_argument("--offset", type=int, default=0,
                               help="skip the first M missing artist terms")
    artist_images.add_argument("--out-dir", default="out", help="artifact directory (default: out)")

    apply_artist_images = sub.add_parser(
        "apply-artist-images", parents=[base],
        help="validate and apply a reviewed artist image plan")
    apply_artist_images.add_argument("plan", help="path to artist-images-planned.json")
    apply_artist_images.add_argument("--offset", type=int, default=0)
    apply_artist_images.add_argument("--limit", type=int)
    apply_artist_images.add_argument("--out-dir")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    env = load_env(args.env)
    wp_names = ("WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD")
    if args.cmd == "run":
        if not args.dry_run and not args.apply:
            args.dry_run = True
        if args.dry_run and args.apply:
            ap.error("--dry-run and --apply are mutually exclusive")
        return cmd_run(args, require_env(env, *wp_names, "SPOTIFY_CLIENT_ID",
                                         "SPOTIFY_CLIENT_SECRET", "LASTFM_API_KEY"))
    if args.cmd == "apply-plan":
        return cmd_apply_plan(args, require_env(env, *wp_names))
    if args.cmd == "stats":
        return cmd_stats(args, require_env(env, *wp_names))
    if args.cmd == "fuzzy":
        return cmd_fuzzy(args, require_env(env, "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"))
    if args.cmd == "artist-images":
        return cmd_artist_images(args, require_env(
            env, *wp_names, "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"))
    if args.cmd == "apply-artist-images":
        return cmd_apply_artist_images(args, require_env(env, *wp_names))
    ap.error("unknown subcommand")
    return 2
