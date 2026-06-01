"""
Discord bot for control plane.
"""

import asyncio
import discord
from discord import app_commands
import logging
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from config import Config
from database import Database
from tracker import Tracker
from models import Release, PromptType, PromptState, DiscordPrompt, LifecycleStatus, PlaybackState, WordPressPost, SavedLibraryAlbum
from inprogress import INPROGRESS_PAGE_SIZE, InProgressPage, build_inprogress_page, get_next_unlistened_track

logger = logging.getLogger(__name__)

ACTIVE_TRACKED_STATUSES = {
    LifecycleStatus.ACTIVE,
    LifecycleStatus.AWAITING_75_DECISION,
    LifecycleStatus.PUBLISHING,
}


@dataclass(frozen=True)
class CurrentPostContext:
    tracked_release: Optional[Release]
    release_for_post: Optional[Release]
    duplicate_post: Optional[WordPressPost]
    is_actively_tracked: bool

    @property
    def will_publish_as_relisten(self) -> bool:
        return (
            self.duplicate_post is not None
            or (self.tracked_release is not None and self.tracked_release.is_relisten)
        )

def _clip_discord_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:max(0, limit - 1)] + "…"

class PromptView(discord.ui.View):
    def __init__(self, discord_bot: "DiscordBot"):
        super().__init__(timeout=None)
        self.discord_bot = discord_bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.discord_bot._check_authorized(interaction.user.id):
            await interaction.response.send_message(
                "❌ You are not authorized to interact with this prompt.",
                ephemeral=True
            )
            return False
        return True

class SeventyFivePromptView(PromptView):
    @discord.ui.button(
        label="Publish now",
        style=discord.ButtonStyle.success,
        custom_id="prompt_75_publish_now"
    )
    async def publish_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "publish_now")

    @discord.ui.button(
        label="Wait for full completion",
        style=discord.ButtonStyle.secondary,
        custom_id="prompt_75_wait"
    )
    async def wait_for_completion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "wait")

class RelistenApprovalPromptView(PromptView):
    @discord.ui.button(
        label="Yes, track as relisten",
        style=discord.ButtonStyle.success,
        custom_id="prompt_relisten_approve_tracking"
    )
    async def approve_tracking(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "approve_relisten_tracking")

class PostContentModal(discord.ui.Modal):
    def __init__(
        self,
        discord_bot: "DiscordBot",
        discord_message_id: str,
        release_id: Optional[str],
        wordpress_post_id: Optional[int],
    ):
        super().__init__(title="Add post content", timeout=300)
        self.discord_bot = discord_bot
        self.discord_message_id = discord_message_id
        self.release_id = release_id
        self.wordpress_post_id = wordpress_post_id
        self.body_input = discord.ui.TextInput(
            label="Post content",
            style=discord.TextStyle.paragraph,
            placeholder="Write the content to add to the WordPress post.",
            required=True,
            max_length=4000,
            custom_id="post_content_body",
        )
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.discord_bot._handle_post_content_submit(
            interaction=interaction,
            discord_message_id=self.discord_message_id,
            release_id=self.release_id,
            fallback_wordpress_post_id=self.wordpress_post_id,
            raw_content=str(self.body_input.value),
        )


class PublishedPostActionView(PromptView):
    @discord.ui.button(
        label="Add content",
        style=discord.ButtonStyle.primary,
        custom_id="prompt_add_content"
    )
    async def add_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "add_content")

    @discord.ui.button(
        label="Undo post",
        style=discord.ButtonStyle.danger,
        custom_id="prompt_undo_post"
    )
    async def undo_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "undo_post")

    @discord.ui.button(
        label="Keep post",
        style=discord.ButtonStyle.secondary,
        custom_id="prompt_keep_post"
    )
    async def keep_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "keep_post")


UndoPromptView = PublishedPostActionView


class InProgressView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", page_data: InProgressPage):
        super().__init__(discord_bot)
        self.page = page_data.page
        self.previous_page.disabled = page_data.page <= 0
        self.next_page.disabled = page_data.page >= page_data.total_pages - 1

        options = [self._build_option(page_data.featured, featured=True)]
        options.extend(self._build_option(release) for release in page_data.items)

        select = discord.ui.Select(
            placeholder="Select a release to manage",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="inprogress_select",
            row=0
        )

        async def select_callback(interaction: discord.Interaction):
            if select.values:
                await self.discord_bot._handle_inprogress_selection(interaction, select.values[0], self.page)
            else:
                await interaction.response.send_message(
                    "⚠️ No release selected.",
                    ephemeral=True
                )

        select.callback = select_callback
        self.add_item(select)

    def _build_option(self, release: Release, featured: bool = False) -> discord.SelectOption:
        artist_names = ", ".join([artist.name for artist in release.artists][:2]) or "Unknown"
        progress_percent = int(release.progress * 100)
        label_prefix = "Featured: " if featured else ""
        label = _clip_discord_text(f"{label_prefix}{release.title}", 100)
        description = _clip_discord_text(
            f"{release.release_type.value}, {progress_percent}%, {artist_names}",
            100
        )
        return discord.SelectOption(label=label, value=release.spotify_id, description=description)

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        custom_id="inprogress_previous_page",
        row=1
    )
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_inprogress_page(interaction, self.page - 1)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="inprogress_refresh_page",
        row=1
    )
    async def refresh_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_inprogress_page(interaction, self.page)

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary,
        custom_id="inprogress_next_page",
        row=1
    )
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_inprogress_page(interaction, self.page + 1)

class ReleaseActionView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", release_id: str, return_page: int = 0):
        super().__init__(discord_bot)
        self.release_id = release_id
        self.return_page = return_page

    @discord.ui.button(
        label="Publish early",
        style=discord.ButtonStyle.success,
        custom_id="release_publish_early"
    )
    async def publish_early(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_publish_release(interaction, self.release_id)

    @discord.ui.button(
        label="Remove from database",
        style=discord.ButtonStyle.danger,
        custom_id="release_remove_database"
    )
    async def remove_from_database(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_remove_release_prompt(interaction, self.release_id)

    @discord.ui.button(
        label="Show missing songs",
        style=discord.ButtonStyle.secondary,
        custom_id="release_show_missing_songs"
    )
    async def show_missing_songs(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_missing_songs(interaction, self.release_id)

    @discord.ui.button(
        label="Back",
        style=discord.ButtonStyle.secondary,
        custom_id="release_back_to_inprogress"
    )
    async def back_to_inprogress(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_inprogress_page(interaction, self.return_page)

class ConfirmRemoveView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", release_id: str):
        super().__init__(discord_bot)
        self.release_id = release_id

    @discord.ui.button(
        label="Confirm remove",
        style=discord.ButtonStyle.danger,
        custom_id="confirm_remove_release"
    )
    async def confirm_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_confirm_remove_release(interaction, self.release_id)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="cancel_remove_release"
    )
    async def cancel_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "✅ Removal canceled.",
            ephemeral=True
        )

class CurrentPlaybackActionView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", playback_state, post_label: str = "Post current content"):
        super().__init__(discord_bot)
        self.playback_state = playback_state
        for child in self.children:
            if getattr(child, "custom_id", None) == "current_post_content":
                child.label = post_label

    @discord.ui.button(
        label="Post current content",
        style=discord.ButtonStyle.success,
        custom_id="current_post_content"
    )
    async def post_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_current_post_request(interaction, self.playback_state)

class ConfirmCurrentPostView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", playback_state):
        super().__init__(discord_bot)
        self.playback_state = playback_state

    @discord.ui.button(
        label="Confirm post",
        style=discord.ButtonStyle.success,
        custom_id="confirm_current_post"
    )
    async def confirm_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_current_post_confirm(interaction, self.playback_state)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="cancel_current_post"
    )
    async def cancel_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "✅ Post canceled.",
            ephemeral=True
        )


class RandomAlbumView(PromptView):
    @discord.ui.button(
        label="Re-roll",
        style=discord.ButtonStyle.secondary,
        custom_id="random_album_reroll"
    )
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot._handle_random_reroll(interaction)


class DiscordBot:
    def __init__(self, config: Config, db: Database, tracker: Tracker):
        self.config = config
        self.db = db
        self.tracker = tracker
        self.ready_event = asyncio.Event()

        intents = discord.Intents.default()
        # intents.message_content = True  # Not needed for slash commands
        self.bot = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.bot)

        self._register_views()
        self._setup_commands()

    def _setup_commands(self):
        """Setup slash commands."""

        @self.tree.command(name="inprogress", description="View active release lifecycles")
        async def inprogress(interaction: discord.Interaction):
            await self._handle_inprogress(interaction)

        @self.tree.command(name="current", description="Show current listening target")
        async def current(interaction: discord.Interaction):
            await self._handle_current(interaction)

        @self.tree.command(name="service", description="Service health and status")
        async def service(interaction: discord.Interaction):
            await self._handle_service(interaction)

        @self.tree.command(name="random", description="Pick a random unposted saved-library album")
        async def random(interaction: discord.Interaction):
            await self._handle_random(interaction)

        @self.bot.event
        async def on_ready():
            await self.tree.sync()
            self.ready_event.set()
            logger.info(f"Discord bot logged in as {self.bot.user}")

    def _register_views(self):
        self.bot.add_view(SeventyFivePromptView(self))
        self.bot.add_view(RelistenApprovalPromptView(self))
        self.bot.add_view(PublishedPostActionView(self))
        self.bot.add_view(RandomAlbumView(self))

    async def start(self):
        """Start the Discord bot."""
        await self.bot.start(self.config.discord_bot_token)

    async def wait_until_ready(self):
        await self.ready_event.wait()

    async def stop(self):
        """Stop the Discord bot."""
        await self.bot.close()

    def _check_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        return user_id == self.config.discord_user_id

    async def _get_user(self) -> Optional[discord.User]:
        user = self.bot.get_user(self.config.discord_user_id)
        if user is None:
            user = await self.bot.fetch_user(self.config.discord_user_id)
        return user

    async def _send_dm(self, content: str, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None) -> Optional[discord.Message]:
        try:
            user = await self._get_user()
            if user is None:
                raise ValueError("Discord user not found")
            return await user.send(content=content, embed=embed, view=view)
        except Exception as e:
            logger.error(f"Unable to send Discord DM: {e}")
            return None

    def _get_public_wordpress_link(self, raw_link: Optional[str]) -> Optional[str]:
        if not raw_link:
            return None

        try:
            parsed_link = urlparse(raw_link)
            public_base = urlparse(self.config.wordpress_public_url.rstrip("/"))

            if not parsed_link.path:
                return raw_link

            return urlunparse((public_base.scheme, public_base.netloc, parsed_link.path, parsed_link.params, parsed_link.query, parsed_link.fragment))
        except Exception:
            return raw_link

    async def update_presence(self, state) -> None:
        """Update the bot presence to reflect current listening state."""
        if not self.bot.is_ready():
            return

        status = discord.Status.do_not_disturb
        activity = None

        if state is not None and state.item:
            item = state.item
            track_name = item.get("name", "Unknown")
            artists = [a.get("name") for a in item.get("artists", []) if a.get("name")]
            artist_text = ", ".join(artists[:2]) if artists else "Unknown"
            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{track_name} by {artist_text}"
            )
            status = discord.Status.online if state.is_playing else discord.Status.idle

        try:
            await self.bot.change_presence(status=status, activity=activity)
        except Exception:
            pass

    async def send_75_percent_prompt(self, release: Release) -> Optional[discord.Message]:
        """Send a 75% completion prompt to the authorized user."""
        embed = discord.Embed(
            title="Release reached 75% progress",
            description=f"{release.title} by {', '.join([a.name for a in release.artists])}",
            color=0x1DB954
        )
        embed.add_field(name="Progress", value=f"{int(release.progress * 100)}%", inline=True)
        embed.add_field(name="Release type", value=release.release_type.value, inline=True)
        embed.set_thumbnail(url=release.cover_url)

        view = SeventyFivePromptView(self)
        return await self._send_dm(
            "A tracked release reached 75% progress. Would you like to publish it early or wait for full completion?",
            embed=embed,
            view=view
        )

    async def send_relisten_tracking_prompt(
        self,
        release: Release,
        duplicate_post: WordPressPost,
        expires_at: datetime,
    ) -> Optional[discord.Message]:
        """Ask whether a duplicate release should start tracking as a relisten."""
        embed = discord.Embed(
            title="Track duplicate as relisten?",
            description=f"{release.title} by {', '.join([a.name for a in release.artists])}",
            color=0xE67E22
        )
        embed.add_field(
            name="Existing WordPress post",
            value=f"{_clip_discord_text(duplicate_post.title, 160)} (post {duplicate_post.id})",
            inline=False
        )
        embed.add_field(
            name="Expires",
            value=expires_at.strftime("%Y-%m-%d %H:%M"),
            inline=False
        )
        embed.set_thumbnail(url=release.cover_url)

        view = RelistenApprovalPromptView(self)
        return await self._send_dm(
            (
                "This album already exists on WordPress. Approve it to start tracking as a Relisten. "
                "If you do nothing, it will stay untracked and can prompt again after this expires."
            ),
            embed=embed,
            view=view
        )

    async def send_publish_notification(self, release: Release, post: dict) -> Optional[discord.Message]:
        """Send notification after a release is published."""
        embed = discord.Embed(
            title=f"Release {'republished' if release.is_relisten else 'published'} to WordPress",
            description=f"{release.title} by {', '.join([a.name for a in release.artists])}",
            color=0x1DB954
        )
        embed.add_field(name="Post ID", value=str(post["id"]), inline=True)
        wordpress_link = self._get_public_wordpress_link(post.get("link") or post.get("guid", ""))
        embed.add_field(name="WordPress link", value=wordpress_link or "Unavailable", inline=False)
        embed.add_field(name="Release type", value=release.release_type.value, inline=True)
        embed.add_field(name="Progress", value=f"{int(release.progress * 100)}%", inline=True)
        embed.set_thumbnail(url=release.cover_url)

        if release.is_relisten and release.duplicate_post_id:
            embed.add_field(name="Relisten", value=f"Original post ID: {release.duplicate_post_id}", inline=False)

        view = None
        if release.wordpress_post_id:
            view = PublishedPostActionView(self)

        message = await self._send_dm(
            "The release has been published to WordPress.",
            embed=embed,
            view=view
        )

        if message and view is not None:
            prompt = DiscordPrompt(
                id=0,
                prompt_type=PromptType.PROMPT_UNDO.value,
                release_id=release.spotify_id,
                wordpress_post_id=release.wordpress_post_id,
                discord_message_id=str(message.id),
                state=PromptState.PENDING.value
            )
            await self.db.save_discord_prompt(prompt)

        return message

    async def send_library_missing_notification(self, release: Release) -> Optional[discord.Message]:
        """Warn that a published album is not saved in the user's Spotify library."""
        embed = discord.Embed(
            title="Published album is not saved in Spotify",
            description=f"{release.title} by {', '.join([a.name for a in release.artists])}",
            color=0xE67E22
        )
        embed.add_field(name="Spotify ID", value=release.spotify_id, inline=False)
        embed.add_field(name="Action", value="Save it in Spotify if it belongs on the listen-to list.", inline=False)
        if release.cover_url:
            embed.set_thumbnail(url=release.cover_url)

        return await self._send_dm(
            "A published album was not found in your saved Spotify library.",
            embed=embed
        )

    async def handle_prompt_action(self, interaction: discord.Interaction, action: str):
        if action != "add_content":
            await interaction.response.defer(ephemeral=True)

        prompt = await self.db.get_discord_prompt(str(interaction.message.id))
        if prompt is None:
            await self._send_prompt_action_response(
                interaction,
                "⚠️ This prompt is no longer available.",
            )
            return

        if prompt.state != PromptState.PENDING.value:
            await self._send_prompt_action_response(
                interaction,
                "⚠️ This prompt has already been handled.",
            )
            return

        if prompt.expires_at and prompt.expires_at <= datetime.now():
            await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.EXPIRED.value)
            await self._send_prompt_action_response(
                interaction,
                "⚠️ This prompt has expired and is no longer available.",
            )
            return

        release = await self.db.get_release(prompt.release_id) if prompt.release_id else None
        can_handle_without_release = (
            (
                prompt.prompt_type == PromptType.PROMPT_UNDO.value
                and action == "add_content"
            )
            or (
                prompt.prompt_type == PromptType.PROMPT_RELISTEN_APPROVAL.value
                and action == "approve_relisten_tracking"
            )
        )
        if release is None and not can_handle_without_release:
            await self._send_prompt_action_response(
                interaction,
                "⚠️ Unable to find the release associated with this prompt.",
            )
            return

        try:
            if prompt.prompt_type == PromptType.PROMPT_75_PERCENT.value:
                if action == "publish_now":
                    await self._handle_75_publish(interaction, release, prompt)
                elif action == "wait":
                    await self._handle_75_wait(interaction, release, prompt)
                else:
                    await self._unknown_prompt_action(interaction)
            elif prompt.prompt_type == PromptType.PROMPT_RELISTEN_APPROVAL.value:
                if action == "approve_relisten_tracking":
                    await self._handle_relisten_tracking_approval(interaction, prompt)
                else:
                    await self._unknown_prompt_action(interaction)
            elif prompt.prompt_type == PromptType.PROMPT_UNDO.value:
                if action == "add_content":
                    await self._handle_add_content(interaction, release, prompt)
                elif action == "undo_post":
                    await self._handle_undo_post(interaction, release, prompt)
                elif action == "keep_post":
                    await self._handle_keep_post(interaction, release, prompt)
                else:
                    await self._unknown_prompt_action(interaction)
            else:
                await self._unknown_prompt_action(interaction)
        except Exception as e:
            logger.error(f"Error handling prompt action {action}: {e}")
            await self._send_prompt_action_response(
                interaction,
                f"❌ Error handling prompt action: {str(e)[:100]}",
            )

    async def _send_prompt_action_response(self, interaction: discord.Interaction, content: str):
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _unknown_prompt_action(self, interaction: discord.Interaction):
        await self._send_prompt_action_response(
            interaction,
            "⚠️ Unknown action for this prompt.",
        )

    def _resolve_wordpress_post_id(
        self,
        release: Optional[Release],
        prompt: Optional[DiscordPrompt],
        fallback_wordpress_post_id: Optional[int] = None,
    ) -> Optional[int]:
        if prompt and prompt.wordpress_post_id:
            return prompt.wordpress_post_id
        if release and release.wordpress_post_id:
            return release.wordpress_post_id
        return fallback_wordpress_post_id

    async def _handle_75_publish(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.ACCEPTED.value)
        await self._publish_release_with_feedback(
            interaction,
            release,
            success_message="✅ Published early. A notification has been sent.",
            already_published_message=(
                f"✅ This release is already published as post {release.wordpress_post_id}."
                if release.wordpress_post_id
                else "✅ This release is already published."
            ),
            already_publishing_message="⏳ This release is already being published."
        )

    async def _handle_75_wait(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.DECLINED.value)
        release.status = LifecycleStatus.ACTIVE
        await self.db.save_release(release)
        await interaction.followup.send(
            "✅ Okay — continuing to track the release until it completes.",
            ephemeral=True
        )

    async def _handle_relisten_tracking_approval(self, interaction: discord.Interaction, prompt: DiscordPrompt):
        outcome = await self.tracker.approve_relisten_tracking(prompt)
        if outcome == "tracking_started":
            await interaction.followup.send(
                "✅ Tracking started as a Relisten. It will publish automatically when complete.",
                ephemeral=True
            )
        elif outcome == "expired":
            await interaction.followup.send(
                "⚠️ This prompt has expired. Listen again later to receive a fresh approval prompt.",
                ephemeral=True
            )
        elif outcome == "not_trackable":
            await interaction.followup.send(
                "⚠️ This release is no longer trackable as an album.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "⚠️ This prompt is no longer available.",
                ephemeral=True
            )

    async def _handle_add_content(
        self,
        interaction: discord.Interaction,
        release: Optional[Release],
        prompt: DiscordPrompt,
    ):
        post_id = self._resolve_wordpress_post_id(release, prompt)
        if not post_id:
            await self._send_prompt_action_response(
                interaction,
                "⚠️ Unable to add content because the WordPress post ID is missing.",
            )
            return

        await interaction.response.send_modal(
            PostContentModal(
                discord_bot=self,
                discord_message_id=prompt.discord_message_id,
                release_id=prompt.release_id,
                wordpress_post_id=post_id,
            )
        )

    async def _handle_post_content_submit(
        self,
        interaction: discord.Interaction,
        discord_message_id: str,
        release_id: Optional[str],
        fallback_wordpress_post_id: Optional[int],
        raw_content: str,
    ):
        await interaction.response.defer(ephemeral=True)

        prompt = await self.db.get_discord_prompt(discord_message_id)
        if prompt is None:
            await interaction.followup.send(
                "⚠️ This prompt is no longer available.",
                ephemeral=True
            )
            return

        if prompt.state != PromptState.PENDING.value:
            await interaction.followup.send(
                "⚠️ This prompt has already been handled.",
                ephemeral=True
            )
            return

        release_lookup_id = prompt.release_id or release_id
        release = await self.db.get_release(release_lookup_id) if release_lookup_id else None
        post_id = self._resolve_wordpress_post_id(
            release,
            prompt,
            fallback_wordpress_post_id=fallback_wordpress_post_id,
        )
        if not post_id:
            await interaction.followup.send(
                "⚠️ Unable to add content because the WordPress post ID is missing.",
                ephemeral=True
            )
            return

        try:
            await self.tracker.publisher.update_post_content(post_id, raw_content)
        except Exception as e:
            logger.error(f"Error updating post content for post {post_id}: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error updating WordPress post: {str(e)[:100]}",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            "✅ WordPress post content has been updated.",
            ephemeral=True
        )

    async def _handle_undo_post(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.ACCEPTED.value)
        post_id = self._resolve_wordpress_post_id(release, prompt)
        if not post_id:
            await interaction.followup.send(
                "⚠️ Unable to undo because the WordPress post ID is missing.",
                ephemeral=True
            )
            return

        success = await self.tracker.publisher.trash_post(post_id)
        if success:
            await self.db.mark_saved_library_album_unposted(release.spotify_id)
            await self.db.delete_release(release.spotify_id)
            await interaction.followup.send(
                "✅ The post has been moved to trash and removed from the tracking database.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ Could not move the post to trash.",
                ephemeral=True
            )

    async def _handle_keep_post(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.DECLINED.value)
        await interaction.followup.send(
            "✅ The WordPress post will be kept.",
            ephemeral=True
        )

    async def _handle_inprogress_selection(self, interaction: discord.Interaction, release_id: str, return_page: int = 0):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        embed = self._build_release_summary_embed(release, title="Manage Release")
        await interaction.response.edit_message(embed=embed, view=ReleaseActionView(self, release_id, return_page))

    async def _handle_publish_release(self, interaction: discord.Interaction, release_id: str):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        await self._publish_release_with_feedback(
            interaction,
            release,
            success_message="✅ Release published successfully. A notification has been sent.",
            already_published_message=(
                f"✅ This release is already published as post {release.wordpress_post_id}."
                if release.wordpress_post_id
                else "✅ This release is already published."
            ),
            already_publishing_message="⏳ This release is already being published.",
            as_relisten=release.is_relisten
        )

    async def _handle_remove_release_prompt(self, interaction: discord.Interaction, release_id: str):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Confirm removal",
            description=(
                f"Are you sure you want to remove '{release.title}' from the tracking database? "
                "This will delete its progress and history."
            ),
            color=0xE74C3C
        )
        embed.add_field(name="Release", value=release.title, inline=False)
        embed.add_field(name="Artists", value=", ".join([a.name for a in release.artists][:5]) or "Unknown", inline=False)
        embed.add_field(name="Progress", value=f"{int(release.progress * 100)}%", inline=True)

        await interaction.response.send_message(
            embed=embed,
            view=ConfirmRemoveView(self, release_id),
            ephemeral=True
        )

    async def _handle_missing_songs(self, interaction: discord.Interaction, release_id: str):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        missing_tracks = [t.title for t in release.tracks if t.is_countable and not t.listened]
        if not missing_tracks:
            description = "No missing countable tracks. All tracked songs have been marked listened."
        else:
            visible = missing_tracks[:10]
            description = "\n".join(f"• {title}" for title in visible)
            if len(missing_tracks) > len(visible):
                description += f"\n…and {len(missing_tracks) - len(visible)} more missing track(s)."

        embed = discord.Embed(
            title=f"Missing songs for {release.title}",
            description=description,
            color=0xF1C40F
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _handle_confirm_remove_release(self, interaction: discord.Interaction, release_id: str):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        deleted = await self.db.delete_release(release_id)
        if not deleted:
            await interaction.response.send_message(
                "⚠️ Could not remove the release.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Removed '{release.title}' from the tracking database.",
            ephemeral=True
        )

    def _build_release_summary_embed(self, release: Release, title: str = "Release Manager") -> discord.Embed:
        artist_names = ", ".join([a.name for a in release.artists][:5]) or "Unknown"
        listened, countable, progress_percent = self._get_release_progress_parts(release)

        embed = discord.Embed(
            title=title,
            description=f"{release.title} by {artist_names}",
            color=0x1DB954
        )
        if release.is_relisten:
            duplicate_label = f"Original post ID: {release.duplicate_post_id}" if release.duplicate_post_id else "Approved relisten"
            embed.add_field(name="Relisten", value=duplicate_label, inline=False)
        embed.add_field(name="Release type", value=release.release_type.value, inline=True)
        embed.add_field(name="Progress", value=f"{listened}/{countable} ({progress_percent}%)", inline=True)
        embed.add_field(name="Status", value=release.status.value, inline=True)
        embed.add_field(name="Spotify ID", value=release.spotify_id, inline=False)
        embed.set_thumbnail(url=release.cover_url)
        return embed

    async def _handle_current_post_request(self, interaction: discord.Interaction, playback_state):
        if not playback_state or not playback_state.item:
            await interaction.response.send_message(
                "⚠️ Unable to preview the current track.",
                ephemeral=True
            )
            return

        try:
            context = await self._resolve_current_post_context(playback_state, check_duplicate=True)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Unable to determine the current album.",
                ephemeral=True
            )
            return

        embed = self._build_current_preview_embed(playback_state, context)
        view = ConfirmCurrentPostView(self, playback_state)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _build_current_preview_embed(self, state, context: CurrentPostContext) -> discord.Embed:
        item = state.item
        album = item.get("album", {})
        artists = [a["name"] for a in item.get("artists", [])]
        if context.is_actively_tracked:
            title = "Post current playback early"
            description = "This would publish the current listening target early to WordPress."
        else:
            title = "Post current playback"
            description = "This would publish the current listening target to WordPress."

        embed = discord.Embed(
            title=title,
            description=description,
            color=0x1DB954
        )
        embed.add_field(name="Track", value=item.get("name", "Unknown"), inline=False)
        embed.add_field(name="Album", value=album.get("name", "Unknown"), inline=False)
        embed.add_field(name="Artists", value=", ".join(artists[:5]) or "Unknown", inline=False)
        embed.add_field(name="Status", value="▶ Playing" if state.is_playing else "⏸ Paused", inline=True)
        embed.add_field(name="Shuffle", value="On" if state.shuffle_state else "Off", inline=True)
        embed.add_field(name="Album type", value=album.get("album_type", "Unknown"), inline=True)
        if context.duplicate_post is not None:
            duplicate_title = _clip_discord_text(context.duplicate_post.title, 120)
            embed.add_field(
                name="Relisten ⚠️",
                value=(
                    "Existing WordPress post found: "
                    f"{duplicate_title} (post {context.duplicate_post.id}). "
                    "Confirming will publish this as a Relisten."
                ),
                inline=False
            )
        if album.get("images"):
            embed.set_thumbnail(url=album.get("images")[0].get("url", ""))
        return embed

    def _build_current_embed(self, state, context: Optional[CurrentPostContext] = None) -> discord.Embed:
        item = state.item
        album = item.get("album", {})
        artists = [a["name"] for a in item.get("artists", [])]

        embed = discord.Embed(
            title="Current Playback",
            color=0x1DB954
        )
        if album.get("images"):
            embed.set_thumbnail(url=album.get("images")[0].get("url", ""))

        embed.add_field(
            name="Track",
            value=item.get("name", "Unknown"),
            inline=False
        )
        embed.add_field(
            name="Album",
            value=album.get("name", "Unknown"),
            inline=False
        )
        embed.add_field(
            name="Artists",
            value=", ".join(artists[:5]) or "Unknown",
            inline=False
        )
        embed.add_field(
            name="Status",
            value="▶ Playing" if state.is_playing else "⏸ Paused",
            inline=True
        )
        embed.add_field(
            name="Shuffle",
            value="On" if state.shuffle_state else "Off",
            inline=True
        )

        qualifies = self.tracker._qualifies_for_tracking(state)
        embed.add_field(
            name="Counts for Tracking",
            value="✅ Yes" if qualifies else "❌ No",
            inline=True
        )
        if context is not None and context.is_actively_tracked and context.tracked_release is not None:
            listened, countable, progress_percent = self._get_release_progress_parts(context.tracked_release)
            embed.add_field(
                name="Progress",
                value=f"{listened}/{countable} ({progress_percent}%)",
                inline=True
            )

        return embed

    async def _resolve_current_post_context(
        self,
        state: PlaybackState,
        check_duplicate: bool = False
    ) -> CurrentPostContext:
        if not state.item:
            raise ValueError("Missing playback item")

        album = state.item.get("album", {})
        album_id = album.get("id")
        if not album_id:
            raise ValueError("Missing album ID")

        tracked_release = await self.db.get_release(album_id)
        release_for_post = tracked_release
        if release_for_post is None and check_duplicate:
            release_for_post = await self.tracker._build_release_from_spotify(album_id)

        duplicate_post = None
        if check_duplicate and release_for_post is not None:
            if tracked_release is not None:
                if tracked_release.is_relisten or tracked_release.duplicate_state == "found":
                    duplicate_post = await self.tracker._get_cached_wordpress_post(
                        tracked_release.duplicate_post_id
                    )
                elif tracked_release.duplicate_post_id:
                    duplicate_post = await self.tracker._get_cached_wordpress_post(
                        tracked_release.duplicate_post_id
                    )
            else:
                duplicate_post = await self.tracker._check_duplicate(release_for_post)
                if duplicate_post:
                    release_for_post.duplicate_post_id = duplicate_post.id
                else:
                    release_for_post.duplicate_post_id = None

        return CurrentPostContext(
            tracked_release=tracked_release,
            release_for_post=release_for_post,
            duplicate_post=duplicate_post,
            is_actively_tracked=(
                tracked_release is not None
                and tracked_release.status in ACTIVE_TRACKED_STATUSES
            )
        )

    async def _handle_current_post_confirm(self, interaction: discord.Interaction, playback_state):
        if not playback_state or not playback_state.item:
            await interaction.response.send_message(
                "⚠️ Unable to publish the current track.",
                ephemeral=True
            )
            return

        album = playback_state.item.get("album", {})
        album_id = album.get("id")
        if not album_id:
            await interaction.response.send_message(
                "⚠️ Unable to determine the current album.",
                ephemeral=True
            )
            return

        context = await self._resolve_current_post_context(playback_state, check_duplicate=True)
        release = await self.tracker._get_or_create_release(album_id)
        if context.duplicate_post is not None:
            release.duplicate_post_id = context.duplicate_post.id
        logger.info(f"Publishing current playback for album {album_id} with context: {context}")
        await self._publish_release_with_feedback(
            interaction,
            release,
            success_message="✅ Current content has been posted to WordPress. A notification has been sent.",
            already_published_message=(
                f"✅ This content has already been published as post {release.wordpress_post_id}."
                if release.wordpress_post_id
                else "✅ This content has already been published."
            ),
            already_publishing_message="⏳ This content is already being published.",
            as_relisten=context.will_publish_as_relisten
        )

    async def _publish_release_with_feedback(
        self,
        interaction: discord.Interaction,
        release: Release,
        success_message: str,
        already_published_message: str,
        already_publishing_message: str,
        as_relisten: bool = False
    ):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        try:
            outcome = await self.tracker.publish_release_now(release, as_relisten=as_relisten)
            if outcome == "already_published":
                await interaction.followup.send(already_published_message, ephemeral=True)
                return

            if outcome == "already_publishing":
                await interaction.followup.send(already_publishing_message, ephemeral=True)
                return

            await interaction.followup.send(success_message, ephemeral=True)
        except Exception as e:
            logger.error(f"Error publishing release {release.spotify_id}: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error publishing release: {str(e)[:100]}",
                ephemeral=True
            )

    def _build_inprogress_embed(self, page_data: InProgressPage) -> discord.Embed:
        featured = page_data.featured
        embed = discord.Embed(
            title=f"Active Releases ({page_data.total_releases})",
            description="Most recently tracked album is pinned at the top.",
            color=0x1DB954
        )
        if featured.cover_url:
            embed.set_thumbnail(url=featured.cover_url)

        embed.add_field(
            name=f"Featured: {_clip_discord_text(featured.title, 245)}",
            value=self._format_inprogress_release(featured, include_last_seen=True),
            inline=False
        )

        for offset, release in enumerate(page_data.items, start=1):
            position = page_data.page * INPROGRESS_PAGE_SIZE + offset
            embed.add_field(
                name=f"{position}. {_clip_discord_text(release.title, 245)}",
                value=self._format_inprogress_release(release, include_last_seen=False),
                inline=False
            )

        if not page_data.items:
            embed.add_field(
                name="Other releases",
                value="No other active releases on this page.",
                inline=False
            )

        embed.set_footer(
            text=(
                f"Page {page_data.page + 1}/{page_data.total_pages} "
                f"| Total active releases: {page_data.total_releases}"
            )
        )
        return embed

    def _format_inprogress_release(self, release: Release, include_last_seen: bool) -> str:
        artist_names = [artist.name for artist in release.artists]
        listened, countable, progress_percent = self._get_release_progress_parts(release)
        next_track = get_next_unlistened_track(release)

        lines = [
            f"Artists: {', '.join(artist_names[:2]) or 'Unknown'}",
            f"Type: {release.release_type.value} | Progress: {listened}/{countable} ({progress_percent}%)",
            f"Status: {release.status.value}",
            f"Next: {next_track.title if next_track else 'None'}"
        ]
        if include_last_seen:
            lines.append(f"Last tracked: <t:{int(release.last_seen.timestamp())}:R>")
        return "\n".join(lines)

    def _get_release_progress_parts(self, release: Release) -> tuple[int, int, int]:
        listened = sum(1 for track in release.tracks if track.is_countable and track.listened)
        countable = sum(1 for track in release.tracks if track.is_countable)
        progress_percent = int(release.progress * 100)
        return listened, countable, progress_percent

    async def _handle_inprogress_page(self, interaction: discord.Interaction, page: int):
        try:
            releases = await self.db.get_active_releases()
            page_data = build_inprogress_page(releases, page)

            if page_data is None:
                await interaction.response.edit_message(
                    content="No active releases.",
                    embed=None,
                    view=None
                )
                return

            await interaction.response.edit_message(
                content=None,
                embed=self._build_inprogress_embed(page_data),
                view=InProgressView(self, page_data)
            )
        except Exception as e:
            logger.error(f"Error paging /inprogress: {e}")
            message = f"❌ Error refreshing releases: {str(e)[:100]}"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def _handle_inprogress(self, interaction: discord.Interaction):
        """Handle /inprogress command."""
        # Check authorization
        if not self._check_authorized(interaction.user.id):
            await interaction.response.send_message(
                "❌ You are not authorized to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            # Get active releases
            releases = await self.db.get_active_releases()

            if not releases:
                await interaction.followup.send("No active releases.", ephemeral=True)
                return

            page_data = build_inprogress_page(releases, 0)
            embed = self._build_inprogress_embed(page_data)
            await interaction.followup.send(embed=embed, view=InProgressView(self, page_data), ephemeral=True)

        except Exception as e:
            logger.error(f"Error in /inprogress: {e}")
            await interaction.followup.send(
                f"❌ Error fetching releases: {str(e)[:100]}",
                ephemeral=True
            )

    async def _handle_current(self, interaction: discord.Interaction):
        """Handle /current command."""
        # Check authorization
        if not self._check_authorized(interaction.user.id):
            await interaction.response.send_message(
                "❌ You are not authorized to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            # Get current playback state
            playback_data = await self.tracker.spotify.get_playback_state()

            if playback_data is None:
                await interaction.followup.send(
                    "⏹ No active playback",
                    ephemeral=True
                )
                return

            # Parse state
            state = self.tracker._parse_playback_state(playback_data)

            if not state.item:
                await interaction.followup.send(
                    "⏹ No active playback",
                    ephemeral=True
                )
                return

            context = await self._resolve_current_post_context(state)
            embed = self._build_current_embed(state, context)

            post_label = "Post early" if context.is_actively_tracked else "Post current content"
            view = CurrentPlaybackActionView(self, state, post_label=post_label)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in /current: {e}")
            await interaction.followup.send(
                f"❌ Error fetching playback: {str(e)[:100]}",
                ephemeral=True
            )

    async def _handle_random(self, interaction: discord.Interaction):
        """Handle /random command."""
        if not self._check_authorized(interaction.user.id):
            await interaction.response.send_message(
                "❌ You are not authorized to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            album = await self.db.get_random_unposted_saved_library_album()
            if album is None:
                await interaction.followup.send(
                    "No unposted saved-library albums found.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                embed=self._build_random_album_embed(album),
                view=RandomAlbumView(self),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in /random: {e}")
            await interaction.followup.send(
                f"❌ Error picking a random album: {str(e)[:100]}",
                ephemeral=True
            )

    async def _handle_random_reroll(self, interaction: discord.Interaction):
        """Handle the /random re-roll button by editing the existing message."""
        if not self._check_authorized(interaction.user.id):
            await interaction.response.send_message(
                "❌ You are not authorized to interact with this prompt.",
                ephemeral=True
            )
            return

        try:
            album = await self.db.get_random_unposted_saved_library_album()
            if album is None:
                await interaction.response.edit_message(
                    content="No unposted saved-library albums found.",
                    embed=None,
                    view=None
                )
                return

            await interaction.response.edit_message(
                content=None,
                embed=self._build_random_album_embed(album),
                view=RandomAlbumView(self)
            )
        except Exception as e:
            logger.error(f"Error re-rolling /random: {e}")
            message = f"❌ Error picking a random album: {str(e)[:100]}"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    def _build_random_album_embed(self, album: SavedLibraryAlbum) -> discord.Embed:
        artist_text = ", ".join(album.artists[:5]) or "Unknown"
        embed = discord.Embed(
            title=album.title,
            description=artist_text,
            url=album.spotify_url or None,
            color=0x1DB954
        )
        embed.add_field(name="Release type", value=album.release_type.value, inline=True)
        embed.add_field(name="Saved", value=f"<t:{int(album.added_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Spotify ID", value=album.spotify_id, inline=False)
        if album.spotify_url:
            embed.add_field(name="Spotify link", value=album.spotify_url, inline=False)
        if album.cover_url:
            embed.set_thumbnail(url=album.cover_url)
        return embed

    async def _handle_service(self, interaction: discord.Interaction):
        """Handle /service command."""
        # Check authorization
        if not self._check_authorized(interaction.user.id):
            await interaction.response.send_message(
                "❌ You are not authorized to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            # Get service status
            active_releases = await self.db.get_active_releases()
            last_poll = await self.db.get_service_state("last_poll")
            saved_library_stats = await self.db.get_saved_library_stats()
            saved_library_last_sync = await self.db.get_service_state("spotify_saved_library.last_synced_at")

            embed = discord.Embed(
                title="Service Status",
                color=0x1DB954
            )

            embed.add_field(
                name="Status",
                value="✅ Running",
                inline=True
            )
            embed.add_field(
                name="Active Releases",
                value=str(len(active_releases)),
                inline=True
            )
            embed.add_field(
                name="Saved Library Albums",
                value=str(saved_library_stats.total),
                inline=True
            )
            embed.add_field(
                name="Listened",
                value=(
                    f"{saved_library_stats.posted_listened}/{saved_library_stats.total} "
                    f"({saved_library_stats.percent * 100:.1f}%)"
                ),
                inline=True
            )
            embed.add_field(
                name="Database",
                value="✅ Connected",
                inline=True
            )

            if last_poll:
                embed.add_field(
                    name="Last Poll",
                    value=f"<t:{int(datetime.fromisoformat(last_poll).timestamp())}:R>",
                    inline=True
                )

            if saved_library_last_sync:
                embed.add_field(
                    name="Library Sync",
                    value=f"<t:{int(datetime.fromisoformat(saved_library_last_sync).timestamp())}:R>",
                    inline=True
                )

            embed.add_field(
                name="Commands",
                value="/inprogress, /current, /random, /service",
                inline=False
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in /service: {e}")
            await interaction.followup.send(
                f"❌ Error fetching status: {str(e)[:100]}",
                ephemeral=True
            )
