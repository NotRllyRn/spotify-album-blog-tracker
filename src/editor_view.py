"""
Discord editor for the SCF fields exposed by plan-interactive-edit.

Provides a persistent ``EditorView`` plus ``EditorTracksView`` sub-view, with
strategy-shaped ``EditorSink`` implementations that route edits to either the
in-memory ``Release`` (pre-publish) or the live WordPress ``acf`` block
(post-publish).

YAGNI: no "draft" accumulator, no DB draft table, no Select-router screen.
A click on a bool button writes the field directly through the sink; a modal
is opened only when the field needs freeform input.
"""

import asyncio
import logging
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable, TYPE_CHECKING

import discord

from models import Release, Track

if TYPE_CHECKING:
    from database import Database
    from publisher import Publisher

logger = logging.getLogger(__name__)


# --- Field & view constants -------------------------------------------------

CUSTOM_ID_PREFIX = "editor"
NUM_TRACKS_PER_PAGE = 5
TRACKS_PAGE_ROWS = 5  # 5 buttons per row × 5 rows = 25 slots; we stay under
PROJECTION_PREFIX = "f_"  # SCF field name alias for edit persistence


def _field_id(name: str) -> str:
    """Return a Discord-safe custom_id for a field short-name."""
    return f"{CUSTOM_ID_PREFIX}:f:{name}"


# --- Mutable editor state ---------------------------------------------------


@dataclass
class EditorState:
    """Snapshot of the editable fields. Single mutable dict the sink updates."""

    rating: Optional[int] = None
    favorite: bool = False
    notes: Optional[str] = None
    unreleased: bool = False
    music_tracks: Optional[List[Dict[str, Any]]] = None  # full repeater rows (post-publish only)


def state_from_release(release: Release) -> EditorState:
    return EditorState(
        rating=release.rating,
        favorite=release.favorite,
        notes=release.notes,
        unreleased=release.unreleased,
    )


def state_from_acf(acf: Dict[str, Any]) -> EditorState:
    tracks = acf.get("music_tracks")
    if isinstance(tracks, list):
        music_tracks = [dict(row) for row in tracks if isinstance(row, dict)]
    else:
        music_tracks = None
    rating = acf.get("music_rating")
    return EditorState(
        rating=int(rating) if isinstance(rating, (int, float)) else None,
        favorite=bool(acf.get("music_favorite", False)),
        notes=(acf.get("music_notes") or None) or None,
        unreleased=bool(acf.get("unreleased", False)),
        music_tracks=music_tracks,
    )


# --- Strategy / sink --------------------------------------------------------


@runtime_checkable
class EditorSink(Protocol):
    mode: str  # "pre-publish" or "post-publish"
    state: EditorState

    async def snapshot(self) -> EditorState: ...
    async def update_field(self, name: str, value: Any) -> None: ...
    async def update_track_highlight(self, spotify_id: str, on: bool) -> None: ...


class PrePublishSink:
    """Sink that writes edits into an in-memory Release and persists via the DB."""

    mode = "pre-publish"

    def __init__(self, db: "Database", release: Release):
        self.db = db
        self.release = release
        self.state = state_from_release(release)

    async def snapshot(self) -> EditorState:
        # The in-memory Release is the source of truth; re-read it cheaply.
        self.state = state_from_release(self.release)
        return self.state

    async def update_field(self, name: str, value: Any) -> None:
        setattr(self.release, name, value)
        setattr(self.state, name, value)
        await self.db.save_release(self.release)

    async def update_track_highlight(self, spotify_id: str, on: bool) -> None:
        tracks_by_id = {t.spotify_id: t for t in self.release.tracks}
        track = tracks_by_id.get(spotify_id)
        if track is None:
            return
        track.highlight = bool(on)
        await self.db.save_release(self.release)


class PostPublishSink:
    """Sink that writes edits directly to WordPress via the live ``acf`` block."""

    mode = "post-publish"

    def __init__(self, publisher: "Publisher", wordpress_client, post_id: int, initial_acf: Optional[Dict[str, Any]] = None):
        self.publisher = publisher
        self.wordpress = wordpress_client
        self.post_id = post_id
        # Trust the initial snapshot if the caller already fetched it.
        self.state = state_from_acf(initial_acf) if initial_acf else EditorState()

    async def snapshot(self) -> EditorState:
        # Always re-fetch from WP so the editor reads the live SCF state.
        acf = await self.wordpress.get_post_acf(self.post_id)
        self.state = state_from_acf(acf)
        return self.state

    async def update_field(self, name: str, value: Any) -> None:
        scf_field = _project_field_to_scf(name)
        scf_value = _coerce_field_for_scf(name, value)
        await self.publisher.update_post_scf(self.post_id, {scf_field: scf_value})
        setattr(self.state, name, value)

    async def update_track_highlight(self, spotify_id: str, on: bool) -> None:
        await self.snapshot()
        rows = self.state.music_tracks or []
        matched = [r for r in rows if r.get("spotify_id") == spotify_id]
        if not matched:
            return
        for row in matched:
            row["highlight"] = bool(on)
        await self.publisher.update_post_scf(self.post_id, {"music_tracks": rows})


# --- Field-name projection (release field ↔ SCF acf key) -------------------

_FIELD_TO_SCF = {
    "rating": "music_rating",
    "favorite": "music_favorite",
    "notes": "music_notes",
    "unreleased": "unreleased",
}


def _project_field_to_scf(name: str) -> str:
    return _FIELD_TO_SCF.get(name, name)


def _coerce_field_for_scf(name: str, value: Any) -> Any:
    """Format a Release-style value into the SCF ``acf`` payload shape."""
    if name == "rating":
        # SCF number field accepts integer; represent unset as 0 is wrong; use "".
        return int(value) if value is not None else ""
    if name == "notes":
        return value or ""
    return value


# --- Embed rendering --------------------------------------------------------

BOOL_FIELDS = ("favorite", "unreleased")
SCALAR_MODAL_FIELDS = ("rating", "notes")
# listen-count, music_rating, music_notes, music_favorite, unreleased, body
TRACKS_OPEN_BUTTONS = {"editor:open:tracks"}


def build_editor_embed(release_title: str, state: EditorState, *, mode: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{'Pre-publish' if mode == 'pre-publish' else 'Post-publish'} editor",
        description=f"Editing: {release_title}",
        color=0x3498DB,
    )
    bool_lines = "\n".join(
        f"{name}: {'✅' if getattr(state, name) else '❌'}" for name in BOOL_FIELDS
    )
    embed.add_field(name="Booleans", value=bool_lines or "—", inline=True)
    rating_text = f"{state.rating}" if state.rating is not None else "—"
    notes_text = (state.notes or "—").strip()
    if len(notes_text) > 200:
        notes_text = notes_text[:199] + "…"
    embed.add_field(name="Rating", value=rating_text, inline=True)
    embed.add_field(name="Notes", value=notes_text, inline=False)
    if state.music_tracks:
        highlighted = sum(1 for row in state.music_tracks if row.get("highlight"))
        embed.add_field(
            name="Highlighted tracks",
            value=f"{highlighted}/{len(state.music_tracks)}",
            inline=True,
        )
    embed.set_footer(text=f"Mode: {mode}")
    return embed


# --- Modal classes (one input per modal, in line with the plan §4b) --------


class _SingleFieldModal(discord.ui.Modal):
    """Base modal that re-renders the editor embed after a successful submit."""

    field_name: str = ""  # override in subclasses
    field_label: str = ""
    placeholder: str = ""
    style: discord.TextStyle = discord.TextStyle.short
    max_length: int = 4000
    prefill: Optional[str] = None

    def __init__(self, view: "EditorView"):
        super().__init__(title=self.field_label, timeout=180)
        self.editor_view = view
        text_input = discord.ui.TextInput(
            label=self.field_label,
            style=self.style,
            placeholder=self.placeholder,
            default=self.prefill or "",
            max_length=self.max_length,
        )
        # TextInput has no custom_id limit problems at this length; safe.
        self.add_item(text_input)
        self._text_input = text_input

    @abstractmethod
    async def parse(self, raw: str) -> Any:
        """Convert the modal text to the typed value to write."""

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = await self.parse(str(self._text_input.value))
        except _InvalidInput as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        try:
            await self.editor_view._apply_field_edit(self.field_name, value, interaction)
        except _LockedForPublish:
            await interaction.response.send_message(
                "⏳ Publishing now; try again in a second.",
                ephemeral=True,
            )
            return


class _InvalidInput(ValueError):
    """Raised by modals when the input is malformed."""


class RatingModal(_SingleFieldModal):
    field_name = "rating"
    field_label = "Music rating (0-100)"
    placeholder = "0-100"
    max_length = 3

    def __init__(self, view: "EditorView"):
        self.prefill = str(view.state.rating) if view.state.rating is not None else ""
        super().__init__(view)

    async def parse(self, raw: str) -> Any:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise _InvalidInput("⚠️ Rating must be an integer between 0 and 100.") from exc
        if not 0 <= value <= 100:
            raise _InvalidInput("⚠️ Rating must be between 0 and 100.")
        return value


class NotesModal(_SingleFieldModal):
    field_name = "notes"
    field_label = "Notes"
    placeholder = "Editorial notes for the post (optional)"
    style = discord.TextStyle.paragraph
    max_length = 4000

    def __init__(self, view: "EditorView"):
        self.prefill = view.state.notes or ""
        super().__init__(view)

    async def parse(self, raw: str) -> Any:
        return raw


# Track-highlights sub-view --------------------------------------------------


class _LockedForPublish(RuntimeError):
    """Raised when a write is attempted while publishing."""


@dataclass(frozen=True)
class _TrackButtonKey:
    spotify_id: str
    page: int

    def custom_id(self) -> str:
        # Stay well under the 100-char Discord limit; the prefix is short.
        return f"{CUSTOM_ID_PREFIX}:track:{self.spotify_id}:{self.page}"


class EditorTracksView(discord.ui.View):
    """Paged per-track toggle view; opens via ``editor:open:tracks``."""

    def __init__(self, editor_view: "EditorView", page: int = 0):
        super().__init__(timeout=None)
        self.editor_view = editor_view
        self.page = page
        self._build_buttons()

    def _build_buttons(self) -> None:
        self.clear_items()
        # Back to editor button in its own row
        back = discord.ui.Button(
            label="← Back to editor",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{CUSTOM_ID_PREFIX}:nav:back_to_editor:{self.page}",
            row=0,
        )
        back.callback = self._back_to_editor
        self.add_item(back)

        tracks = self.editor_view.tracks_for_editor()
        start = self.page * NUM_TRACKS_PER_PAGE
        page_tracks = tracks[start : start + NUM_TRACKS_PER_PAGE]

        if not page_tracks:
            note = discord.ui.Button(
                label="No tracks on this page",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1,
            )
            self.add_item(note)
        else:
            for offset, track in enumerate(page_tracks):
                on = bool(track.highlight)
                button = discord.ui.Button(
                    label=f"{track.track_number}. {track.title[:40]}",
                    style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                    emoji="⭐" if on else None,
                    custom_id=f"{CUSTOM_ID_PREFIX}:track:{track.spotify_id}:{self.page}",
                    row=1 + offset,  # only 5 rows; offset 0-4 → row 1-5
                )
                button.callback = self._make_track_callback(track.spotify_id)
                self.add_item(button)

        # Pager in the last row
        total_pages = max(1, (len(tracks) + NUM_TRACKS_PER_PAGE - 1) // NUM_TRACKS_PER_PAGE)
        prev_button = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{CUSTOM_ID_PREFIX}:nav:tracks_prev:{self.page}",
            disabled=self.page <= 0,
            row=4,
        )
        prev_button.callback = self._nav_prev
        self.add_item(prev_button)

        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{CUSTOM_ID_PREFIX}:nav:tracks_next:{self.page}",
            disabled=self.page >= total_pages - 1,
            row=4,
        )
        next_button.callback = self._nav_next
        self.add_item(next_button)

    def _make_track_callback(self, spotify_id: str) -> Callable[[discord.Interaction, discord.ui.Button], Awaitable[None]]:
        async def _cb(interaction: discord.Interaction, button: discord.ui.Button):
            try:
                current = next((t for t in self.editor_view.tracks_for_editor() if t.spotify_id == spotify_id), None)
                if current is None:
                    await interaction.response.send_message("⚠️ Track not found.", ephemeral=True)
                    return
                await self.editor_view._apply_track_toggle(interaction, current, page=self.page)
            except _LockedForPublish:
                await interaction.response.send_message(
                    "⏳ Publishing now; try again in a second.",
                    ephemeral=True,
                )
        return _cb

    async def _nav_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_view = EditorTracksView(self.editor_view, page=max(0, self.page - 1))
        await interaction.response.edit_message(
            embed=self.editor_view.build_tracks_embed(page=new_view.page),
            view=new_view,
        )

    async def _nav_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        tracks = self.editor_view.tracks_for_editor()
        total_pages = max(1, (len(tracks) + NUM_TRACKS_PER_PAGE - 1) // NUM_TRACKS_PER_PAGE)
        new_page = min(total_pages - 1, self.page + 1)
        new_view = EditorTracksView(self.editor_view, page=new_page)
        await interaction.response.edit_message(
            embed=self.editor_view.build_tracks_embed(page=new_page),
            view=new_view,
        )

    async def _back_to_editor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.editor_view.build_editor_embed(),
            view=self.editor_view,
        )


# Main EditorView -----------------------------------------------------------


class EditorView(discord.ui.View):
    """Persistent top-level SCF editor view."""

    # Use the prefix-only custom_ids so Discord re-routes buttons after restart.

    def __init__(
        self,
        sink: EditorSink,
        release_title: str,
        tracks_for_editor: Callable[[], List[Track]],
    ):
        super().__init__(timeout=None)
        self.sink = sink
        self.release_title = release_title
        self._tracks_for_editor = tracks_for_editor
        self.state: EditorState = sink.state  # populated at open time
        # Mark "non-prefilled" custom_ids to satisfy Discord's persistent-view requirement.
        # Each button class below uses self.role-style construction with row attrs.
        self._add_buttons()

    # Discord persistent-view compatibility: every button below is constructed
    # using a row-anchored declaration so the persistent custom_id gives
    # ``on_interaction`` a stable callback target.
    def _add_buttons(self) -> None:
        bool_names = BOOL_FIELDS
        for offset, name in enumerate(bool_names):
            btn = discord.ui.Button(
                label=self._bool_label(name),
                style=discord.ButtonStyle.success if getattr(self.state, name) else discord.ButtonStyle.secondary,
                custom_id=f"{CUSTOM_ID_PREFIX}:bool:{name}",
                row=0,
            )
            btn.callback = self._make_bool_callback(name)
            self.add_item(btn)

        # Row 0 has room for 2 (5 buttons / row limit); use the remaining slots
        # for the "edit tracks" entry point.
        tracks_btn = discord.ui.Button(
            label=self._tracks_label(),
            style=discord.ButtonStyle.primary,
            custom_id=f"{CUSTOM_ID_PREFIX}:open:tracks",
            row=0,
        )
        tracks_btn.callback = self._open_tracks
        self.add_item(tracks_btn)

        # Row 1 — modals for freeform fields
        for offset, name in enumerate(("rating", "notes")):
            btn = discord.ui.Button(
                label=self._scalar_label(name),
                style=discord.ButtonStyle.primary,
                custom_id=f"{CUSTOM_ID_PREFIX}:modal:{name}",
                row=1,
            )
            btn.callback = self._make_modal_callback(name)
            self.add_item(btn)

        # Row 1 also hosts the optional "Body" button which is the same as the
        # existing PostContentModal flow inside the post-publish view. The
        # editor_body button is only present post-publish to avoid two ways to
        # open the same modal in the pre-publish view (pre-publish edits ride
        # onto publish time).
        if self.sink.mode == "post-publish":
            body_btn = discord.ui.Button(
                label="Body: ✏️",
                style=discord.ButtonStyle.primary,
                custom_id=f"{CUSTOM_ID_PREFIX}:modal:body",
                row=1,
            )
            body_btn.callback = self._open_body_modal
            self.add_item(body_btn)

        # Row 3 — navigation row, always present
        if self.sink.mode == "post-publish":
            resync = discord.ui.Button(
                label="Re-sync from WP",
                style=discord.ButtonStyle.secondary,
                custom_id=f"{CUSTOM_ID_PREFIX}:nav:resync",
                row=3,
            )
            resync.callback = self._resync
            self.add_item(resync)

        refresh = discord.ui.Button(
            label="Refresh display",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{CUSTOM_ID_PREFIX}:nav:refresh",
            row=3,
        )
        refresh.callback = self._refresh_display
        self.add_item(refresh)

        done = discord.ui.Button(
            label="Done",
            style=discord.ButtonStyle.danger,
            custom_id=f"{CUSTOM_ID_PREFIX}:nav:done",
            row=3,
        )
        done.callback = self._done
        self.add_item(done)

    # --- Label helpers ----------------------------------------------------

    def _bool_label(self, name: str) -> str:
        on = "✅" if getattr(self.state, name) else "❌"
        return f"{name.title()}: {on}"

    def _scalar_label(self, name: str) -> str:
        if name == "rating":
            return f"Rating: {self.state.rating if self.state.rating is not None else '—'}"
        if name == "notes":
            preview = (self.state.notes or "—").strip().replace("\n", " ")
            if len(preview) > 30:
                preview = preview[:29] + "…"
            return f"Notes: {preview}"
        return name

    def _tracks_label(self) -> str:
        if self.sink.mode == "post-publish" and self.state.music_tracks:
            highlighted = sum(1 for r in self.state.music_tracks if r.get("highlight"))
            return f"Highlight tracks ({highlighted}) →"
        tracks = self._tracks_for_editor()
        highlighted = sum(1 for t in tracks if t.highlight)
        return f"Highlight tracks ({highlighted}) →"

    # --- Discord callback helpers ----------------------------------------

    def _make_bool_callback(self, name: str):
        async def _cb(interaction: discord.Interaction, button: discord.ui.Button):
            try:
                new_value = not getattr(self.state, name)
                await self._apply_field_edit(name, new_value, interaction)
                button.label = self._bool_label(name)
                button.style = discord.ButtonStyle.success if new_value else discord.ButtonStyle.secondary
            except _LockedForPublish:
                await interaction.response.send_message(
                    "⏳ Publishing now; try again in a second.",
                    ephemeral=True,
                )
        return _cb

    def _make_modal_callback(self, name: str):
        async def _cb(interaction: discord.Interaction, button: discord.ui.Button):
            modal = self._build_modal(name)
            await interaction.response.send_modal(modal)
        return _cb

    def _build_modal(self, name: str):
        if name == "rating":
            return RatingModal(self)
        if name == "notes":
            return NotesModal(self)
        if name == "body":
            return self._build_body_modal()
        raise ValueError(f"Unknown modal field: {name}")

    # The post-publish "Body" field reuses the existing PostContentModal shape.
    def _build_body_modal(self) -> "BodyModal":
        return BodyModal(self)

    async def _open_body_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(self._build_body_modal())

    # --- Top-level transitions -------------------------------------------

    async def _open_tracks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.sink.snapshot()  # ensure highlight rows reflect current source
        new_view = EditorTracksView(self, page=0)
        await interaction.response.edit_message(
            embed=self.build_tracks_embed(page=0),
            view=new_view,
        )

    async def _resync(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.sink.mode != "post-publish":
            await interaction.response.send_message(
                "⚠️ Re-sync is only available in post-publish mode.",
                ephemeral=True,
            )
            return
        try:
            await self.sink.snapshot()
            await interaction.response.edit_message(
                embed=self.build_editor_embed(),
                view=self,
            )
        except Exception as error:
            logger.warning("Re-sync from WP failed: %s", error)
            await interaction.response.send_message(
                f"⚠️ Re-sync failed: {error}",
                ephemeral=True,
            )

    async def _refresh_display(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.sink.snapshot()
        self._rebuild_button_labels()
        await interaction.response.edit_message(
            embed=self.build_editor_embed(),
            view=self,
        )

    async def _done(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.message.delete()
        except Exception:
            try:
                await interaction.response.edit_message(
                    content="(editor closed)",
                    embed=None,
                    view=None,
                )
                return
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Single-user authorization is enforced by the parent discord_bot before
        # the view is registered; the guard here is a belt-and-suspenders for
        # interactions after a bot restart.
        if not getattr(self, "_authorized_user_id", None):
            return True
        if interaction.user.id != self._authorized_user_id:
            await interaction.response.send_message(
                "❌ You are not authorized to interact with this editor.",
                ephemeral=True,
            )
            return False
        return True

    # --- Public API used by modals and track buttons ---------------------

    def build_editor_embed(self) -> discord.Embed:
        return build_editor_embed(self.release_title, self.state, mode=self.sink.mode)

    def build_tracks_embed(self, page: int) -> discord.Embed:
        tracks = self.tracks_for_editor()
        total_pages = max(1, (len(tracks) + NUM_TRACKS_PER_PAGE - 1) // NUM_TRACKS_PER_PAGE)
        embed = discord.Embed(
            title="Highlight tracks",
            description=f"Editing: {self.release_title}",
            color=0x3498DB,
        )
        start = page * NUM_TRACKS_PER_PAGE
        page_tracks = tracks[start : start + NUM_TRACKS_PER_PAGE]
        if not page_tracks:
            embed.add_field(name="Tracks", value="No tracks on this page.", inline=False)
        else:
            for track in page_tracks:
                emoji = "⭐" if track.highlight else "—"
                embed.add_field(
                    name=f"{track.track_number}. {track.title}",
                    value=f"{emoji}  ({track.duration_ms // 1000}s)",
                    inline=False,
                )
        embed.set_footer(text=f"Page {page + 1}/{total_pages}")
        return embed

    def tracks_for_editor(self) -> List[Track]:
        """Return the track list the highlight sub-view can toggle."""
        return self._tracks_for_editor()

    async def _apply_field_edit(self, name: str, value: Any, interaction: discord.Interaction):
        await self.sink.update_field(name, value)
        # Re-render in place; bool rows label themselves post-update, scalar
        # rows reflect the new value via the modal default on the next open.
        try:
            await interaction.response.edit_message(
                embed=self.build_editor_embed(),
                view=self,
            )
        except discord.InteractionResponded:
            await interaction.followup.edit_message(
                message_id=interaction.message.id,
                embed=self.build_editor_embed(),
                view=self,
            )

    async def _apply_track_toggle(self, interaction: discord.Interaction, track: Track, page: int):
        new_value = not bool(track.highlight)
        await self.sink.update_track_highlight(track.spotify_id, new_value)
        # Re-fetch release to keep in-memory truth aligned for the next click.
        tracks = self.tracks_for_editor()
        for t in tracks:
            if t.spotify_id == track.spotify_id:
                t.highlight = new_value
                break
        new_view = EditorTracksView(self, page=page)
        await interaction.response.edit_message(
            embed=self.build_tracks_embed(page=page),
            view=new_view,
        )

    def _rebuild_button_labels(self) -> None:
        for child in list(self.children):
            cid = getattr(child, "custom_id", "") or ""
            if cid.startswith(f"{CUSTOM_ID_PREFIX}:bool:"):
                name = cid.split(":")[2]
                child.label = self._bool_label(name)
                child.style = discord.ButtonStyle.success if getattr(self.state, name) else discord.ButtonStyle.secondary
            elif cid.startswith(f"{CUSTOM_ID_PREFIX}:modal:"):
                name = cid.split(":")[2]
                if name in ("rating", "notes"):
                    child.label = self._scalar_label(name)
            elif cid.startswith(f"{CUSTOM_ID_PREFIX}:open:tracks"):
                child.label = self._tracks_label()


# --- Post-publish body modal (reuses PostContentModal shape but routed to
# the live acf-style update) -------------------------------------------------


class BodyModal(discord.ui.Modal):
    """Modal that edits the WP post ``content`` field through the sink."""

    def __init__(self, editor_view: "EditorView"):
        super().__init__(title="Post body", timeout=180)
        self.editor_view = editor_view
        body_input = discord.ui.TextInput(
            label="Post body",
            style=discord.TextStyle.paragraph,
            placeholder="Write the body to add to the WordPress post.",
            default="",
            max_length=4000,
        )
        self.add_item(body_input)
        self.body_input = body_input

    async def on_submit(self, interaction: discord.Interaction):
        # Body is a WP-core field, not SCF. The sink exposes ``update_field``
        # only for SCF fields; for body we route through the same publisher
        # helper as the existing PostContentModal flow.
        publisher = getattr(self.editor_view.sink, "publisher", None)
        post_id = getattr(self.editor_view.sink, "post_id", None)
        if publisher is None or post_id is None:
            await interaction.response.send_message(
                "⚠️ Body updates are only available in post-publish mode.",
                ephemeral=True,
            )
            return
        try:
            await publisher.update_post_content(post_id, str(self.body_input.value))
        except Exception as error:
            logger.error("Body update failed: %s", error)
            await interaction.response.send_message(
                f"❌ Body update failed: {error}",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=self.editor_view.build_editor_embed(),
            view=self.editor_view,
        )


# --- Factory helpers --------------------------------------------------------


class _NullEditorSink:
    """No-op sink used to seed a placeholder EditorView for bot.add_view at startup.

    Persistent static ``custom_id`` s (``editor:bool:favorite`` etc.) are matched
    by ``bot.add_view``; dynamic custom_ids (``editor:track:<id>``) are not
    re-routable after a bot restart, which the plan accepts (see §3c).
    """

    mode = "pre-publish"
    state = EditorState()

    async def snapshot(self):
        return self.state

    async def update_field(self, name, value):  # pragma: no cover - no-op
        return None

    async def update_track_highlight(self, spotify_id, on):  # pragma: no cover
        return None


class _PlaceholderEditorTrackProvider:
    """Returned through ``_PlaceholderEditorTrackProvider().tracks`` for placeholders."""

    def tracks(self) -> List[Track]:
        return []


async def open_pre_publish_editor(
    *,
    db: "Database",
    release: Release,
    on_open: Optional[Callable[["EditorView", discord.Embed], Awaitable[None]]] = None,
) -> "EditorView":
    """Build and return an ``EditorView`` bound to an in-memory ``Release``."""
    sink = PrePublishSink(db=db, release=release)
    await sink.snapshot()

    def tracks_provider() -> List[Track]:
        return list(release.tracks)

    view = EditorView(
        sink=sink,
        release_title=release.title,
        tracks_for_editor=tracks_provider,
    )
    if on_open is not None:
        await on_open(view, view.build_editor_embed())
    return view


async def open_post_publish_editor(
    *,
    publisher: "Publisher",
    wordpress_client,
    post_id: int,
    release_title: str,
    initial_acf: Optional[Dict[str, Any]] = None,
    on_open: Optional[Callable[["EditorView", discord.Embed], Awaitable[None]]] = None,
) -> "EditorView":
    """Build an editor bound to a live WordPress post."""
    sink = PostPublishSink(
        publisher=publisher,
        wordpress_client=wordpress_client,
        post_id=post_id,
        initial_acf=initial_acf,
    )
    await sink.snapshot()

    def tracks_provider() -> List[Track]:
        # For post-publish, the authoritative highlight state is the acf block,
        # but the Discord view shows friendly Track-shaped labels when present.
        return _tracks_from_acf(sink.state.music_tracks)

    view = EditorView(
        sink=sink,
        release_title=release_title,
        tracks_for_editor=tracks_provider,
    )
    if on_open is not None:
        await on_open(view, view.build_editor_embed())
    return view


def _tracks_from_acf(music_tracks: Optional[List[Dict[str, Any]]]) -> List[Track]:
    """Build ephemeral ``Track`` objects for the highlight sub-view from acf."""
    if not music_tracks:
        return []
    out: List[Track] = []
    for row in music_tracks:
        out.append(
            Track(
                spotify_id=str(row.get("spotify_id") or row.get("id") or ""),
                title=str(row.get("title") or "Unknown"),
                normalized_title="",
                duration_ms=int(row.get("duration_ms") or 0),
                disc_number=int(row.get("disc_number") or 1),
                track_number=int(row.get("track_number") or 1),
                is_countable=True,
                listened=False,
                highlight=bool(row.get("highlight")),
            )
        )
    return out
