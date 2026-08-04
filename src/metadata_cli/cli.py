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
from album_metadata.providers import ProviderCircuit
from album_metadata.schema import (
    AUTO_FILLABLE_FIELDS, PLAN_SCHEMA_VERSION, TAXONOMIES,
    UNRESOLVED_SCHEMA_VERSION, WRITE_FILL_ONLY, WRITE_OVERWRITE_MANAGED,
)
from album_metadata.spotify import (
    Spotify, _is_true, choose_spotify_candidate, search_ladder,
    spotify_candidate_score,
)
from metadata_cli.wordpress import WordPress  # pyright: ignore[reportMissingImports]

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
    for k in ("WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD",
              "LASTFM_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        env.setdefault(k, os.environ.get(k, ""))
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
    ap = argparse.ArgumentParser(prog="post_to_album",
                                 description="Verbose Python CLI to backfill SCF music metadata "
                                             "from Spotify (album + tracks) and Last.fm (genre + mood).")
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
    ap.error("unknown subcommand")
    return 2
