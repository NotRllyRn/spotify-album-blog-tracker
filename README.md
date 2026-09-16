# Spotify Album Blog Tracker

Tracks Spotify listening, publishes releases to WordPress, and provides a manual metadata CLI. Both interfaces use the same Spotify/Last.fm enrichment and WordPress payload code.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. Metadata enrichment is enabled by default and requires Last.fm credentials; set `SPOTIFY_BLOG_TRACKER_FILL_SCF=0` to disable it. WordPress must expose the SCF fields and the `artist`, `genre`, and `release_type` taxonomies defined by `scf-export-2026-09-16.json`.

To refresh the frontend immediately after a publish, set `MUSICBLOG_WEBHOOK_URL` to its full `/api/wordpress/webhook` URL and set `WORDPRESS_WEBHOOK_SECRET` to the same long, random value used by the frontend. If either value is absent, webhook delivery is disabled. A delivery failure is logged without retrying the already-created WordPress post.

## Tracker

```bash
PYTHONPATH=src python3 main.py
```

The tracker monitors playback, avoids duplicate posts, manages a saved-album queue, and sends Discord controls. Important commands are `/inprogress`, `/current`, `/random`, `/search`, and `/editor`. `/random` accepts an optional popularity focus from `0` (the full true-random queue) to `100` (a random choice from the ten most-listened-to rankable albums).

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
python3 post_to_album.py artist-images --limit 25
python3 post_to_album.py apply-artist-images out/artist-images-planned.json
```

Review these files before applying a plan:

- `planned.json`: validated WordPress updates
- `unresolved.json`: releases requiring attention
- `ignored.json`: safely skipped releases
- `applied.json`: apply results
- `artist-images-planned.json`: matched missing artist images, ready for review
- `artist-images-corrections.json`: ambiguous, low-confidence, or image-less artists
- `artist-images-applied.json`: artist term/media successes, skips, and failures

`run --apply` remains available for compatibility but is deprecated. Use `apply-plan` for a reviewable, replay-safe workflow. The CLI accepts both `WORDPRESS_URL` and its legacy `WORDPRESS_BASE_URL` alias.

`artist-images` is also dry-run-first and scans only artist taxonomy terms whose `image` field is empty. It never processes or updates album metadata. Review both artist-image artifacts, then apply the saved plan with `apply-artist-images`.

## Metadata ownership

The shared engine manages provider-derived SCF fields, categories, and custom taxonomies. Rating, favorite, notes, and track highlights remain editor-owned. The tracker uses the known Spotify release ID; the CLI discovers an identity from the WordPress title and artist tags, then both follow the same validation and enrichment path.

The service prepares static Last.fm metadata for unposted saved albums in the background and caches it by Spotify album ID. Newly tracked releases are prioritized from their already-persisted Spotify evidence, so normal publication reuses local metadata instead of repeating provider requests.

Code is split into three parts:

- `src/album_metadata/`: reusable schema, providers, matching, enrichment, and payloads
- `src/tracker_metadata_adapter.py`: tracker adapter
- `src/metadata_cli/`: manual CLI adapter

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall -q main.py post_to_album.py src tests
docker compose config
```

## License

[CC BY-NC 4.0](LICENSE)
