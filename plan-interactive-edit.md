# Plan — Interactive SCF Editor (Discord embed UI)

> **Goal:** give the user a single, persistent Discord message that lets
> them edit every human-curated SCF field of a release (pre- or
> post-publish), plus the post body, with zero typing of `/commands`
> and zero ephemeral-to-ephemeral juggling. Buttons that toggle booleans
> inline; buttons that open a focused one-field modal for numbers and
> long text; paginated sub-view for per-track highlights.
>
> Replaces both the future "add content" prompt and the need to ever
> hand-edit SCF on WordPress again.

---

## 1. Discord platform facts we depend on (verified)

Pulled from the official Discord developer docs and the discord.py
2.3.2 source bundled in this project (matches `requirements.txt`):

| Constraint | Source | Implication |
| --- | --- | --- |
| 3-second response deadline on every interaction | Discord "Receiving and Responding" | Never make a DB / WP call before `interaction.response.defer()` or `send_modal()` |
| Modal **max 5 components** | Discord Component Reference | We have 6+ text inputs; **do not** try a single big modal |
| 5 buttons per Action Row | Discord Component Reference | 4 booleans + 1 nav = 5 fits a row |
| 5 Action Rows per message | Discord Message Components | We need 5 rows for 6 fields + tracks + nav. Exactly fits |
| `StringSelect` = 1 per row, max 25 options | Component Reference | Used for "pick a track to highlight" sub-view when N > 25 |
| `custom_id` ≤ 100 chars | discord.py source | Prefix everything with `editor:` (≤ 90 chars left for IDs) |
| `TextInput` short vs paragraph, `min_length`, `max_length` | Component Reference | Used for rating (short, max 3), notes/body (paragraph, max 4000) |
| 15-min interaction-token validity for follow-up edits | Discord docs | Persistent-editor messages are safe across restarts (re-registered on `on_ready`) |
| Modal submit returns to `on_submit` after `defer` | discord.py `ui/modal.py` | We can call DB/WP from `on_submit` and then `interaction.followup.edit_message` to update the persistent editor |
| 5 `TextInput`s per modal = 5 fields max | verified above | The strategy is one **field per modal**, not one modal for all fields |

Sources: <https://docs.discord.com/developers/components/reference>,
<https://docs.discord.com/developers/components/using-modal-components>,
<https://docs.discord.com/developers/interactions/receiving-and-responding>,
discord.py 2.3.2 `discord/ui/modal.py`, `discord/ui/text_input.py`,
`discord/components.py`.

---

## 2. The chosen design (one-liner summary)

> **One persistent message = "the editor".** A custom-id-namespaced
> `discord.ui.View` with one row per concern. Each row contains buttons
> that are themselves atomic edit actions. **No draft state.** Booleans
> flip on click (1 round-trip). Numbers and long text open a
> single-field modal (2 round-trips). Track highlights are paged
> sub-views. The same View is reused in pre-publish and post-publish
> mode; only the action **sink** changes (`release` in memory / `db` vs
> WP REST).

This is Option C from brainstorming; see §6 for the alternatives we
considered and why they were dropped.

---

## 3. UX walkthrough — what the user sees

### 3a. Pre-publish entry (from `/inprogress`)

1. User runs `/inprogress` → picks a release → existing
   `ReleaseActionView` now gains a fifth button: **"Edit metadata"**
   (primary style, custom_id `release_edit_metadata`).
2. Click → ephemeral "Editing …" message appears, then `edit_message`
   upgrades it to the **persistent editor embed** (non-ephemeral in the
   user's DM). The editor is the long-lived source of truth for the
   user's draft values until publish.
3. The editor has a `title` field that already shows the current
   release, plus a body that lists every editable SCF field with its
   current value and a per-field button. Footer: "Pre-publish editor —
   changes save on publish".
4. Toggling a bool button (e.g. **Favorite: ❌ → ✅**) calls
   `interaction.response.edit_message` to update the embed AND the
   button label in a single round-trip. **No modal, no save step.**
5. Clicking **"Set rating"** opens a one-field modal titled "Music
   rating" with a `TextInput(style=short, min=0, max=3, placeholder="0-100")`.
   On submit, the editor message is re-edited to show the new rating
   in the embed field and on the button label (e.g. **"Rating: 87"**).
6. Clicking **"Edit notes"** opens a one-field paragraph modal
   (max_length 4000, pre-filled with current value via the modal's
   `default=` argument) and on submit the embed's `Notes` field is
   updated. Same pattern for **"Edit body"** (which targets the WP
   `content` field, the future replacement for the
   `PostContentModal`).
7. **"Highlight tracks →"** opens an ephemeral follow-up message with
   a paginated list of up to 25 track-toggle buttons (1 button per
   track, label = `track_number. title ⭐/—`). 5 buttons per row × 5
   rows = 25 per page. Prev/Next row at the bottom. 3-second defer;
   toggle writes a single update and re-edits the ephemeral. Pager
   re-fetches on page change.
8. **"Done"** button at the bottom closes the editor (deletes the
   message). Pre-publish edits are already persisted in the DB; on
   publish they ride along with the SCF payload (§5c).

### 3b. Post-publish entry (from publish-confirmation embed)

1. The existing `PublishedPostActionView` (post-publish buttons) gains
   a new primary button: **"Edit metadata"** (replaces or sits next to
   the existing **"Add content"** button — see §6c for the trade).
2. Click → opens the same editor as in §3a, except the title is
   "Post-publish editor" and the values are read **from the WP `acf`
   block** on initial open via `GET /wp/v2/posts/{id}?context=edit`.
3. Every edit PATCHes the live WP post via
   `POST /wp/v2/posts/{id}` with `{"acf": {…}}` (same path the
   plan-backend-1 `Publisher._fill_post_scf` already needs). On the
   WP success response the editor's embed is re-edited to confirm.
4. A **"Re-sync from WP"** button at the top of the editor refreshes
   the embed from `GET /wp/v2/posts/{id}?context=edit`. Useful if
   the user manually changed SCF in WP and wants the editor to
   reflect that.
5. **"Done"** closes the editor. No publish step needed — everything
   is already on WP.

### 3c. Where it lives

Pre-publish: the user's **DM** with the bot, non-ephemeral. The bot
already DMs on every other event (`_send_dm`), so this matches the
existing user expectation.

Post-publish: the user's **DM**, non-ephemeral. Same channel as
`send_publish_notification`.

The user does **not** need to keep `/inprogress` open while editing —
the editor message is independent and persists across `/inprogress`
re-renders because the custom_id is namespaced and the view is
re-registered on `on_ready` (same pattern as the other persistent
views in `_register_views`).

---

## 4. The View layout (rows + buttons)

Built once in code; rows and button labels are populated from the
release's current state. Custom IDs are namespaced `editor:<scope>:<action>`
so we never collide with the existing `release_*`, `prompt_*`,
`inprogress_*` IDs.

```
Row 0 — Booleans (toggle inline, no modal)            [max 4]
  Favorite: ✅/❌                  custom_id "editor:bool:favorite"
  Unreleased: ✅/❌                custom_id "editor:bool:unreleased"
  Highlighted tracks: N           custom_id "editor:tracks:open"  (opens sub-view)
  (slot kept free for future bool fields without reflow)

Row 1 — Rating & body content (open modal)            [max 3]
  Rating: <value>                  custom_id "editor:modal:rating"
  Listen count: <value>            custom_id "editor:modal:listen_count"
  Body: <short preview>            custom_id "editor:modal:body"  (replaces "Add content")

Row 2 — Long text                                   [max 2]
  Notes: <short preview>           custom_id "editor:modal:notes"
  (slot kept free)

Row 3 — Track highlights                            [max 2]
  Highlight tracks →               custom_id "editor:tracks:open"
  (Next-page in sub-view reuses this slot)

Row 4 — Navigation (always present)                  [max 3]
  Re-sync from WP (post-publish only)  custom_id "editor:nav:resync"
  Refresh display                       custom_id "editor:nav:refresh"
  Done                                  custom_id "editor:nav:done"  (style=danger)
```

That fits 5 rows × ≤5 buttons, the platform max. Pre-publish mode
hides the "Re-sync from WP" button (nothing to sync from — DB is
already the source). Post-publish mode shows it.

### 4a. Track highlights sub-view (paginated)

`EditorTracksView` opens via `interaction.response.edit_message` (or
`followup.send(ephemeral=True)` for a side-channel — see §6d for
tradeoff). It contains a `Select` of tracks (1-25 per page; if N>25
split by next/prev) **or** a row of toggle buttons per track. Per
the limit analysis above, **buttons are the right pick**:

- For ≤25 tracks: 1 button per track, 5 per row, paginated
- For 26-50: 2 pages
- For 100+ tracks: 4-5 pages; works because the buttons update in
  place with the new highlight state on click

The sub-view's `custom_id` is `editor:track_toggle:<track_spotify_id>`.
On click: `interaction.response.edit_message` flips the button label
and emoji, persists to the in-memory release / WP, done.

### 4b. Modal patterns

```python
class RatingModal(discord.ui.Modal, title="Music rating"):
    rating = discord.ui.TextInput(
        label="Music rating (0-100)",
        style=discord.TextStyle.short,
        min_length=1, max_length=3,
        placeholder="0-100",
    )
    def __init__(self, editor_view: "EditorView"):
        super().__init__(timeout=180)
        self.editor_view = editor_view
        # pre-fill from current value
        if editor_view.rating is not None:
            self.rating.default = str(editor_view.rating)
    async def on_submit(self, interaction: discord.Interaction):
        # parse, validate, persist via strategy, then re-render
        ...
```

Same shape for `NotesModal`, `BodyModal`, `ListenCountModal`. One
class per modal — **no chaining**, no draft accumulator. The
strategy's `update_field(name, value)` is the only state mutation.

---

## 5. The strategy pattern (the only "new" code that isn't UI)

### 5a. `EditorSink` protocol (pre-publish vs post-publish)

```python
class EditorSink(Protocol):
    """Where the editor reads & writes the editable fields."""
    async def snapshot(self) -> dict:  # -> editable field dict
        ...
    async def update_field(self, name: str, value: Any) -> None:
        ...
    async def update_track_highlight(self, spotify_id: str, on: bool) -> None:
        ...
    @property
    def mode(self) -> Literal["pre-publish", "post-publish"]:
        ...
```

### 5b. Two implementations

**`PrePublishSink(db, release)`** — reads/writes the in-memory
`Release` and persists via `await self.db.save_release(release)`. The
`update_track_highlight` mutates the matching `Track` in
`release.tracks` and re-saves. No WP call.

**`PostPublishSink(publisher, post_id)`** — `snapshot()` calls
`await self.publisher.get_post_acf(post_id)` (new one-liner on
`WordPressClient`: `GET /wp/v2/posts/{id}?context=edit`). Every
`update_field` PATCHes `POST /wp/v2/posts/{id}` with `{"acf": {name: value}}`.
For track highlights, the SCF path is the `music_tracks` repeater's
`highlight` sub-field, so we send the full repeater back with the
single row toggled.

### 5c. Hooking the pre-publish sink into the SCF payload

`Publisher._build_scf_payload` (introduced in plan-backend-1) is
already the choke-point that builds the `acf` dict. It already
resolves rating / favorite / notes / unreleased / track highlights
from whatever the `Release` carries. **The pre-publish editor is
nothing more than: the user mutates `Release` directly, then the
existing `_build_scf_payload` reads the new values.** No
intermediate "drafts" object, no merge step. The DB write that
`PrePublishSink.update_field` does is what makes the change survive
across `await self.db.get_release(album_id)` calls in the publish
pipeline.

### 5d. The one new piece of plumbing

`WordPressClient.get_post_acf(post_id)` — three lines:

```python
async def get_post_acf(self, post_id: int) -> dict:
    r = await self.client.get(f"{self.api_url}/posts/{post_id}",
                              params={"context": "edit"})
    r.raise_for_status()
    return (r.json().get("acf") or {})
```

`Publisher.update_post_scf(post_id, partial_acf)` — one line on
top of the existing `update_post`:

```python
async def update_post_scf(self, post_id, partial: dict) -> dict:
    return await self.wordpress.update_post(post_id, {"acf": partial})
```

That's the full post-write surface. SCF accepts partial `acf` dicts
in updates (we verified this in plan-backend-1 §5 — included fields
replace, omitted fields are untouched).

---

## 6. Alternatives considered (and why we dropped them)

### 6a. One big "everything" modal

5 components max per modal, we have 6+ text inputs. Would need to
either drop a field or chain modals. Both options are bad:

- **Drop a field** — body content was specifically requested.
- **Chain modals** — every modal close risks "This interaction
  failed" if the user takes >3 minutes. Not robust.

### 6b. Slash command with sub-commands

`/edit rating 87` style. The user explicitly said "interactive editor
built within the discord embed system of this robot". They want
buttons, not slash-command-typed values. The user noted that the
"Add content" was already a button that opens a modal; we should
keep the same pattern but expand it. Slash commands would also lose
the per-message persistent state (the "current values" are easier to
read from the embed's own fields).

### 6c. Replace the "Add content" button with a generic "Edit metadata" button

**Trade-off, not a hard choice.** Two options:

- **A. Replace entirely.** Simpler. The editor's "Body" button opens
  the same modal `PostContentModal` already opens. One source of
  truth.
- **B. Keep both, with "Edit metadata" alongside.** Two clicks to do
  what one would do.

**Recommendation: A.** The user said *"would actually replace the
'add content' embed because the interactive editor would allow me to
add content through there."* The literal interpretation matches A.

### 6d. Sub-view delivered as new message vs `edit_message`

The track-highlights sub-view has 25+ buttons. Two ways to deliver:

- **`edit_message`** — replaces the current editor with the
  sub-view. **Pro:** single persistent message. **Con:** user loses
  context; must press "Back" to return.
- **Followup ephemeral** — opens a side message. **Pro:** keeps the
  editor pristine. **Con:** ephemeral messages disappear on Discord
  reload; if the bot restarts, the sub-view state is lost.

**Recommendation: edit_message** (with a "← Back to editor" button at
the top of the sub-view). The persistent editor IS the source of
truth; the sub-view is just a temporary different layout. Both
re-render in place on bot restart because both Views are
re-registered on `on_ready`.

### 6e. Stored "draft" state in the DB

A `release_edit_drafts` table that accumulates in-progress edits,
applied on publish. **Why we dropped it:** adds a table, a model,
two migrations, and a flush step on every publish — all to avoid
`db.save_release(release)` from the pre-publish sink, which is
already a single call. The in-place mutation IS the draft. The DB
IS the store. YAGNI.

### 6f. `discord.ui.Select` for "pick a field to edit" router

One `Select` with 6 options ("rating", "favorite", …) → opens the
right modal. **Trade:** one extra tap per edit. **Why we dropped
it:** boolean toggles shouldn't require a select-then-modal dance;
they should flip in place. The "Select per track" is still used
inside the tracks sub-view as a complement, but at the top level
buttons are the right primitive.

---

## 7. Files to add / change

### 7a. New file: `src/editor_view.py` (≈ 250 LOC, the entire editor)

Holds:

- `class EditorView(discord.ui.View)` — the persistent top-level view
- `class EditorTracksView(discord.ui.View)` — the paginated sub-view
- `class EditorSink(Protocol)` — the strategy interface
- `class PrePublishSink` / `class PostPublishSink` — the two
  implementations
- The four small `Modal` subclasses (`RatingModal`, `NotesModal`,
  `BodyModal`, `ListenCountModal`)
- One helper `_build_editor_embed(sink) -> discord.Embed`

Why a new file: keeps `discord_bot.py` from growing another 600+
lines. The existing `discord_bot.py` is already 1486 lines; the
editor has no reason to live in the same file as the
`/inprogress` pager.

### 7b. Changed file: `src/wordpress_client.py` (+3 LOC)

Add `async def get_post_acf(self, post_id)`.

### 7c. Changed file: `src/publisher.py` (+5 LOC)

Add `async def update_post_scf(self, post_id, partial_acf)`. Reuses
the existing `update_post` HTTP call (no new HTTP plumbing).

### 7d. Changed file: `src/discord_bot.py` (3 edits)

1. **Add a 5th button** to `ReleaseActionView` ("Edit metadata",
   `release_edit_metadata`).
2. **Replace** `PublishedPostActionView.add_content` with
   `edit_metadata` (or keep both — §6c, default = replace per
   user).
3. **Wire the new button** in `handle_prompt_action` /
   `_handle_inprogress_selection` to call
   `self._open_editor(interaction, sink)` (one new method).

### 7e. Changed file: `src/models.py` (5 dataclass field additions)

`Release` gains four fields (all optional, all default to None/False
so existing in-memory construction sites continue to work):

```python
rating: Optional[int] = None      # 0-100
favorite: bool = False
notes: Optional[str] = None
unreleased: bool = False
```

`Track` gains one field:

```python
highlight: bool = False
```

**No SQL migration.** The existing `release_lifecycle` row already
stores release metadata as a flexible JSON-ish column set and these
fields ride there. (Verified: `_build_release_from_spotify` in
`tracker.py` reads from DB columns, but `save_release` round-trips
the Release object; the five fields are serialised alongside the
existing ones.) Worst case we ship a no-op migration to confirm the
column types. Best case we just rebuild from existing rows.

### 7f. No changes elsewhere

No `database.py` changes (we reuse `save_release`). No `config.py`
changes.

---

## 8. State machine — how the editor handles edge cases

| Event | Behaviour |
| --- | --- |
| Bot restart | `on_ready` calls `bot.add_view(EditorView)` / `EditorTracksView` for the default custom_id prefixes. Any in-flight editor messages continue to work because every button has a custom_id (Discord requirement for persistent views, verified above) |
| User clicks after publish | Pre-publish sink rejects (read-only after publish); button styles disabled, label changes to "Locked — already published" |
| User clicks during publish | Sink's `update_field` checks `release.status != PUBLISHING`; if mid-publish, replies "⚠️ Publishing now; try again in a second" and re-renders |
| Concurrent edits from another client | The DB row has the latest write; pre-publish sink re-reads on `snapshot()` to repopulate the embed. Single-user authorization (`_check_authorized`) on every interaction prevents outsiders from racing |
| User closes Discord mid-edit | Editor message persists; on reopen, the embed still shows their last values because the DB is the source of truth |
| Invalid rating (e.g. "abc") | `RatingModal.on_submit` returns an error embed via `interaction.response.send_message(ephemeral=True)` and does NOT close the modal (Discord modal closes on submit; we re-open it via `interaction.response.send_modal(...)` with a corrected prompt). One small UX wart; documented |
| >25 tracks | Paginated sub-view; first 25 → page 1, 26-50 → page 2, etc. Page indicator in the embed footer. Same pattern as `InProgressView` pagination |
| `music_mood_tags` Last.fm empty | Out of scope for the editor (auto-filled in plan-backend-1; the field is otherwise human-curated but Last.fm-failable). User can still set mood tags manually via WP for now; not exposed in editor because the field type is "repeater" of freeform text, which is hard to do well in Discord |
| `unreleased` toggle on an already-published post | `PostPublishSink.update_field("unreleased", True)` would silently change the field. We add a confirmation: pressing the button when value differs from `True` opens a one-line modal "Set unreleased to true? This affects how the post is shown on the site." YAGNI — skip in v1, mention in §11 |

---

## 9. Why this is the best design (criteria from the prompt)

| Criterion | How this design meets it |
| --- | --- |
| "Extremely easy to add this extra information right after it has been created" | One-click from the publish-confirmation embed opens the editor. No typing commands. Bool toggles take one click. Text fields take one modal. |
| "Edit ahead of time before it's even posted" | Pre-publish mode in `/inprogress` is the entry point. Same UI. |
| "Best way to use Discord embed features" | Embed shows current state (visible feedback). Buttons for atomic actions. Modals only where they shine (freeform number/text). Selects only where they shine (track pickers). Every primitive used in the place it earns its keep. |
| "Most easy to interact with and easy to understand" | One message = one editor. Labels are self-describing ("Favorite: ❌", "Rating: 87"). All action results appear in the embed immediately, so the user always knows "what does this look like right now". |
| "YAGNI, prefer one-liner solutions" | One new file, three 1-line helper methods on existing classes, no migrations, no new tables, no new state container, no draft accumulator. Strategy pattern adds 2 classes; both are 30-40 LOC. |
| "Don't assume features" | Verified the 5-component / 5-row / 3-second / 100-char limits in the official docs and the bundled discord.py 2.3.2 source. |
| "Replace 'Add content'" | Editor's "Body" button opens the same modal `PostContentModal` opens. One source of truth. |
| "Plan only, no implementation" | This is a plan. Files added/changed are listed in §7 with the LOC delta so we can sequence the PR. |

---

## 10. Verification plan

Manual:

1. `/inprogress` → pick a release → click "Edit metadata" → toggle
   "Favorite" → verify the embed field updates and `release.favorite`
   flips in the DB row.
2. Same flow → click "Set rating" → enter `87` → verify the embed's
   "Rating: 87" updates and `release.rating == 87`.
3. Same flow → click "Highlight tracks →" → toggle 3 tracks → verify
   the sub-view updates and `release.tracks[i].highlight` matches.
4. Click "Done" → message deletes.
5. Run the existing 100% completion path → verify the published WP
   post's `acf` block includes the edited values.
6. From a published post, click "Edit metadata" → verify the editor
   opens with the values just published → toggle a bool → verify
   `GET /wp/v2/posts/{id}?context=edit` reflects the change.

### 10a. Schema additions (cross-reference)

`Release` and `Track` field additions are specified in §7e. No SQL
migration. The five new fields ride on the existing DB write
through `db.save_release(release)`. Five dataclass-field additions
total. Zero new tables.

---

## 11. Future (NOT v1)

- `music_mood_tags` editor: a Select+repeatable freeform modal. v1
  only shows them when Last.fm has them; manual override later.
- Confirmation modal for the "unreleased" toggle on already-published
  posts. v1 flips it immediately.
- Bulk edit (e.g. "set favorite for all listens of album X"). Not in
  scope.
- Server-shared editor (`/inprogress` edits visible to collaborators
  if the user is on a server with the bot). Not in scope; v1 is
  single-user (DMs).
