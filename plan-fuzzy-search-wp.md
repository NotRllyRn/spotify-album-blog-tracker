# Spec: Fuzzy search of cached WordPress posts + `/editor` shortcut

> **Status:** Implemented. Slice 1–6 shipped on `main` via the YAGNI
> approach: one pure module (`src/search.py`), one View module
> (`src/search_view.py`), two slash commands in `discord_bot.py`, one
> staleness signal written by `Publisher._save_post_cache_validation_state`,
> and one audit event (`search_opened_editor`).
>
> **Triage:** `ready-for-agent` — alignment is complete, this spec is shaped
> and ready to be sliced into tickets via `to-tickets` and implemented.
>
> **Source conversation:** grilled against `PROGRAM_DOCUMENTATION.md`,
> `README.md`, `plan-interactive-edit.md` and the implemented
> `src/editor_view.py` on 2026-07-09. Verbal commitments from that
> conversation are encoded as **[decision]** annotations throughout this
> spec.
>
> **Vocabulary:** SCF = Smart Custom Fields (`acf` block on a WP post).
> Editor = the persistent `EditorView` already shipping in
> `src/editor_view.py`, with `PrePublishSink` (writes to in-memory
> `Release` + DB) and `PostPublishSink` (PATCHes live WP `acf` via
> `Publisher.update_post_scf`). Cache = `wordpress_post_cache` SQLite
> table populated by `Publisher.refresh_post_cache`. Audit =
> `audit_event` table written via `db.log_audit_event`.

---

## Problem Statement

When the operator wants to **fix up an existing published album post on
the blog** — change a rating, add notes, set an `unreleased` flag, fix a
mistyped title that survived into SCF, attach a `music_mood_tags` row —
they currently have one path: a `PublishedPostActionView` button that
appears on the *publish-confirmation* DM right after `release_published`.
That button is only available for ~24 hours of post-publish retention,
and the operator has to know exactly which post they want to edit.

After that window closes, the operator has no way to revisit a post's
SCF block from the bot. They have to log into WordPress and edit SCF by
hand, which is how the `music_favorite` / `music_notes` /
`music_rating` / `unreleased` / per-track `highlight` fields were
edited before `src/editor_view.py` shipped. The goal of this spec is to
extend the editorial surface: any cached WP post is reachable by fuzzy
search from a Discord slash command, and the existing `EditorView`
opens against the chosen post in post-publish mode, exactly as if the
operator had hit "Edit metadata" inside the post-publish retention
window.

This matters because: (a) the blocker for v1 of the `EditorView` was
"how do I find a post by name?" — this spec answers it. (b) The blog
already has hundreds of legacy posts from `Wordpress-PostToAlbum-Script`
that were never auto-fill'd and never carry `music_favorite`. A
back-fill pass needs an editor reachable by name. (c) Operator confidence
that *every* published post is editable through the same UI, regardless
of when it was published, closes a parity gap between the publish-time
edit affordance and the long-term edit affordance.

## Solution

Add two new slash commands and one Discord `View`:

- `/search query:string` — primary entry. The bot opens an ephemeral
  picker embed with the top-N fuzzy matches from the local cache and a
  `StringSelect` of the chosen post IDs. The operator picks one; the
  bot DMs the existing `EditorView` against that WP post in
  post-publish mode. Subsequent SCF edits round-trip through the
  `PostPublishSink`.
- `/editor post_id:int` — secondary entry. The operator already has a
  WP post ID from the publish notification or from a `sqlite3` query
  against `wordpress_post_cache`. The bot opens the same persistent
  editor against that ID in DM without fuzzy search. Acts as a
  power-user shortcut and as a no-cache-first bypass.
- `SearchPickerView` — the ephemeral in-channel picker. Carries a
  `StringSelect` of up to 25 post options, plus three buttons: `Search
  again`, `Match loosely` (re-runs at lower trigram threshold against
  the same cache), `Search live` (issues a WP `search` request and
  applies the same fuzzy ladder to the response).

Both commands run through the existing `_check_authorized` gate. The
picker view inherits its auth from `PromptView.interaction_check`.
The persistent editor DM message is delivered via the existing
`open_post_publish_editor(…)` helper, which we do not modify.

**Cache freshness fallback.** When the cache is empty (cold start)
or hasn't been refreshed in over `WORDPRESS_CACHE_MAX_AGE_HOURS`, the
picker silently searches live WordPress instead and tags the picker
embed so the operator knows. This is audited in the
`search_opened_editor` audit event.

**Audit.** Every successful picker → editor handoff (cache *or* live)
writes an `audit_event` row of type `search_opened_editor` with
`post_id`, the original query string, and the funnel source
(`"cache"` or `"live"`).

## User Stories

These are independently checkable. Tests live in
`tests/test_unit.py` (existing precedent: 114 tests, target 130+).

1. As the operator, I want to run `/search pink floyd moon` and see
   the post-publish album "The Dark Side of the Moon" as the top
   match, so that I can edit its SCF fields without remembering the
   exact title.

2. As the operator, I want to type `/search dark side` and see all
   Pink Floyd "Dark Side" releases ranked above the threshold, even
   if some are spelled with extra whitespace or different
   capitalization, so that case and formatting don't matter.

3. As the operator, I want to type `/search pnik floyd moon` (typo
   on the artist) and still see Pink Floyd posts, so that a single
   transposed letter doesn't kill my search.

4. As the operator, I want to type `/search something obscure` and
   receive an empty-state picker with `Match loosely` and `Search
   live` buttons, so that I have an obvious next step instead of a
   confusing "no results" silence.

5. As the operator, I want `/search dark floyd` to NOT match "Dark
   Tranquillity" or other unrelated artists, so that AND-token
   semantics keep false positives off the picker.

6. As the operator, I want the picker to show at most 9 candidates at
   a time, so that I can scan the list without overflow.

7. As the operator, I want equal-score matches to be broken by
   newest-WP-ID-first, so that recent posts win ties.

8. As the operator, I want to click a picker's `StringSelect` option
   and have the editor open in my DM within ~3 seconds, so that the
   round-trip feels synchronous.

9. As the operator, I want the picker to remain on the channel after
   I pick, so that I can re-pick if I made a mistake without running
   `/search` again.

10. As the operator, I want to click `Match loosely` on an empty
    picker and have the threshold drop to `0.30`, so that partial
    matches surface immediately.

11. As the operator, I want to click `Search live` on an empty picker
    and have a `GET /wp/v2/posts?search=…` issued, ranked by the same
    rapidfuzz ladder as the cache path, so that the live funnel
    behaves symmetrically with the cache path.

12. As the operator, I want to click `Search live` repeatedly and see
    the picker re-render in place, so that I can iterate.

13. As the operator, I want `/search` to be empty-safe — i.e. if the
    cache has 0 rows, the picker should auto-route to live search and
    banner the picker embed with a ⚠️ explanation.

14. As the operator, I want `/search` to detect a stale cache (no
    refresh in `WORDPRESS_CACHE_MAX_AGE_HOURS`) and route to live
    search with the same banner, so that the operator knows the
    picker may be incomplete.

15. As the operator, I want to run `/editor post_id:4567` and have the
    editor open against that WP post, even if `4567` is not in the
    local cache (cold cache or older post).

16. As the operator, I want `/editor post_id:99999` to produce an
    ephemeral `no post #99999` error, not a stack trace.

17. As the operator, I want `/search` and `/editor` to only respond to
    me (`DISCORD_USER_ID`); other users should see an ephemeral
    rejection — same as the existing slash commands.

18. As the operator, I want every picker pick to write an
    `audit_event` row tagged `search_opened_editor`, so that I can
    see which posts I've edited through the bot.

19. As the operator, I want the picker embed title to echo my query,
    so that I can confirm what I typed from a glance.

20. As the operator, I want each picker candidate row to show the WP
    post ID and the public WordPress link, so that I can verify the
    target without clicking.

21. As the operator, I want successful search-driven editor opens to
    land in the existing `EditorView` against the same WP post the
    picker referred to, so that the editing UX is identical to
    post-publish editing.

22. As the operator, I want long WP titles (> 100 chars) to be safely
    truncated in the `StringSelect` option label so that Discord's
    100-char label limit doesn't reject the message.

23. As the operator, I want unicode-normalized input — typing `JVKE`
    should match `jvke` in titles, so that accent/case mismatches
    don't matter.

24. As the operator, I want the entire `/search` flow to honour the
    Discord 3-second interaction deadline (defer immediately, do all
    work in `interaction.followup`), so that the bot never times out.

25. As the operator, I want `/editor` to validate `post_id` against
    WP on a cache miss (cache-first, live-second), so that I don't
    open a phantom editor against a deleted post.

26. As the operator, I want the picker to use `StringSelect` rather
    than per-track buttons so that the picker fits Discord's
    one-`StringSelect`-per-row component limit cleanly.

## Implementation Decisions

[decision] **Search target: cache-only first, live WordPress as fallback.**
The bot reads candidates from `wordpress_post_cache` populated by
`Publisher.refresh_post_cache` and only escalates to live WP when the
cache is empty, stale, or yields zero above-threshold matches. The
cache is the happy path because it costs zero HTTP per query and is
already validated for the duplicate-detector use case.

[decision] **Scoring: per-token AND semantics with rapidfuzz
fallback.** Multi-word queries split on whitespace; every token must
score ≥ `FUZZY_BASE_THRESHOLD` (default `0.55`) against the haystack
`title + " " + " ".join(artists)` (all NFKC-normalised via
`utils.normalize_text` and `utils.normalize_artist_name`). Per-token
substring pass-through first; if the token is not a substring, the
rapidfuzz `WRatio` score against the haystack is used (mapped to
`[0.0, 1.0]`). The row's overall score is the max token score, used
for ranking and tiebreakers. Tokens are deduped and trimmed.

[decision] **rapidfuzz is the only new dependency.** Add
`rapidfuzz>=3.0` to `requirements.txt`. No other new packages. This is
a SUT-level decision because the codebase otherwise stays deps-clean.

[decision] **Cap + ordering:** top 9 by score, tiebreaker `wp_id
DESC` (newest WP ID first). Mirrors `InProgressView`'s `page_size=9`
convention. Discord `StringSelect` caps at 25 options per menu — we
stay at 9 to keep the picker scannable in a single glance.

[decision] **Result picker preview:** an ephemeral in-channel embed
whose description lists the top 3 matches as numbered bullet rows.
Each row: `**Title** #wp_id · public_link`. No thumbnail, no score,
no artist detail (the `StringSelect` option label already carries
artist for disambiguation). A footer field notes "Top 9 of N above
threshold" or "≥0.30 (loose)" depending on the threshold.

[decision] **Two source modes, one picker:** the picker is the same
UI for both "searched from cache" and "searched live." The source
funnel is recorded for audit but does not change the visual layout.
Only one banner is conditionally rendered: a ⚠️ line in the embed
description saying "Cache was empty / stale — searched live
WordPress" when the funnel was live.

[decision] **Threshold ladder:** `FUZZY_BASE_THRESHOLD = 0.55`
initial and `FUZZY_LOOSE_THRESHOLD = 0.30` after `Match loosely`.
Both are constants at the top of the search module. Tunable.

[decision] **Cache freshness signal:** a single
`wordpress_post_cache.last_synced_at` key in the `service_state`
table. `Publisher.refresh_post_cache` writes this after a successful
full replace. Stale threshold is `WORDPRESS_CACHE_MAX_AGE_HOURS = 24`.
No new SQL migration — `service_state` schema is unchanged.

[decision] **Live search HTTP:** the existing
`WordPressClient.get_posts(search=…, per_page=100, page=1)` already
supports search via `**params` passthrough; **no new HTTP plumbing.**
The exact `search=` Response is then fed through the same scoring
function used for cache rows. Symmetrical with the cache path.

[decision] **Editor handoff:** the picker selection calls
`open_post_publish_editor(publisher, wordpress, post_id,
release_title, initial_acf, on_open)` already shipped in
`src/editor_view.py`. No changes to the editor View or its sink
implementations. `release_title` is the WP post title; `initial_acf`
is read once via `WordPressClient.get_post_acf(post_id)` if not
already in hand.

[decision] **`/editor post_id` validation order:**

1. Cache hit (`wordpress_post_cache` contains the row) — use cached
   title + link.
2. Cache miss + `GET /wp/v2/posts/{id}` returns 200 — use live
   title + link, fetch `acf` from `?context=edit`.
3. Cache miss + WP 404/5xx — ephemeral error response, no editor
   open, no audit.

[decision] **Audit event:** every picker pick writes via
`db.log_audit_event("search_opened_editor", {"post_id": int, "query":
str, "source": "cache|live"})`. Skipping would lose the trail of
"which WP posts has the operator edited through this path," which is
useful both as a `Wordpress-PostToAlbum-Script` backfill metric and
as a debugging signal.

[decision] **Single deep-module seam.** All scoring, formatting, and
fallback logic is consolidated in one pure function
`search.search_for_posts(db, wordpress_client, query, *,
threshold, force_source) -> SearchOutcome` that returns a single
structured value. Discord `View`s and command handlers are thin
wrappers around it. Reason: this is the highest seam in the feature,
sits alongside the existing pure helpers (`utils.normalize_text`,
`inprogress.build_inprogress_page`), and gives tests something
durable to target. Tests cover behaviour at this seam; Discord-level
tests stay as mocks with the existing `TestDiscordBotEmbeds`
precedent.

[decision] **Discord interaction lifecycle.** Both `/search` and
`/editor` call `interaction.response.defer(ephemeral=True)`
immediately, then do their work in `interaction.followup.send(…)`.
This honours the 3-second Discord interaction deadline. The picker
DDMs the editor via the existing `_send_dm` path; the picker message
in-channel is edit-replied with a "✓ opened editor for post #N in
your DM" confirmation.

[decision] **No SQL migration.** The cache table already exists in
schema version `001_initial_schema.sql`. No schema changes.

[decision] **Single-user model.** Both commands honour
`_check_authorized` (the existing gate). Pickers honour their parent's
`PromptView.interaction_check`. No collectives, no shared editor.

[decision] **Custom_id namespace:** every picker interaction
custom_id is prefixed `search:` ≤ 100 chars total. The `StringSelect`
values are post IDs (numeric).

[decision] **Open implementation details — resolved as
recommendations, push back if any read differently:**

- A short `nonce` is included in the picker `custom_id` so two
  `/search` invocations don't reuse the same `StringSelect` callback
  routing.
- The picker `Done` button explicitly deletes its own message; the
  picker auto-deletes after 15 min of Discord idle per Discord
  behaviour.
- The cache-first path runs synchronously and is fast — no task
  spawning required.
- The live search path is `asyncio`-threaded via the existing
  `httpx.AsyncClient`; no new HTTP client.
- Picker View lifetime uses the standard `discord.ui.View( timeout =
  None )` pattern, matching the post-publish editor's lifetime.
- The fuzzy ladder, threshold constants, and result cap are
  top-of-module constants in the search module for easy tuning.

## Testing Decisions

[decision] **Seam under test:** the single function
`search.search_for_posts(...)` is the deep-module seam under test.
Every behavioural user story (`1`–`25`) is encoded as a test at this
seam or at the View/command seam, whichever is closer.

[decision] **What makes a good test:**

- *Test external behaviour, not implementation details.* Tests at the
  `search_for_posts` seam compare `SearchOutcome` shapes. They do not
  assert call counts on internal helpers (`normalize_text`,
  `WRatio`), they assert the *result* of having called them. Discord
  View tests assert `Embed.title`, `Embed.description`,
  `StringSelect.options` — the visible shape — not the callback
  implementation.
- *One assertion per story where possible.* Each test file maps to a
  specific user story (1-test-1-story). When a story is too compound,
  the test splits into two.
- *Cache + live parity.* Tests for stories 11 and 1 both verify the
  same scoring rules. Any divergence is a bug.

[decision] **Modules tested:**

- `src/search.py` (new module) — every behaviour at the pure seam:
  scoring, threshold, AND semantics, cache vs live routing, staleness
  signal handling. ~6 tests.
- `src/search_view.py` (new module) — picker embed and `StringSelect`
  shape, custom_id namespacing, label truncation for long titles,
  button labels and visible text. ~3 tests.
- `src/discord_bot.py` (modified) — `/search` and `/editor`
  registration: defer-on-respond, ephemeral flag, audit log
  invocation. ~2 tests.
- Existing `tests/test_unit.py::TestPublisherPostCacheRefresh` —
  extend with one assertion: a successful `refresh_post_cache(force=True)`
  writes `service_state["wordpress_post_cache.last_synced_at"]` with a
  parseable ISO timestamp.

[decision] **Prior art for the tests:**

- `TestSavedLibraryService` (existing) is the closest prior art. It
  exercises a cache-vs-live-equivalent decision (validate by hash or
  full scan) at the same seam depth. The new search tests mirror its
  fixture style: build an in-memory `WordPressPost` list, mock
  `WordPressClient.get_posts` for live searches, assert on the
  ranked result.
- `TestDiscordBotEmbeds` (existing) is the prior art for View-level
  tests. The new picker tests mirror its style: construct a
  `SearchPickerView`, assert on `Embed.title` / `Embed.fields` /
  `View.children`.
- `TestPublisherSCFFill` (existing) is the prior art for log-write
  assertions. The new tests reuse the same `db.log_audit_event`
  mock pattern to assert `search_opened_editor` is written with the
  expected payload.

[decision] **What is *not* tested (deliberately):**

- Discord interaction lifecycle (defer/followup timing) — tested
  manually in `tests/manual/`-style smoke checks; automation would
  need a Discord test account.
- Real WP network reachability in CI — the live WP tests use
  `WordPressClient` mocks (HTTP `AsyncClient` patchable via existing
  test patterns).

## Out of Scope

- **Editing non-album posts.** All WordPress posts in the cache are
  searchable. Posts without an `acf` block open the editor with empty
  defaults — this is out of scope to filter against.
- **Category-based search filter.** Considered during grilling —
  user explicitly chose "search everything." A future spec could
  add `category` as an optional `/search` arg.
- **`music_mood_tags` editor.** The persistent `EditorView` already
  excludes mood tags (see `plan-interactive-edit.md §11`). Manual WP
  edits remain the only path; that does not change.
- **Bulk edits.** A "set favorite for all listens of album X" flow is
  not in scope; user did not request it.
- **Server-shared editors.** This spec is single-user. DM-only as
  before.
- **Saved-library (Spotify) search.** User confirmed "search the
  WordPress database"; Spotify saved-library is reachable through
  the existing `/random` and `/inprogress` flows and not added here.
- **Pre-publish metadata editor for saved-library rows.** Pre-publish
  editing of saved-library albums has no semantics today; the `Release`
  cache is the pre-publish path, and saved-library rows are
  Spotify-cached, not release-tracked.

## Further Notes

- **Backlog of legacy posts from `Wordpress-PostToAlbum-Script`**.
  Those posts were published without `music_favorite` /
  `music_notes` / `music_rating` / `unreleased` SCF keys. They
  survive this spec change intact — older posts open the editor with
  empty defaults and the operator can back-fill them in batches
  using `/editor post_id:N`. The audit trail under
  `audit_event.search_opened_editor` makes "which legacy posts have
  we touched" quantifiable — but filling them is not in this spec's
  scope.
- **The picker is *not* a replacement for `/inprogress` or `/random`.**
  It's a different question: "I know what post I want, I just don't
  know its WP ID." All three commands co-exist.
- **`/random`, `/inprogress`, `/current`, `/service` are untouched.**
  No behavioral changes there.
- **`EditorView` is unchanged.** This spec uses `EditorView` as-is
  via `open_post_publish_editor`. The post-publish editor View was
  designed to accept *any* WP post ID — that generality pays off
  here.
- **The spec ships alongside `plan-interactive-edit.md` (already
  implemented as `src/editor_view.py`) and `plan-backend-1.md` (already
  implemented as `Publisher._build_scf_payload`)**. It uses the
  surfaces they exposed without modifying them.
- **Slice order**: scoring → picker View → `/search` → `/editor` →
  staleness plumbing → audit + cleanup. Six PRs via `to-tickets`.
- **Triage label**: this spec is marked `ready-for-agent`. No
  additional triage needed.
