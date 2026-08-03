# WordPress Post → Album CLI

A dependency-free Python 3.10+ CLI that enriches WordPress album posts with Spotify release data, Last.fm identity data, genre tags, and SCF taxonomies. It is single-threaded and treats one WordPress post as one release:

- the rendered post title supplies the release title;
- WordPress tags supply the expected artist names;
- the post publication date supplies `music_listened_at`;
- Spotify is authoritative for the selected release, tracks, durations, explicitness, and release date;
- Last.fm provides a separately validated release identity, URL/MBID when available, and the preferred genre tags.

The normal workflow is deliberately two-stage: `run` creates reviewable JSON without modifying WordPress, then `apply-plan` replays exactly the reviewed writes.

## Matching and enrichment

### Input handling

Provider queries are HTML-unescaped and trimmed but otherwise preserve the source text, including accents, punctuation, and edition labels such as `Deluxe` and `Remastered`. Stored provider values are also preserved verbatim. Comparison-only normalization performs Unicode NFC normalization, case folding, whitespace folding, HTML unescaping, and equivalent apostrophe/dash normalization.

A post without at least one resolvable artist tag cannot be matched safely and is written to `unresolved.json` as `spotify_missing_artist`.

### Spotify discovery and initial selection

Spotify requests use Client Credentials authentication and the US market. Album discovery uses a strongest-intent-first ladder, with up to ten results per query:

1. `album:"<title>" artist:"<first artist>"`
2. the free-text title followed by every supplied artist
3. the title alone, but only if the artist-aware queries returned no candidates

Candidates are accumulated in provider order and deduplicated by Spotify ID. The ladder stops as soon as the accumulated candidates contain a safe winner. Skipping the broad title-only query when artist-aware candidates already exist avoids adding no stronger identity evidence and avoids Spotify failures on very common titles. If a stored Spotify album ID is absent and the post title ends in bracketed provider-style annotations, discovery may repeat with the unannotated title base.

Each candidate receives:

- a title score, with a minimum of **0.80**;
- the best pairwise similarity between any WordPress artist tag and any credited Spotify artist, with a minimum of **0.70**;
- a combined score of `0.65 × title + 0.35 × artist`, with a minimum of **0.82**.

Trailing parenthesized or bracketed edition annotations are ignored only when comparing otherwise identical title bases. They are not removed from queries or stored values.

Among candidates passing all three gates:

1. one unique exact normalized title wins;
2. otherwise, a top candidate wins when its score is at least **0.05** above the runner-up;
3. when contenders are within `0.05`, an existing trustworthy release type may break the tie only if exactly one contender is compatible;
4. otherwise the result remains ambiguous and enters full-evidence recovery.

Existing release-type evidence comes from legacy category IDs or an existing recognized `release_type` term. Search-row compatibility means Spotify `album` with at least 7 tracks for `Album`, `single` with 1–3 for `Single`, `album` or `single` with 4–6 for `EP`, and `compilation` for `Compilation`. Conflicting, multiple, or unknown values are ignored. Type evidence never changes scores, rescues a low-confidence candidate, or overrides an already safe textual winner.

### Spotify ambiguity recovery and full release validation

Only candidates that passed the original text gates can enter ambiguity recovery. Existing metadata is corroborating evidence, never authority.

Recovery proceeds conservatively:

1. A stored album ID may win only if it still appears in current search, still passes identity and optional type gates, and its complete ordered stored track-ID list exactly equals the current fully paginated Spotify list.
2. Otherwise every contender is fetched before narrowing; one failed or unavailable contender cannot manufacture a unique winner.
3. Complete contenders are narrowed by trusted release type when available.
4. Candidates must be playable public releases with tracks and an HTTP(S) Spotify URL.
5. A unique best full-title score, then a unique exact full edition title, may select the winner.
6. If still tied, one unique integer popularity maximum may select the winner; missing or tied popularity is not evidence.
7. Releases with identical complete fingerprints—album identity plus ordered track metadata—use the lexicographically smallest Spotify ID. Materially different editions remain unresolved.

The selected album and every paginated track are then validated for required IDs, names, positive durations, positions, explicitness, and consistency with `total_tracks`. Market-restricted rows that hide required titles or durations are placed in `ignored.json`; malformed or incomplete provider responses are operational failures. The selected full-object title becomes `spotify_title`.

The `fuzzy` command displays discovery candidates and initial text scores only; it does not run stored-track or full-release ambiguity recovery.

### Last.fm discovery and candidate selection

Last.fm matching starts only after Spotify has supplied one validated release. Discovery runs `album.search` for:

1. the Spotify title plus each credited Spotify artist, one query per artist;
2. the Spotify title alone.

Rows repeated by later fallback queries are deduplicated, while duplicate rows returned by the same query are retained as real ambiguity evidence.

Selection prefers one exact normalized title/artist row. Multiple exact rows can be reduced only by one unique exact raw spelling/punctuation match for the primary Spotify artist or one uniquely usable UUID-shaped MBID. Otherwise, fuzzy candidates must pass:

- title score **0.85**;
- artist score **0.75**;
- combined score `0.70 × title + 0.30 × artist` of **0.85**;
- a gap of at least **0.03** from the runner-up.

For multi-artist Spotify releases, a Last.fm combined credit containing every credited artist is treated as an exact artist match.

### Last.fm detail validation and recovery

A valid selected search-row MBID is the preferred `album.getInfo` locator; otherwise the exact selected artist/title is queried with autocorrection disabled. The returned detail must agree with both the Spotify release and selected search row at the same **0.85 title / 0.75 artist** identity gates.

Tracks are optional confirmation:

- If Last.fm supplies no usable tracks, matching album and artist identity is sufficient.
- If it supplies tracks, matching is deterministic and one-to-one. Each pair must score at least **0.90**, and at least **60%** of the smaller provider track list must match.
- Placeholder rows such as `Track 02` are discarded when informative tracks exist.
- Comparison recognizes narrowly bounded punctuation/remaster variants, balanced quoted bases followed by punctuated annotations, and exact-position Latin↔Japanese sequences with at least three lexical anchors and 30% anchored tracks.
- A supplied track list below the 60% gate is contradictory evidence, not missing evidence.

When an MBID lookup changes identity or returns contradictory tracks, exactly one raw selected artist/title lookup may replace it, but only after complete validation. Ambiguous Last.fm search results first try an exact combined-artist page, then one unique exact primary-artist contender, then detail validation of every contender. Ambiguity recovery requires at least 60% coverage of the full Spotify track list, not merely the smaller-list overlap.

A narrowly bounded stale-cache exception accepts an exact non-eponymous album/artist page when Last.fm’s track cache contradicts current Spotify data; its search-row MBID is discarded and the patch records `lastfm_stale_tracks`. Unresolved identity changes, track contradictions, and ambiguities are never converted into matches by result order.

After acceptance, `lastfm_url` and `mbid` prefer validated `album.getInfo` values and fall back to the accepted search row. Missing or malformed values are omitted. A missing MBID is nonfatal and produces `lastfm_no_mbid`.

### Genre-tag discovery

At most three genres are retained, in provider order. Discovery stops at the first source yielding useful values:

1. tags embedded in accepted Last.fm album detail;
2. `album.getTopTags` using the accepted MBID or exact accepted artist/title route;
3. `artist.getTopTags` for the accepted detail artist and selected search artist, first exact and then with Last.fm autocorrection;
4. Spotify genres fetched from each exact credited artist ID.

Tags are trimmed and deduplicated case-insensitively while preserving provider spelling. Artist-name tags and the following non-genre patterns are removed: year-only values, `AOTY`, `best of <year>`, `seen live`, `favorite(s)`, and `under <number>`. Single-word genres such as `rock`, `pop`, and `ambient` are valid. If every source is empty or unavailable, enrichment continues without changing genre and records `lastfm_no_tags`.

Spotify and Last.fm release identity are both required for a planned patch; MBIDs and genre tags are optional.

## Generated metadata and taxonomies

Program-managed SCF fields are:

- `spotify_title`
- `music_tracks` (`disc_number`, `track_number`, `title`, `duration_ms`, `spotify_id`, `highlight`, `explicit`)
- `music_length_ms`, `music_avg_track_ms`, `music_total_tracks`, `music_explicit`
- `spotify_album_id`, `spotify_album_url`
- `music_release_date`, `music_listened_at`
- `lastfm_url`, `mbid`
- `listen_count`

`music_release_date` comes from Spotify. Year-only releases become 1 January and month-only releases become the first of that month. `music_listened_at` comes from the WordPress post date. Plans store both as `dd/mm/YYYY`; replay converts them to ACF/SCF’s required `YYYYMMDD` REST storage format. `listen_count` is currently computed as `1`.

The release type is computed from Spotify tracks:

- Spotify `compilation` → `Compilation`;
- at least 7 tracks or at least 30 minutes total → `Album`;
- 4–6 tracks under 30 minutes, or 1–3 tracks with any track at least 10 minutes → `EP`;
- 1–3 tracks under 30 minutes with every track under 10 minutes → `Single`;
- otherwise → `Album`.

The `artist` taxonomy uses the original WordPress tag names, not Spotify’s display credits. `genre` uses the accepted tag pipeline above. `release_type` always receives exactly one computed value. Its legacy category twin is ID 6 (`Album`), 7 (`EP`), 5 (`Single`), or 98 (`Compilation`); only those legacy IDs are replaced, while marker IDs such as 93/200 and unrelated categories are preserved.

Editor-owned values are never managed: `music_rating`, `music_favorite`, `music_notes`, and each track’s `highlight`. Removed fields `music_mood_tags`, `unreleased`, and hyphenated `listen-count` are never written.

## Write policies

### Fill-only default

`run` uses `fill_only` unless `--overwrite-managed` is supplied. For posts that proceed to enrichment, it omits each populated managed ACF value independently, omits populated `artist` and `genre` taxonomies, and normalizes the computed release type and its category twin. A post whose managed ACF fields plus `artist` and `release_type` are already complete is skipped before provider calls; a missing genre alone does not prevent that fast-path skip.

### Overwrite-managed

`run --overwrite-managed` plans replacements for every valid program-managed value and managed taxonomy even when populated. It still never clears an existing field when a provider has no valid replacement. Rebuilt tracks preserve highlights by Spotify ID, default new IDs to `false`, and remove provider-owned rows no longer returned by Spotify. Ratings, favorites, notes, and highlights remain protected.

## Configuration

Copy `example.env` to `.env`, or provide the same values as environment variables. Use `--env PATH` to select another simple `KEY=VALUE` file.

| Command | Required variables |
| --- | --- |
| `run` | `WORDPRESS_BASE_URL`, `WORDPRESS_USERNAME`, `WORDPRESS_APP_PASSWORD`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `LASTFM_API_KEY` |
| `apply-plan`, `stats` | `WORDPRESS_BASE_URL`, `WORDPRESS_USERNAME`, `WORDPRESS_APP_PASSWORD` |
| `fuzzy` | `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` |

WordPress must expose the active SCF field group and `artist`, `genre`, and `release_type` taxonomies through the standard WP REST API routes. Authentication uses a WordPress Application Password.

## Commands and safe workflow

```bash
# Inspect current managed-field and taxonomy fill counts.
python3 post_to_album.py stats

# Inspect Spotify search candidates and initial scores; performs no writes.
python3 post_to_album.py fuzzy "Été (Deluxe Edition)" "Beyoncé"

# Plan every post with the default fill-only policy. --dry-run is the default.
python3 post_to_album.py run --dry-run

# Plan replacement of valid program-managed metadata without writing WordPress.
python3 post_to_album.py run --dry-run --overwrite-managed --out-dir out

# Review all generated artifacts.
less out/planned.json
less out/unresolved.json
less out/ignored.json

# Apply one reviewed plan without refetching Spotify, Last.fm, or source posts.
python3 post_to_album.py apply-plan out/planned.json
```

Common batch flags are `--offset N` and `--limit N`. `run` also accepts `--out-dir DIR`; `apply-plan --out-dir DIR` controls where `applied.json` is written. Run `python3 post_to_album.py COMMAND --help` for complete syntax.

`run --apply` is retained for compatibility but deprecated. Prefer a dry run, review, and separate `apply-plan` invocation. `--dry-run` and `--apply` are mutually exclusive.

## Artifacts, diagnostics, and failures

`run` atomically writes:

- `planned.json`: schema version 2, containing `schema_version`, `generated_at`, `write_policy`, and accepted `patches`;
- `unresolved.json`: schema version 3, containing ambiguous, contradictory, malformed, or operationally failed posts;
- `ignored.json`: schema version 3, containing current provider-catalog gaps and market-restricted releases that cannot be enriched safely.

Each planned patch contains post identity, optional `source_modified`, accepted Spotify and Last.fm evidence, intended writes, and nonfatal diagnostics. Typical nonfatal diagnostics record missing MBIDs/tags or a narrowly bounded Last.fm recovery. `source_modified` is audit information, not an optimistic-concurrency check.

Provider HTTP 429/5xx, network, and timeout failures receive up to three attempts with 1-second then 2-second fallback delays. An integer `Retry-After` controls ordinary 429 delays. Spotify development-mode `QUOTA_EXCEEDED` opens the Spotify circuit immediately for the reported delay and is not retried. Three consecutive exhausted transient operations open that provider’s process-local circuit; later requests fail without network access. Spotify and Last.fm circuits are independent. The run still writes artifacts for the requested batch and exits nonzero when a provider circuit opened. Request counts are logged by provider operation.

WordPress tag IDs/names are cached in `out/tag-cache.json` for up to 24 hours. A cheap total/highest-ID probe validates a fresh cache; stale caches are replaced only after a stable full scan, and a previously valid cache can be used if refresh fails.

## Applying a reviewed plan

`apply-plan` validates the entire schema-2 artifact before applying `--offset`/`--limit` or performing any write. Old schema versions, unknown keys, invalid evidence, unsupported managed fields, malformed track rows, invalid dates/URLs/MBIDs, duplicate IDs, and invalid taxonomy replacements are rejected.

For the selected patches it then:

1. lists all destination taxonomy terms;
2. creates every missing term before the first post update;
3. converts reviewed taxonomy names to environment-specific integer IDs;
4. converts planned `dd/mm/YYYY` date-picker values to ACF’s `YYYYMMDD` REST format;
5. posts exactly the reviewed ACF, category, and taxonomy replacements;
6. atomically writes `applied.json` with succeeded post IDs and failures.

Replay makes no Spotify/Last.fm calls, does not refetch source posts, does not re-evaluate field presence or write policy, and does not verify that `source_modified` is unchanged. A successful HTTP update is recorded as succeeded without a subsequent read-back. Review and apply plans promptly.

### Controlled rollout

Before a batch rollout, generate and review one isolated plan:

```bash
python3 post_to_album.py run --dry-run --offset N --limit 1 --out-dir out/one-post
# Or add --overwrite-managed after --dry-run.

python3 post_to_album.py apply-plan out/one-post/planned.json \
  --out-dir out/one-post
```

Manually verify managed ACF values, date pickers, protected rating/favorite/notes/highlights, all three custom taxonomies, unrelated and marker categories, and the effective release-type category ID. Then proceed to a small reviewed batch before a full rollout.

## Safety and scope

- Planning reads WordPress and providers but never creates terms or updates posts.
- Applying may create taxonomy terms and update only fields represented in the reviewed plan.
- Media, featured images, ratings, favorites, notes, and unrelated categories are untouched.
- Tests use fakes and make no live writes.
- `out/planned_patches.json` is protected historical output and is not accepted by `apply-plan`.
- Application is not transactional and has no programmatic rollback; retain reviewed plans and `applied.json` as evidence.

## Supporting documentation

The [current metadata contract](./wordpress-album-metadata-change-plan.md) supersedes the historical root `plan.md`, `questions.md`, and `vision.md`. The completed overhaul sequence remains useful implementation history:

- [`Plan 00: index`](wordpress-album-metadata-overhaul-plans/00-overhaul-index.md)
- [`Plan 01: search and matching`](wordpress-album-metadata-overhaul-plans/01-search-and-matching-plan.md)
- [`Plan 02: SCF and WordPress payload`](wordpress-album-metadata-overhaul-plans/02-scf-and-wordpress-payload-plan.md)
- [`Plan 03: planned JSON and replay`](wordpress-album-metadata-overhaul-plans/03-planned-json-and-replay-plan.md)
- [`Plan 04: integration, tests, and rollout`](wordpress-album-metadata-overhaul-plans/04-implementation-order-and-tests.md)

`scf-export-2026-07-24.json` is the active schema evidence for REST exposure, field types, taxonomies, date display/return formats, and the seven track repeater children. Older exports are historical snapshots.
