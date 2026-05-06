# Album Tracking and Auto-Posting Research Blueprint

## Recommended architecture

The best implementation is a **standalone local Python service** that runs continuously, polls Spotify, stores state in a local database, posts to WordPress over the **WordPress REST API**, and exposes a Discord bot as the control plane. WordPress’ REST API is explicitly intended for external applications in any language that can make HTTP requests and speak JSON, and Application Passwords exist specifically for scripts, integrations, and external tools. By contrast, WP-Cron is page-load triggered rather than truly continuous, which makes it a poor fit for second-by-second or even every-few-seconds playback polling. citeturn9search2turn12view0turn12view1turn3view8

That means the original concern—“if the script is separate from the WordPress site, posting might be difficult”—turns out not to be the blocking issue. It is technically straightforward for a separate local process to create posts, upload media, create tags/categories, and trash posts later, because WordPress exposes posts, media, categories, and tags over standard REST endpoints. A separate service is therefore not only possible; it is the cleaner architecture for this use case. citeturn8view0turn8view7turn10view0turn10view1turn12view0

A **full WordPress plugin as the always-on runtime** is the wrong default architecture. PHP-in-WordPress is excellent for request/response work and admin interfaces, but a continuous Spotify listener needs a long-lived process, resilient polling, token refresh, local state transitions, and Discord gateway connectivity. WP-Cron can be driven by a system scheduler, but even WordPress’ own documentation presents that as a way to trigger scheduled jobs, not as a substitute for a long-running daemon. citeturn7view7turn3view8

For Discord, the cleanest model is a **gateway-connected bot** built with `discord.py`, because this project already wants a long-running local process. Discord’s platform docs state that interactions arrive over the Gateway by default, and HTTP interactions are an alternative mainly for apps that only need request/response handling without a persistent connection. Since this project already benefits from a persistent process for Spotify polling and optional presence updates, Gateway is the better fit. citeturn16view3turn16view5

The right final shape is:

- **Core runtime:** standalone Python 3.12 async service
- **Discord:** `discord.py` app commands + views/selects/buttons
- **Spotify:** direct Web API calls from an async HTTP client
- **WordPress:** direct REST API calls with Application Password auth
- **Database:** SQLite
- **Optional helper on WordPress:** a *small companion plugin*, only if you want better duplicate-checking helpers or REST-exposed post meta; not as the primary runtime

The architecture comparison is simple:

| Option | Strengths | Weaknesses | Final judgment |
|---|---|---|---|
| Standalone service only | Clean separation, proper daemon behavior, easy Docker/systemd deployment, easiest Spotify + Discord integration | Slightly more work for exact WordPress matching/meta conveniences | **Recommended default** |
| Standalone service + tiny WordPress helper plugin | Keeps clean runtime while improving duplicate checks and REST meta support | Requires maintaining a second small codebase | **Best “power user” version** |
| Full WordPress plugin runtime | Can live inside the site codebase | Bad fit for continuous polling and gateway connections; brittle operationally | **Not recommended** |

I also do **not** recommend forcing playback through the Spotify Web Playback SDK or any custom player. Spotify describes the Web Playback SDK as a **client-side JavaScript library** for creating a local Spotify Connect device in a browser. That would move the project toward “playback through my app” rather than “observe my real listening across devices,” which is the opposite of your stated goal. citeturn7view2

## What the uploaded brief and plugin imply for the implementation

I used the uploaded **ResearchPlanBriefV2** as the source of truth and inspected the uploaded **Album Art Picker (Spotify)** WordPress plugin. The plugin matters because it reveals the conventions that your site already uses, and those conventions should be preserved by the new tracker rather than re-invented.

From the uploaded plugin source, the important behaviors are clear:

- It computes release type with the following logic:
  - **Compilation** if Spotify says `album_type == compilation`
  - **Album** if track count is at least 7 **or** total duration is at least 30 minutes
  - **EP** if 4–6 tracks under 30 minutes, **or** 1–3 tracks with a longest track of at least 10 minutes
  - **Single** if 1–3 tracks, under 30 minutes total, and longest track under 10 minutes
- It stores Spotify-related metadata such as album ID, album name, artists, album URL, chosen type, and artwork attachment ID.
- It sets the **post title** to the album name.
- It assigns a **category** matching the chosen release type.
- It assigns **artist tags**, but strips commas from tag names before insertion.
- It downloads Spotify artwork, adds it to the media library, and can set it as the featured image.

That plugin behavior is a very strong compatibility anchor. The new system should **replicate those conventions** rather than creating a new taxonomy or naming system.

The only notable product-level change I recommend over the plugin is this: **keep the plugin’s release-type heuristics, but do not let the new tracker depend on the plugin runtime**. The standalone service should reproduce the same release classification rules and posting conventions over REST.

Spotify’s own album metadata reinforces why this is necessary. Spotify exposes `album_type` values such as `album`, `single`, and `compilation`, but not a first-class `EP` type for the way you want to categorize content, so the EP logic does need to remain heuristic. citeturn27view0turn25view1

The best language and library stack, based on the architecture above, is:

| Layer | Recommendation | Why |
|---|---|---|
| Language | Python 3.12 | Best fit for Discord bot + HTTP + local daemon + SQLite |
| Discord | `discord.py` | Mature app-command/view model, persistent views, easy gateway bot |
| Spotify HTTP | `httpx.AsyncClient` | Async-friendly for a daemon already using asyncio |
| Spotify auth | Small custom OAuth manager, or Spotipy only for bootstrap/prototyping | Async custom client is cleaner in production; Spotipy is still useful as reference |
| WordPress HTTP | `httpx.AsyncClient` | Same async transport and retry policy as Spotify |
| DB | SQLite | Single-user local service; lowest operational burden |
| Migrations | Lightweight SQL migration folder or Alembic | Enough structure for schema evolution without overbuilding |

Spotipy is still relevant, but mostly as a reference or bootstrap convenience. Its docs explicitly position **Authorization Code flow** as suitable for long-running apps with refreshable tokens, and it supports custom `CacheHandler` implementations. That is useful guidance, but because this project is already an async daemon, a small custom async Spotify client is the cleaner final fit than wrapping sync Spotipy calls inside thread executors. citeturn24view0turn24view1

One more important compatibility note: Spotify’s docs say metadata and cover art must be accompanied by attribution and a link back to the Spotify object. The uploaded plugin stores the Spotify album URL in post meta, which strongly suggests your site may already have a theme/plugin path for rendering or otherwise using that link. Before launch, confirm that the public site really does satisfy that attribution requirement somewhere in the post experience. If it does not, add it. citeturn27view0

## Spotify tracking design

The **primary endpoint** should be `GET /me/player` — Spotify’s “Get Playback State” endpoint. It returns the fields this project actually needs in one place: `is_playing`, `item`, `progress_ms`, `timestamp`, `shuffle_state`, `repeat_state`, `context`, and active device data. It also has the response/error shapes you need to handle in production, including `200`, `204`, `401`, `403`, and `429`. citeturn3view3turn4view0turn25view3

The recommended **scope set** for the final design is:

- `user-read-playback-state`
- `user-read-recently-played`

If you intentionally choose not to implement any recovery/backfill behavior, then `user-read-playback-state` alone is sufficient. The reason the hybrid set is better is that the playback-state endpoint provides the real-time truth, while recently played gives you a conservative recovery tool after missed polls, restarts, or short outages. Spotify documents `user-read-playback-state` for playback state and available devices, and `user-read-recently-played` for recently played items. citeturn25view0turn7view1turn7view3

For authentication, the best fit is Spotify’s **Authorization Code flow**. Spotify explicitly recommends Authorization Code for long-running applications where the client secret can be safely stored, while PKCE is the better choice when the secret cannot be safely kept. Since your preferred deployment is a local daemon on a machine you control, Authorization Code is appropriate. Spotify access tokens last **one hour**, and refresh-token support is part of the documented flow. citeturn29search2turn29search1turn29search7turn29search0

The service should **not** use `GET /me/player/currently-playing` as the primary endpoint. That endpoint is narrower and does not add decisive value once you already use `GET /me/player`. The playback-state endpoint is the better single truth source because it is already documented to include the player state and device information you care about. citeturn25view1turn25view3

The qualifying-listen rule should be implemented exactly as your brief now specifies:

1. Poll playback state.
2. Count listening only if all of the following are true:
   - HTTP response is usable
   - `is_playing == true`
   - `item` exists
   - `currently_playing_type == "track"` if that field is present
   - `item.is_local == false`
   - `context` exists
   - `context.type == "album"`
   - `shuffle_state == false`
3. Resolve the release from `item.album.id`.
4. Resolve the track inside that release.
5. If that track has not already been marked listened in the current release lifecycle, mark it listened **immediately on observation**.

That rule is intentionally permissive. It does **not** require hearing 50%, 75%, or 100% of a song; it only requires that the song be actually observed in qualifying playback. Spotify’s playback-state and currently-playing docs support the gating fields that make this possible: `context`, `is_playing`, `progress_ms`, `timestamp`, `currently_playing_type`, and `is_local`. citeturn4view0turn25view1turn5view0turn4view5

This means the decision table should be:

| Condition | Count track? | Reason |
|---|---|---|
| HTTP `204` | No | No active usable playback state |
| `item == null` | No | Nothing to count |
| `currently_playing_type != track` | No | Ignore episodes, ads, unknown media |
| `item.is_local == true` | No | User requirement says ignore local tracks |
| `context == null` | No | Album-origin not proven |
| `context.type != album` | No | User requirement says only album-origin counts |
| `shuffle_state == true` | No | User requirement says shuffle must not count |
| `is_playing == false` | No | The playhead is not actively playing |
| Final computed type is `Single` | No automatic progress row | Singles are manual-publish only |
| Otherwise | Yes, once per lifecycle | This is the normal qualifying case |

Spotify’s docs explicitly say `context` can be `null`, and give example context types such as `artist`, `playlist`, `album`, and `show`. They also document `shuffle_state`, `repeat_state`, and the fact that device IDs are only “persistent to some extent” and should not be treated as forever-stable identifiers. citeturn4view0turn7view1

That last point matters a lot: **do not pin the tracker to a device**. This should be an **account-level** tracker that follows the currently active playback state for the user. Cross-device playback transfer is a normal use case here. citeturn7view1turn4view3

For release identity, use **Spotify album ID as the internal release key**. That cleanly preserves the requirement that deluxe editions, remasters, expanded editions, and alternate versions are treated as different releases. Spotify’s album object gives you album ID, URL, artists, name, images, release date, total tracks, and raw `album_type`. citeturn27view0

For track identity inside a release, do **not** use only the track ID. Spotify’s album tracks data includes `disc_number`, `track_number`, `duration_ms`, `is_playable`, `restrictions`, and `is_local`, and track relinking can change what the playable track object looks like in a market. The robust internal key is:

`release_id + flattened_position + disc_number + track_number`

with `spotify_track_id` stored as a helpful matching field, not the sole truth. That design is better because multi-disc releases are one release, track relinking exists, and countability depends partly on track restrictions and locality. citeturn27view1turn28view0turn28view1

The flattened ordering rule should be:

- sort by `disc_number`
- then by `track_number`
- give each countable track a monotonic `position`
- compute completion over **countable** tracks only

This matches your requirement that different discs are one release, while still preserving internal correctness for display and debugging. Spotify exposes both disc and track numbering on album-track results. citeturn27view1

For **local or unavailable tracks**, the cleanest rule is:

- if the observed current item is local: ignore it
- if an album track is unplayable or restricted in your market: mark it `is_countable = false`
- completion percentage uses only countable tracks in the denominator

That is the closest implementation of “local or unavailable tracks should be ignored.” Spotify’s album-track schema documents `is_playable`, `restrictions.reason`, and `is_local`. citeturn28view0turn28view1turn28view2

The best polling cadence is a **hybrid adaptive poller**, not an ultra-fast loop:

- **Every 3 seconds** while `is_playing == true`
- **Every 8 seconds** while paused or while playback is active but non-qualifying
- **Every 15 seconds** after repeated `204` responses or no active playback
- On **429**, honor `Retry-After` exactly and add jitter
- On repeated network/api failures, exponential backoff up to 60 seconds, then recover to the normal cadence

Spotify’s rate-limit docs do not prescribe these exact intervals; they do document that rate limits are app-wide over a thirty-second window and that `429` is the signal to back off. The cadence above is an engineering recommendation aimed at catching track changes without over-polling. citeturn29search3turn3view3

The role of **recently played** should stay deliberately limited. Spotify documents it as a normal endpoint for recently played tracks, not a live event stream, and notes that it does not support podcast episodes. For this project, it should be used only as a **gap-fill mechanism**, never as the sole authority for whether something was album-origin listening. citeturn7view3

The conservative backfill policy I recommend is:

- Use playback state as the only source that can **start** a new in-progress release.
- Use recently played only after:
  - service startup
  - reconnect after outage
  - a long poll stall
  - a suspicious track jump
- Only backfill tracks into a release that is already active or that was very recently active from a verified album-context session.

That prevents recently-played data from “inventing” album sessions that may have actually come from playlists.

The core tracker can be implemented with logic like this:

```text
loop forever:
    state = spotify.get_playback_state()

    if state.status == 204:
        log("no active playback")
        sleep(idle_interval)
        continue

    if state.rate_limited:
        sleep(state.retry_after + jitter)
        continue

    if not state.usable:
        log("bad playback state", state.error)
        sleep(backoff_interval)
        continue

    if not qualifies_for_album_tracking(state):
        update_current_listening_cache(state)
        sleep(adaptive_interval_for_non_qualifying_state)
        continue

    release = get_or_create_release_from_album(state.item.album.id)

    if release.computed_type == "Single":
        update_current_listening_cache(state)
        sleep(active_interval)
        continue

    track = match_observed_track_to_release_track(release, state.item)

    if track is None or not track.is_countable:
        log("track could not be matched or is not countable")
        sleep(active_interval)
        continue

    if not track.listened:
        mark_track_listened(track, source="playback")
        recompute_release_progress(release)

        if release.progress >= 0.75 and not release.prompt_75_sent:
            send_early_publish_prompt(release)

        if release.is_complete:
            if release.duplicate_state == "unknown":
                release.duplicate_state = check_wordpress_duplicate(release)

            if release.duplicate_state == "found":
                send_relisten_prompt(release)
            else:
                publish_release(release)

    update_current_listening_cache(state)
    maybe_run_recently_played_backfill()
    sleep(active_interval)
```

The classification logic should stay plugin-compatible:

| Type | Rule |
|---|---|
| Compilation | If Spotify raw `album_type == compilation`, default to `Compilation` |
| Album | `track_count >= 7` **or** `total_duration >= 30 minutes` |
| EP | `4–6 tracks and total < 30 minutes`, **or** `1–3 tracks and longest_track >= 10 minutes` |
| Single | `1–3 tracks and total < 30 minutes and longest_track < 10 minutes` |

That gives you continuity with the existing WordPress side and avoids a painful taxonomy split later.

## WordPress publishing contract

The new service should publish by calling **core WordPress REST endpoints directly**. That is the clean base path. WordPress’ REST documentation explicitly supports external tools, and the endpoints you need already exist for posts, media, categories, tags, and authentication. citeturn9search2turn8view0turn8view7turn10view0turn10view1turn12view0

The best authentication model is a **dedicated WordPress user plus an Application Password**. WordPress documents Application Passwords as revocable, per-application credentials meant for API access by integrations and scripts, typically sent through HTTP Basic Authentication. WordPress also documents that Application Passwords are available by default on SSL sites or local environments, while still recommending HTTPS because Basic Auth credentials should not traverse the network unencrypted. In practice, for a local-only installation, the safest setup is still a local HTTPS hostname or a trusted loopback setup rather than plain HTTP across a LAN. citeturn12view1turn12view0turn12view2

The dedicated WordPress user should have only the capabilities it actually needs: publish posts, upload media, edit its own posts, and manage categories/tags if necessary. Do not use your main admin account if a less-privileged publishing user is feasible.

Duplicate detection is where most implementations will become brittle unless they are very explicit. Your brief is right to reject historical Spotify-ID matching. The correct historical duplicate key is:

- **normalized post title**
- plus **unordered normalized full artist tag set**

The plugin’s convention means artist tags must first have commas stripped before matching, because that is how new tags are created there.

My recommended normalization is intentionally conservative:

- Unicode NFKC normalize
- casefold
- trim outer whitespace
- collapse repeated internal whitespace to one space
- remove zero-width characters
- for artist tags only, strip commas *before* other normalization to mirror the plugin

Do **not** introduce fuzzy matching, “contains” matching, or “primary artist only” matching. That will create false positives around collaborations, deluxe editions, and similarly named releases.

For the actual duplicate lookup, the simplest robust design is to **cache the published post index locally**. WordPress’ pagination docs say paginated responses expose `X-WP-Total` and `X-WP-TotalPages`, and the REST API supports `_fields` so you can fetch only what you need. Since your site is only a few hundred posts, the service can cheaply hydrate:

- all published posts with `id`, `title`, `tags`, `link`
- all tags with `id`, `name`, `slug`

and build a local map from normalized title + normalized artist-set fingerprint to post IDs. That is cleaner and more exact than repeatedly trying to search the site with text queries at publish time. citeturn30search0turn32search7turn10view1turn7view4

If you want an even cleaner version, this is where the **optional companion plugin** becomes valuable. A tiny helper plugin can register a custom route with `register_rest_route()` and a strict `permission_callback`, then expose an endpoint like:

`GET /wp-json/albumtracker/v1/duplicate-check?title=...&artists[]=...`

That helper can run the exact title/tag normalization inside WordPress and return deterministic duplicate-check results. WordPress documents custom REST routes, permission callbacks, and registration under `rest_api_init`. citeturn31search0turn31search1turn31search8

The post-creation contract should be:

- **Post title:** exact album/release name
- **Post body:** empty string, or the absolute minimum placeholder if your theme/editor needs non-empty content
- **Post status:** `publish`
- **Categories:** type category (`Album`, `EP`, `Single`, or `Compilation`) plus **`Relisten`** when applicable
- **Tags:** all Spotify artist names, commas stripped to match existing behavior
- **Featured image:** uploaded artwork assigned via `featured_media`

WordPress’ posts endpoint supports `featured_media`, and post collections can be filtered by categories, tags, slug, status, and search-related parameters. The categories and tags endpoints support listing, creating, searching, and filtering by slug. citeturn8view1turn8view2turn8view3turn8view4turn10view0turn10view1

The media workflow should be:

1. Resolve or create required category IDs
2. Resolve or create required tag IDs
3. Download the Spotify image bytes locally
4. Upload them to `POST /wp/v2/media`
5. Create the post with `featured_media=<media_id>`
6. Optionally update the media item to set alt text and associate it to the post
7. Persist the WordPress post ID and media ID locally

WordPress exposes `POST /wp/v2/media` and `POST /wp/v2/posts`, and the media schema includes fields such as `alt_text`, `description`, and `post`. citeturn8view7turn8view8turn8view0

The safest transactional behavior is:

- If media upload succeeds but post creation fails, attempt to delete the orphaned media item immediately.
- If the delete fails, log the orphaned `media_id` and keep going; do not lose the failure.
- If post creation succeeds but Discord notification fails, **do not roll back WordPress**. Store a “notification pending” event and retry the Discord notification separately.

That is the right balance between cleanliness and not overbuilding a distributed transaction system.

For **Undo post**, the correct action is to move the post to Trash, not hard-delete it. WordPress’ posts endpoint supports `DELETE /wp/v2/posts/<id>`, and the `force` parameter is specifically documented as the flag that bypasses Trash and forces deletion. Therefore your undo path should delete **without** `force=true`. If the local database already marks the post as trashed, or if WordPress reports that it is already gone/trashed after a prior successful undo, treat the second press as an idempotent success. citeturn8view6

One subtle but important implementation detail: if you want the standalone service to write **Spotify IDs and other custom meta** into WordPress post meta through REST, those meta keys must be registered with `show_in_rest=true`, or exposed via a custom endpoint/REST field. WordPress documents `register_meta()` for exactly this, and warns that REST exposure is opt-in. This is the strongest reason to consider the optional helper plugin even if the rest of the runtime stays standalone. citeturn32search0turn32search1turn32search3

My recommendation is therefore:

- **Pure REST only** if you are happy with standard post, taxonomy, media, and duplicate-cache behavior
- **Standalone service + tiny helper plugin** if you want:
  - exact duplicate-check endpoint
  - REST-exposed Spotify meta
  - a guaranteed place to preserve site-specific post conventions

## Discord control plane

Discord should be treated as the **operator console** for the service, not as a second database and not as the primary source of truth. The database owns release state; Discord presents actions and confirmations.

The right command surface is smaller than the initial brainstorm. I recommend this final core set:

| Command | Purpose | Keep? |
|---|---|---|
| `/inprogress` | View and manage active release lifecycles | Yes |
| `/current` | Show current listening target and allow manual publish | Yes |
| `/service` | Health/status/debug summary for the daemon | Yes |
| `/history` | Previously published/tracked history | Optional future |
| `/relisten-queue` | Dedicated relisten inbox | Optional future |

That keeps the first version focused.

Discord’s application commands are the native slash-command interface, and buttons/select menus are message components that generate interaction payloads when clicked. That is exactly the interaction model you need here. citeturn16view2turn16view1turn16view3

The most important operational rule is response timing. Discord’s receiving/responding docs say interaction tokens are valid for **15 minutes**, but you must send the **initial response within 3 seconds**, otherwise the token is invalidated. Anything that may hit WordPress or Spotify should therefore immediately **defer** the interaction and then edit/follow up after the slow work completes. citeturn17view0turn17view1turn17view2

The `/inprogress` UX should not try to put three buttons next to every release in a ten-item list. That gets messy quickly. The cleanest design is:

- The message shows up to **10 releases per page**
- A **select menu** lets you pick one release on the page
- Buttons below the list do `Prev`, `Next`, and `Refresh`
- Selecting a release opens a detail view with:
  - `View tracks`
  - `Publish early`
  - `Delete progress`
  - `Back`

Discord components already support `custom_id`, selects, and buttons, and `discord.py` views make this pattern straightforward. citeturn17view5turn3view2

The `/current` command should work differently. It should query the live current playback state and present:

- release title
- artist list
- computed release type
- raw Spotify album type
- whether the current playback would count automatically
- a `Publish` button
- optional `Open on Spotify` URL button

The `Publish` button is the **manual path for Singles**, but I recommend allowing it for any resolvable release, not just Singles. That gives you a manual override path even when auto-tracking does not apply, which is useful when you are listening from a playlist but still want to publish the release tied to the current track.

For auto-generated prompts, the service should send:

- **75% early prompt:** `Publish now` / `Wait for full completion`
- **Relisten prompt:** `Post as Relisten` / `Ignore`
- **Undo prompt after publishing:** `Undo post`
- **Delete confirmation:** `Confirm delete` / `Cancel`

These should be **public channel messages** if you want the action history visible, but the action handlers must reject every user except your authorized Discord user ID.

`discord.py` gives you the exact primitive you need for that: `View.interaction_check()`. Its docs explicitly call out the case where you want to ensure the interaction author is a given user. citeturn22view1

For production reliability, I recommend using **persistent views** for important operational buttons. `discord.py` documents persistent views as views with explicit `custom_id` values on all components and `timeout=None`, and it documents `Bot.add_view()` for registering them on startup. That is better than relying on the default timeout for critical actions like relisten approval or undo, especially because a service restart should not silently invalidate your control surface. citeturn22view0turn22view1turn22view2

The pattern should be:

- Every actionable message stores a row in the database with:
  - `prompt_type`
  - `release_id` or `wordpress_post_id`
  - `discord_message_id`
  - `state = pending|accepted|declined|used|expired`
- Every button click re-checks the database state before acting
- If the prompt is already resolved, reply ephemerally with “This action is no longer active; rerun `/inprogress` or `/current`.”

That gives you both security and idempotence.

The optional bot presence is fine, but it should stay modest. `discord.py` supports `change_presence()`, but you should update presence only when the displayed album changes or on a slow heartbeat, not every poll tick. Otherwise the bot becomes noisy for no real product gain. citeturn21view6

## Data model, logging, and reliability

The right database is **SQLite**. This is a single-user local automation tool, not a service that needs separate DB infrastructure. Use SQLite in WAL mode, keep all state local, and wrap state transitions in explicit transactions.

The schema I recommend is:

| Table | Purpose |
|---|---|
| `release_lifecycle` | One row per in-progress or completed tracked release lifecycle |
| `release_artist` | Artists attached to a lifecycle, with normalized names |
| `release_track` | Full flattened track list for the lifecycle, with countability/listened flags |
| `wordpress_link` | Post/media linkage and undo state |
| `discord_prompt` | Pending and resolved Discord actions/prompts |
| `audit_event` | Business-level event stream |
| `service_state` | Small key/value store for cursors, startup markers, etc. |

The `release_lifecycle` row should include at least:

- internal lifecycle ID
- Spotify album ID, URI, URL
- title and normalized title
- raw Spotify album type
- computed release type
- cover URL
- release date and precision
- full normalized artist-set fingerprint
- countable track total
- listened track total
- progress ratio
- `prompt_75_sent`
- `duplicate_state`
- `duplicate_match_post_id` if found
- `relisten_prompt_state`
- lifecycle status:
  - `active`
  - `awaiting_75_decision`
  - `awaiting_relisten_decision`
  - `publishing`
  - `published`
  - `deleted`
  - `ignored_single`
  - `trashed_post`
- first seen / last seen / completed / published / terminal timestamps

`release_track` should include:

- lifecycle ID
- flattened position
- disc number
- track number
- Spotify track ID
- relinked source track ID if known
- track title
- normalized title
- duration
- is local
- is countable
- restriction reason
- listened boolean
- listened-at timestamp
- listened source (`playback`, `recent_backfill`, `manual`)

This is the minimum structure required to answer all of the product questions without recomputation hacks.

The service should also keep a **small durable event log in the database** and a **structured rotating file log on disk**. The split should be:

- **DB audit events:** major business-state transitions
  - release created
  - track marked listened
  - 75% prompt sent
  - duplicate found
  - relisten prompt sent
  - post published
  - post trashed
  - progress deleted
- **File logs:** operational detail
  - poll summaries
  - HTTP retries
  - auth refreshes
  - 204/no-playback events
  - rate-limit handling
  - unexpected payloads
  - stack traces

That avoids shoving every poll into SQLite forever while still giving you durable business history.

The failure philosophy should stay simple and match the brief:

- unusable playback state: ignore and log
- WordPress unavailable: log, retry with bounded policy, keep lifecycle state
- Discord available but prompt send fails: log and retry later
- one action repeated twice: treat idempotently where possible
- service restart: recover from DB and continue

Spotify explicitly documents `429` rate limits and recommends backoff/retry behavior. Discord interaction timing is strict on initial response windows, but followup/edit behavior exists after deferral. WordPress Application Passwords are individually revocable and expose usage metadata, which is useful operationally if you ever need to rotate or audit credentials. citeturn29search3turn17view2turn12view1turn7view6

For secrets, use this model:

- **Spotify client ID / secret / refresh token:** secret store or a restricted local config file
- **WordPress Application Password:** secret store or restricted local config file
- **Discord bot token:** secret store or restricted local config file

If you decide to use Spotipy anywhere, its docs explicitly support custom cache handlers, which is the right place to control token persistence rather than leaving cache behavior implicit. citeturn24view1

The main implementation pitfalls to avoid are:

- treating device IDs as stable forever
- using fuzzy duplicate matching
- counting from playlists because the album ID happens to match
- poll intervals so slow that short tracks are often missed
- poll intervals so fast that you trigger needless rate-limit pressure
- relying on Discord message state instead of the database
- writing WordPress custom meta through REST without registering it for REST exposure
- building the whole runtime inside WordPress/PHP

There are only a few genuinely open product questions left, and none of them block implementation:

- **Should published posts use the completion timestamp or some other post date convention?**  
  The uploaded plugin appears to support setting a date, but the brief does not require backdating. My default recommendation is **publish timestamp = post timestamp**.
- **Does the public WordPress site already render the stored Spotify URL/logo/attribution somewhere?**  
  Verify this before launch because Spotify’s artwork/metadata attribution rules matter.
- **Do you want the optional WordPress helper plugin in v1, or only if pure REST becomes annoying?**  
  I would ship v1 without it unless you specifically want REST-exposed custom meta immediately.

## Implementation sequence and checklist

The cleanest build order is:

1. Create the repo and config system.
2. Build Spotify auth and playback polling first.
3. Add the SQLite schema and state transitions.
4. Add release classification and progress logic.
5. Add duplicate detection against WordPress.
6. Add WordPress publish + trash flows.
7. Add Discord slash commands and views.
8. Add recovery logic, logs, and tests.
9. Add optional helper plugin only if needed.

The testing strategy should mirror that order. Do not start with end-to-end tests only. Start with deterministic unit tests for classification, normalization, and progress transitions; then use integration tests with mocked Spotify/WordPress/Discord APIs; then run manual scenario passes against your real local stack. The scenarios that matter most are:

- qualifying album-context play starts an in-progress release
- playlist-context play does **not** count
- shuffled playback does **not** count
- track replay does **not** double-count
- multiple albums remain active simultaneously
- 75% prompt fires once
- 100% completion auto-posts
- duplicate causes relisten prompt rather than auto-post
- manual `/current` publish works for Singles
- undo moves post to trash
- same release listened again later becomes a new lifecycle
- restart during active progress does not corrupt state
- a missed poll is recovered acceptably
- unauthorized Discord click is rejected cleanly

The implementation checklist below is the project tracker I would hand to an engineer.

| Status | Area | Task | Done when |
|---|---|---|---|
| [ ] | Repo | Create Python project, config loader, secret loading, logging bootstrap | Local service starts with validated config and no hard-coded secrets |
| [ ] | Spotify | Implement Authorization Code auth + refresh flow | Service can obtain and refresh tokens without re-login |
| [ ] | Spotify | Implement `GET /me/player` poller with adaptive intervals | Poll loop runs continuously and records normalized playback snapshots |
| [ ] | Spotify | Implement release fetch with `/albums/{id}` and `/albums/{id}/tracks` | Full release metadata and flattened track list are available |
| [ ] | Rules | Implement plugin-compatible release classification | Unit tests pass for Album / EP / Single / Compilation cases |
| [ ] | Rules | Implement qualifying playback evaluator | Unit tests pass for album context, playlist context, null context, shuffle, local tracks, paused playback |
| [ ] | DB | Create SQLite schema and migration bootstrap | All tables created and migration path documented |
| [ ] | DB | Persist release lifecycles, artists, tracks, prompts, audit events | State survives restarts and can be inspected manually |
| [ ] | Progress | Implement boolean once-per-track counting | Replays do not increase progress |
| [ ] | Progress | Implement 75% prompt transition | Flag persists and no duplicate prompt is sent |
| [ ] | Progress | Implement 100% completion transition | Completion triggers publish or relisten branch exactly once |
| [ ] | Recovery | Implement conservative recently-played backfill | Missed-poll tests recover tracks only into verified active sessions |
| [ ] | WordPress | Implement Application Password auth client | Authenticated REST calls succeed against local WP |
| [ ] | WordPress | Implement taxonomy resolution/creation for categories and tags | Required terms are resolved or created idempotently |
| [ ] | WordPress | Implement duplicate-index cache from posts + tags | Historical duplicate lookup works by normalized title + unordered artist set |
| [ ] | WordPress | Implement media upload workflow | Spotify cover uploads to media library and returns media ID |
| [ ] | WordPress | Implement post publish workflow | Published post appears with correct title, categories, tags, and featured image |
| [ ] | WordPress | Implement undo-to-trash workflow | Pressing Undo moves the correct post to Trash and is idempotent |
| [ ] | Discord | Register `/inprogress` command | Command lists active releases with paging/select-based navigation |
| [ ] | Discord | Register `/current` command | Command shows current release/type and supports manual publish |
| [ ] | Discord | Register `/service` command | Command returns last poll time, active releases, auth health, and queue state |
| [ ] | Discord | Implement authorized-user gate with `interaction_check` | Unauthorized clicks get a clean ephemeral rejection |
| [ ] | Discord | Implement persistent views for critical actions | Buttons still work after process restart when action state is pending |
| [ ] | Discord | Implement early-publish, relisten, delete-confirm, and undo flows | All buttons execute the correct DB transition and side effects |
| [ ] | Logging | Add rotating structured log file and DB audit event writes | Important events are searchable without leaking secrets |
| [ ] | Testing | Add unit tests for classification, normalization, progress, duplicate matching | Core logic is deterministic and covered |
| [ ] | Testing | Add integration tests for Spotify, WordPress, and Discord clients | Clients handle 204/401/403/429 and success paths correctly |
| [ ] | Ops | Add Docker/systemd runtime and restart policy | Service survives reboot and stores DB/logs on persistent volume |
| [ ] | Optional | Add tiny WordPress helper plugin for duplicate check/meta exposure | REST helper endpoints exist with strict permission callbacks |
| [ ] | Launch | Run manual full scenario pass against local Spotify + WP + Discord | End-to-end flow works from listening to published/undone post |

The final recommendation, after comparing the alternatives and pressure-testing the ugly edge cases, is:

**Build a standalone local Python daemon first. Keep WordPress as a REST target, not as the runtime. Preserve the uploaded plugin’s release heuristics and tag/category conventions. Use Spotify playback state as the real-time truth, recently played only as conservative recovery, SQLite as the durable state store, and Discord as the operator console with persistent authorized-only controls.**

That is the cleanest design, the most implementation-ready design, and the one least likely to turn into the kind of “implementation hell hole” you explicitly asked to avoid.