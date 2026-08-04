# Spotify Album Blog Tracker

Tracks Spotify listening, publishes releases to WordPress, and provides a manual metadata CLI. Both interfaces use the same Spotify/Last.fm enrichment and WordPress payload code.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. WordPress must expose the SCF fields and the `artist`, `genre`, and `release_type` taxonomies defined by `scf-export-2026-07-24.json`.

## Tracker

```bash
PYTHONPATH=src python3 main.py
```

The tracker monitors playback, avoids duplicate posts, manages a saved-album queue, and sends Discord controls. Important commands are `/inprogress`, `/current`, `/random`, `/search`, and `/editor`.

Docker keeps the same service entry point and persistent `data/` and `logs/` volumes:

```bash
docker compose up --build -d
```

## Manual metadata CLI

The CLI is dry-run-first:

```bash
python3 post_to_album.py stats
python3 post_to_album.py fuzzy "Album title" "Artist"
python3 post_to_album.py run                 # dry run to out/
python3 post_to_album.py run --limit 10 --out-dir out
python3 post_to_album.py apply-plan out/planned.json
```

Review these files before applying a plan:

- `planned.json`: validated WordPress updates
- `unresolved.json`: releases requiring attention
- `ignored.json`: safely skipped releases
- `applied.json`: apply results

`run --apply` remains available for compatibility but is deprecated. Use `apply-plan` for a reviewable, replay-safe workflow. The CLI accepts both `WORDPRESS_URL` and its legacy `WORDPRESS_BASE_URL` alias.

## Metadata ownership

The shared engine manages provider-derived SCF fields, categories, and custom taxonomies. Rating, favorite, notes, and track highlights remain editor-owned. The tracker uses the known Spotify release ID; the CLI discovers an identity from the WordPress title and artist tags, then both follow the same validation and enrichment path.

Code is split into three parts:

- `src/album_metadata/`: reusable schema, providers, matching, enrichment, and payloads
- `src/tracker_metadata.py`: tracker adapter
- `src/metadata_cli/`: manual CLI adapter

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall -q main.py post_to_album.py src tests
docker compose config
```
