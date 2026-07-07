# Plan — Backend-1: Auto-fill SCF metadata on Discord publish

> **Goal:** when a release is posted to WordPress via the Discord bot
> (any path — 75% prompt, /current confirm, in-progress "Publish early", or
> auto-completion at 100%), automatically fill the same `acf` block on the
> WordPress post that `Wordpress-PostToAlbum-Script` fills in.
>
> Mood tags (from Last.fm) are the only field that can plausibly fail;
> the post-publish Discord embed must surface that failure so it is visible
> instead of silent.

---

## 1. What gets filled (SCF field → source)

Identical to `Wordpress-PostToAlbum-Script/post_to_album.py` lines 41–52 +
`plan.md` §2. Auto-fill only on **create**, never on update; never overwrite
human-curated fields. Last.fm is the **only** source for mood tags. Spotify
is the source for everything else.

| # | SCF field | Type | Source | Transform |
| --- | --- | --- | --- | --- |
| 1 | `music_tracks` | repeater | Spotify `GET /v1/albums/{id}/tracks` (paginated via `tracks.next`) | rows `{disc_number, track_number, title, duration_ms, spotify_id, highlight:false, explicit}` |
| 2 | `music_length_ms` | number | derived | `sum(track.duration_ms)` over countable tracks |
| 3 | `spotify_album_id` | text | Spotify `album.id` | raw |
| 4 | `spotify_album_url` | url | constructed | `https://open.spotify.com/album/{id}` |
| 5 | `music_release_date` | date_picker | Spotify `album.release_date` | coerce YYYY / YYYY-MM to YYYY-01-01 / YYYY-MM-01 → `d/m/Y` (SCF rejects partial dates) |
| 6 | `music_listened_at` | date_picker | the post's own WP publish date (i.e. when the bot publishes it) | `d/m/Y` |
| 7 | `lastfm_release_id` | text | Last.fm `album.getinfo` → `album.mbid` | raw UUID if non-empty |
| 8 | `music_total_tracks` | number | Spotify `album.total_tracks` | int |
| 9 | `music_avg_track_ms` | number | derived | `music_length_ms // music_total_tracks` |
| 10 | `music_explicit` | true_false | derived | `any(t.explicit for t in countable_tracks)` |
| 11 | `music_mood_tags` | repeater | Last.fm `tags.tag[]` (top 3 after blocklist) | `[{mood: <tag>}, ...]` — **may be empty** |
| 12 | `listen-count` | number | derived | `count_matching_posts(title, artists) + 1` (see §7) |

Fields in the Spotify-blog-tracker flow that are already correct today:
category (Album/EP/Single/Compilation), tags (artist names), featured
media (album art) — keep, no SCF work needed.

Fields we deliberately do **not** touch (human-curated in WP repo):
`music_rating`, `music_favorite`, `music_notes`, `unreleased`,
per-track `highlight`.

---

## 2. Files to add / change (YAGNI)

### New file: `src/lastfm_client.py` (~80 lines, stdlib-free, parallels `src/spotify_client.py`)

Single-responsibility client for Last.fm's two read-only calls we need:

```python
class LastFMClient:
    async def album_getinfo(artist: str, album: str) -> dict
    # returns {} on HTTPError so callers don't need try/except.
```

Plus a tiny pure helper next to it (or a private `_pick_mood_tags` in
publisher.py — either is one line of preference; pick the helper, keeps
the client file single-purpose):

```python
LFM_BLOCKLIST = ("^\d{4}$", "^aoty$", "^best of \d{4}$",
                 "^seen live$", "^favorites?$", "^under \d+$")
async def pick_mood_tags(album_info: dict, max_n: int = 3) -> list[str]
# copies the 4-shape handling from post_to_album.py:277-307.
```

This is straight-line code lifted from `post_to_album.py` lines 261–313.
We do **not** bring the fuzzy-search ladder over — the Discord flow always
posts a release whose Spotify ID is already known (it's the album the
user is currently listening to, or a saved-library pick), so there is
nothing to fuzzy-match.

### Changed file: `src/config.py`

Add three lines:

```python
self.lastfm_api_key = os.getenv("LASTFM_API_KEY")
```

Plus include it in `_validate()` only if a feature-flag env var is set
(`SPOTIFY_BLOG_TRACKER_FILL_SCF=1`) **or** forever as required. → forever
required. We have an existing `.env.example` slot for it; just gate it on
the flag so devs without a Last.fm key don't crash on import.

```python
if os.getenv("SPOTIFY_BLOG_TRACKER_FILL_SCF") == "1":
    required.append(("LASTFM_API_KEY", self.lastfm_api_key))
```

### Changed file: `src/publisher.py`

- Construct `self.lastfm = LastFMClient(config.lastfm_api_key)` in `__init__`
  (None safe; the client handles no-key gracefully).
- Add one method `_build_scf_payload(self, release: Release) -> tuple[dict, dict]`
  that returns `(acf_payload, fetch_status)` where `fetch_status` is
  `{"mood_tags": <list|None>}` so the caller can show "mood tags unavailable"
  in the embed when `None`.
- The method does the four reads it needs, all already-cached or one-call:
  1. Album metadata (already in `db.release` from `_build_release_from_spotify`).
  2. Tracks (already in `db.release.tracks`).
  3. Spotify album-cover URL (already in `release.cover_url`).
  4. Last.fm album.getinfo (new call, cached for the run via `self.lastfm`).
- Wire it into `publish_release`: after `post = await self.wordpress.create_post(post_data)`,
  call `await self._fill_post_scf(post["id"], release)` and pass the
  fetched status back to the caller (tracker → discord).

### Changed file: `src/models.py`

Add one tiny dataclass — the only new type — so the publisher's
return value is type-stable:

```python
@dataclass
class PublishResult:
    post: dict                       # the WP post payload
    scf_pending_tags: list[str]      # [] when everything OK
                                     # ["mood_tags"] when Last.fm had no tags
    listen_count: int                # value just written to SCF (>= 1)
```

`Release` does not need new fields; everything we need is already in
`release.tracks` and `release.cover_url`/`release.release_date`/`release.spotify_id`.

### Changed file: `src/tracker.py`

In `_publish_release`, swap `_ = await self.publisher.publish_release(...)`
to a typed return so the on-success branch can pass the SCF status to
Discord. **Minimal** diff — 3 lines:

```python
result = await self.publisher.publish_release(release, as_relisten=as_relisten)
release.wordpress_post_id = result.post["id"]
...
if self.discord_bot:
    await self.discord_bot.send_publish_notification(release, result)
```

### Changed file: `src/discord_bot.py`

Two narrow edits in the existing `send_publish_notification`:

1. Signature gains a `result: PublishResult` param.
2. New embed field, only added when `result.scf_pending_tags`:

```
embed.add_field(
    name="⚠️ SCF metadata",
    value="Filled automatically · mood tags unavailable (Last.fm returned no tags for this release)",
    inline=False,
)
```

No field added when everything is fine — keeps the success path identical
to today and matches the user's "see visually when that happens" ask.

Text body above the embed gets a one-line tweak:
`"The release has been published to WordPress and SCF metadata was auto-filled."`
when everything went well, or
`"The release has been published to WordPress, but SCF mood tags could not be filled (Last.fm returned no tags)."`
when mood tags failed. Single sentence, no bullet, ephemeral-only.

---

## 3. Why this layout

- **Single new module + one dataclass.** Every other change is a
  thin edit to a function that already exists. No new CLI surface, no new
  tables, no new prompts, no migrations.
- **Reuse, don't re-implement.** `_build_release_from_spotify` already
  pulls the album metadata and full track list into the in-memory
  `Release`. We do not re-fetch from Spotify at publish time — we just
  serialise what's already in memory (`release.tracks`, `release.cover_url`,
  `release.release_date`, `release.spotify_id`).
- **No fuzzy-search plumbing.** The Discord flow always has the album ID;
  the complement script needs fuzzy search because it has only a post
  title. Skipping the ladder saves ~150 LOC.
- **Mood tags as the only failure mode.** Spotify album fetch fails →
  no publish (existing path). Last.fm 404s / returns empty → mood tags
  empty → embed surfaces it. No field is silently dropped.
- **SCF taxonomy mirroring (artist / genre / release_type terms).** The
  WP script does this; we skip it intentionally because the Discord flow
  already sets WP `tags` (artist names, 1:1 passthrough in
  `Publisher._resolve_tags`) and `categories` (Album/EP/Single/Compilation
  - Relisten when applicable). The end-state WP post already has those.
  Genre taxonomy would require creating `genre` terms on a base site that
  likely hasn't been seeded; we don't pay that cost now and the existing
  complement script is the natural place to fill `genre` retroactively.

---

## 4. Algorithm when Discord publishes a release (today + tomorrow)

Today:

1. `tracker._publish_release` → `publisher.publish_release(release, ...)`
2. `Publisher.publish_release` builds `post_data` with title/categories/tags/media,
   `POST /wp/v2/posts`, sets `release.wordpress_post_id`, refreshes cache.
3. `tracker` sets `release.status = PUBLISHED_RECENTLY`, saves.
4. `DiscordBot.send_publish_notification(release, post)` formats an embed.

Tomorrow (the diffs above, in order):

1. Same as today.
2. Same as today, then `_fill_post_scf(post["id"], release)` performs:
   - build `acf` payload (Spotify + Last.fm + derived fields).
   - `POST /wp/v2/posts/{id}` (no need for `?context=edit` because the
     write endpoint merges the supplied meta; complement uses the same
     trick on first dry-run and on apply). See point 5 below.
   - collect `fetch_status = {"mood_tags": <list|None>}` and return it.
3. Same as today; `tracker` now also passes back `result` to the bot.
4. `send_publish_notification` gets `result` and emits a single extra
   embed field when mood tags failed.

### 5. How SCF makes it into the WP `meta`/`acf` block

`show_in_rest: 1` is set on the field group (cf.
`scf-export-2026-07-05.json` line 577). SCF exposes its `meta` under the
top-level `acf` block on `POST /wp/v2/posts` and `POST /wp/v2/posts/{id}`.
The complement script confirms this in `plan.md` §1 ("Repeater location in
REST: top-level `acf` block ... `?context=edit`") and demonstrates the
write path in `_req_post(self._url(f"/posts/{pid}"), body)` (line 440).

Action: `POST /wp/v2/posts/{id}` with `{"acf": {…}}`. We do **not** need
`?context=edit` for writes — that's a read-side affordance. The full
`acf` dict we send will REPLACE only the fields we include; SCF keeps
the un-supplied fields untouched on update. (Confirmed by the complement
script: `_set_if_empty` over POST keeps other fields intact; SCF behaves
the same on REST.) Sent on the same async client (`WordPressClient.update_post`).

To be safe, build the `music_tracks` rows from **countable tracks only** —
mirroring how `_build_release_from_spotify` already filters
`is_countable = not is_local and is_playable`, and how the complement
script computes `length_ms = sum(t["duration_ms"] for t in track_rows)`.
That keeps WP totals consistent with what the Discord embed shows on
`/inprogress`.

### 6. Last.fm fail-mode — what the embed shows

`POST /wp/v2/posts/{id}` returns 200 × (we always write the non-Last.fm
fields first in the same `acf` block) → embed says "metadata auto-filled".
`LastFM.album_getinfo` raised → that field is omitted from the `acf` body
(SCF drop semantics, same as complement script: `if not lfm_tags: pass`)
→ embed field "⚠️ SCF metadata: mood tags unavailable (Last.fm returned
no tags)" shows up.

If `LASTFM_API_KEY` is missing or invalid → same path; embed surfaces the
gap; nothing fails to load. (Config gating means a missing key never
crashes startup either.)

---

## 5. Verification plan

Run a manual `/current` confirm against a real listening session. Check
the published post's `acf` block via `curl -u user:app
$WORDPRESS_URL/wp-json/wp/v2/posts/<id>?context=edit`. Every field above
should have its expected value, except `music_mood_tags` which can be
empty if Last.fm had no tags.

For the failure mode: temporarily set `LASTFM_API_KEY` to an invalid
key, restart, post a release. Verify the embed surfaces the
"mood tags unavailable" line.

A relisten (`/current` confirm on an album whose WP post already exists)
goes through the same publisher code → same auto-fill applies and the
embed behaves identically with one addition (see §7 below):
`listen-count` is bumped based on the number of matching duplicates
already in WordPress.

---

## 7. `listen-count` — auto-bump on duplicate publish

### 7a. Rule

`listen-count` = (number of already-published WP posts that match this
release's normalized title + normalized artists) + 1.

> First publish → `1`. After one relisten → `2`. After two relistens →
> `3`. Spelled out so the math is unambiguous.

### 7b. Why this is safe to assume (verification)

Verified against the existing code, not assumed:

- Duplicate detection already exists. `Tracker._check_duplicate`
  (`src/tracker.py` line 349) walks `self.db.get_wordpress_posts()` and
  matches on `(normalized_title == release.normalized_title) and
  (set(normalized_artists) == set(release_normalized_artists))`. Same
  rule used by `SavedLibrary._find_matching_wordpress_post`
  (`src/saved_library.py` line 632). One match = "duplicate".
- The WordPress post fingerprint kept for matching
  (`src/models.py` `WordPressPost`, populated by `database.get_wordpress_posts`
  from the `wordpress_post_cache` table) already has both
  `normalized_title` and `normalized_artists`. No new caching layer.
- The cache is refreshed **after** `create_post` in `publish_release`
  (`refresh_post_cache(force=True)`). Count must run BEFORE
  `create_post`, otherwise the post just being created is double-counted.
- The cache is keyed on `id, title, normalized_title, artists, normalized_artists, link`
  — no `categories`, so we cannot filter by Relisten category from the
  cache alone. We deliberately count ALL matching posts (Relisten or
  first-listen) because all of them count toward listen-count per
  the WP repo's `scf-export-field-meanings.md`.
- Duplicate detection already fires for the relisten branches:
  `_resolve_current_post_context` (discord_bot.py:1048) and the 75%
  prompt both surface the existing duplicate. `release.is_relisten` /
  `release.duplicate_post_id` are already populated correctly in those
  flows. For `_handle_publish_release` (Publish early from
  `/inprogress`), `release.is_relisten` is already on the in-memory
  release. **Every entry-point into `publish_release` therefore already
  has the duplicate signal present in the in-memory release.** No new
  inputs needed.

### 7c. Implementation (YAGNI one-liner)

Add a private helper on `Publisher`:

```python
async def _count_listen_index(self, release: Release) -> int:
    from utils import normalize_artist_list
    title, artists = release.normalized_title, set(
        normalize_artist_list([a.name for a in release.artists])
    )
    posts = await self.db.get_wordpress_posts()
    matches = sum(
        1 for p in posts
        if p.normalized_title == title
        and set(p.normalized_artists) == artists
    )
    return matches + 1
```

Call it inside `publish_release` *before* `create_post`:

```python
listen_count = await self._count_listen_index(release)
```

Use the value as part of the same `acf` payload `_build_scf_payload`
already produces: `acf_out["listen-count"] = listen_count`. Already
covered by the original field map (line 12), now dynamic instead of
always `1`.

### 7d. Correctness of the `+1`

The `+1` is the post we are about to create. The cache held by
`db.get_wordpress_posts()` at the moment of counting was populated by
the last `refresh_post_cache()`, which ran at startup (+ every 24h) and
after every earlier publish. It does not include the about-to-be-created
post. So `matches + 1` is correct in the common case.

Edge case — race when two publishes happen back-to-back: `publish_release`
awaits `create_post` then awaits `refresh_post_cache(force=True)`. The
next publish sees an up-to-date cache. No fix needed.

Edge case — someone manually creates a matching post on WP between the
last cache refresh and the Discord publish: `matches` undercounts by
that many. Acceptable. Same risk exists for `Tracker._check_duplicate`
today; fixing it would mean refetching WP on every publish, which is
out of scope and contradicts YAGNI. We surface a "⚠️ listen-count may
be stale" warning only when manually-marked, which today nobody does.

### 7e. No embed change

The Discord embed does not need a listen-count row today (`send_publish_notification`
already shows post ID + WP link + release type + progress). Add the
field as a tiny one-line addition:

```python
embed.add_field(name="Listen count", value=str(listen_count), inline=True)
```

when the value is `> 1` only, to keep the standard-case embed clean.
The value is sourced from `PublishResult.listen_count` so the embed
reads the same number that was just written to SCF, never recomputed.

---

## 8. Sequence (implementation order)

1. Add `src/lastfm_client.py` (call + blocklist + pick helper).
2. Add `lastfm_api_key` to `Config` with the `SPOTIFY_BLOG_TRACKER_FILL_SCF=1` gate.
3. Update `.env.example` to set `SPOTIFY_BLOG_TRACKER_FILL_SCF=1` and document it.
4. Add `PublishResult` dataclass in `src/models.py` (with `listen_count`).
5. Add `Publisher._build_scf_payload`, `Publisher._fill_post_scf`,
   and `Publisher._count_listen_index`; return `PublishResult` from
   `publish_release`. Listen-count is computed BEFORE `create_post`.
6. Update `tracker._publish_release` to thread `PublishResult` to Discord.
7. Update `DiscordBot.send_publish_notification` to take `PublishResult`,
   show the mood-tag failure line when `scf_pending_tags` is non-empty,
   and add a one-line `Listen count` embed field when `> 1`.
8. Smoke test with a real publish; verify with `curl` against the post.

No migrations. No new tables. No new commands. No new caching layer. No
new background jobs. Three-line change in `tracker.py`, two-line change
in `discord_bot.py` (per side), two methods (`_build_scf_payload` and
`_count_listen_index`) added to `publisher.py`.
