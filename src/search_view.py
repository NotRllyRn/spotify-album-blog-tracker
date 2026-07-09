"""
Search picker UI. Pure rendering on top of ``src/search.py``.

``format_picker_embed`` and ``SearchPickerView`` produce the picker
embed + View from a ranked ``SearchMatch`` list. The Discord-side
wiring lives in ``src/discord_bot.py``; this file is intentionally
View-only and has no Discord business logic.
"""

import uuid
from dataclasses import dataclass
from typing import List, Protocol

import discord

from search import FUZZY_BASE_THRESHOLD, FUZZY_LOOSE_THRESHOLD, RESULT_CAP, SearchMatch


# --- Constants --------------------------------------------------------------

CUSTOM_ID_PREFIX = "search"
EMBED_PREVIEW_ROWS = 3
SELECT_LABEL_LIMIT = 100
SELECT_DESC_LIMIT = 100
VIEW_TIMEOUT_SECONDS = 900  # 15 minutes


# --- Public data shapes -----------------------------------------------------


@dataclass(frozen=True)
class PickerRequest:
    """Carries enough state to resubmit a /search from a picker button."""

    query: str
    threshold: float
    source: str  # "cache" | "live"
    nonce: str = ""  # unique-per-picker; isolates running /search invocations.


class SearchDispatcher(Protocol):
    """Methods SearchPickerView leans on; implemented by DiscordBot."""

    async def render_picker(self, query: str, threshold: float, *, force_source: str | None = None) -> "PickerRender": ...
    async def open_editor_for_post(self, interaction: discord.Interaction, post_id: int, request: PickerRequest) -> None: ...


@dataclass(frozen=True)
class PickerRender:
    embed: discord.Embed
    view: "SearchPickerView"


# --- Helpers ----------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else (text[: limit - 1] + "…")


def _threshold_label(threshold: float) -> str:
    if abs(threshold - FUZZY_LOOSE_THRESHOLD) < 1e-9:
        return "0.30 (loose)"
    if abs(threshold - FUZZY_BASE_THRESHOLD) < 1e-9:
        return "0.55 (base)"
    return f"{threshold:.2f}"


def _new_nonce() -> str:
    return uuid.uuid4().hex[:8]


# --- Embed ------------------------------------------------------------------


def format_picker_embed(
    query: str,
    matches: List[SearchMatch],
    *,
    source: str,
    fell_back_to_live: bool,
    threshold: float,
) -> discord.Embed:
    """Build the picker embed. Pure function; no DB or HTTP."""
    embed = discord.Embed(title=f"Search: {query or '(empty)'}")

    body_parts: List[str] = []
    if fell_back_to_live:
        body_parts.append("⚠️ Cache empty/stale — searched live WordPress.")
    if matches:
        rows = "\n".join(
            f"{i}. **{m.title}** #{m.post_id} · {m.link}"
            for i, m in enumerate(matches[:EMBED_PREVIEW_ROWS], 1)
        )
        body_parts.append(rows)
    else:
        body_parts.append(
            "No matches above threshold. Try Match loosely or Search live."
        )
    embed.description = "\n\n".join(body_parts)

    cap = min(len(matches), RESULT_CAP)
    embed.set_footer(
        text=f"Top {cap} of {len(matches)} above threshold {_threshold_label(threshold)}."
    )
    return embed


# --- View -------------------------------------------------------------------


class SearchPickerView(discord.ui.View):
    """Discord View with ``StringSelect`` of post IDs and three rerun buttons."""

    def __init__(
        self,
        *,
        dispatcher: SearchDispatcher,
        request: PickerRequest,
        matches: List[SearchMatch],
    ):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.dispatcher = dispatcher
        self.request = request
        self.matches = matches

        # Per-picker nonce tags every custom_id so two /search invocations in
        # the same channel can't collide on the rerun routes.
        nonce = request.nonce or _new_nonce()
        cid = lambda name: f"{CUSTOM_ID_PREFIX}:{name}:{nonce}"  # noqa: E731

        if matches:
            options = [
                discord.SelectOption(
                    label=_clip(m.title, SELECT_LABEL_LIMIT),
                    value=str(m.post_id),
                    description=_clip(
                        ", ".join(m.artists) if m.artists else m.link,
                        SELECT_DESC_LIMIT,
                    ),
                )
                for m in matches[:RESULT_CAP]
            ]
            select = discord.ui.Select(
                placeholder="Pick a WordPress post to edit",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=cid("select"),
                row=0,
            )

            async def on_select(interaction: discord.Interaction) -> None:
                await interaction.response.defer(ephemeral=True)
                try:
                    post_id = int(select.values[0])
                except (IndexError, ValueError, TypeError):
                    return
                await dispatcher.open_editor_for_post(interaction, post_id, request)

            select.callback = on_select
            self.add_item(select)

        # Footer buttons.
        self.add_item(self._make_button("Search again", row=1, on_click=self._rerun_cache, nonce=nonce))
        self.add_item(self._make_button("Match loosely", row=1, on_click=self._rerun_loose, nonce=nonce))
        self.add_item(self._make_button("Search live", row=1, on_click=self._rerun_live, nonce=nonce))
        # Aesthetic Done button: deletes the picker message.
        self.add_item(self._make_button("Done", row=1, on_click=self._done, nonce=nonce))

    def _make_button(self, label: str, *, row: int, on_click, nonce: str) -> discord.ui.Button:
        key = label.lower().replace(" ", "_")
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary if label != "Done" else discord.ButtonStyle.danger,
            custom_id=f"{CUSTOM_ID_PREFIX}:{key}:{nonce}",
            row=row,
        )
        btn.callback = on_click
        return btn

    async def _rerun(self, interaction: discord.Interaction, *, threshold: float, force_live: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        render = await self.dispatcher.render_picker(
            self.request.query,
            threshold,
            force_source="live" if force_live else None,
        )
        await interaction.edit_original_response(embed=render.embed, view=render.view)

    async def _rerun_cache(self, interaction: discord.Interaction) -> None:
        await self._rerun(interaction, threshold=FUZZY_BASE_THRESHOLD, force_live=False)

    async def _rerun_loose(self, interaction: discord.Interaction) -> None:
        await self._rerun(interaction, threshold=FUZZY_LOOSE_THRESHOLD, force_live=False)

    async def _rerun_live(self, interaction: discord.Interaction) -> None:
        await self._rerun(interaction, threshold=self.request.threshold, force_live=True)

    async def _done(self, interaction: discord.Interaction) -> None:
        """Delete the picker message; the DM-side editor is unaffected."""
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
