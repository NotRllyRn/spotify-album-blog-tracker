"""
Discord bot for control plane.
"""

import asyncio
import discord
from discord import app_commands
import logging
from typing import Optional, List
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from config import Config
from database import Database
from tracker import Tracker
from models import Release, PromptType, PromptState, DiscordPrompt, LifecycleStatus

logger = logging.getLogger(__name__)

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

class RelistenPromptView(PromptView):
    @discord.ui.button(
        label="Post as Relisten",
        style=discord.ButtonStyle.success,
        custom_id="prompt_relisten_post"
    )
    async def post_relisten(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "post_relisten")

    @discord.ui.button(
        label="Ignore",
        style=discord.ButtonStyle.danger,
        custom_id="prompt_relisten_ignore"
    )
    async def ignore_relisten(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.discord_bot.handle_prompt_action(interaction, "ignore_relisten")

class UndoPromptView(PromptView):
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

class InProgressSelectView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", releases: List[Release]):
        super().__init__(discord_bot)
        options = []
        for release in releases:
            artist_names = ", ".join([a.name for a in release.artists][:2]) or "Unknown"
            progress_percent = int(release.progress * 100)
            label = f"{release.title[:90]}"
            description = f"{release.release_type.value}, {progress_percent}%, {artist_names}"
            options.append(discord.SelectOption(label=label, value=release.spotify_id, description=description[:100]))

        select = discord.ui.Select(
            placeholder="Select a release to manage",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="inprogress_select"
        )

        async def select_callback(interaction: discord.Interaction):
            if select.values:
                await self.discord_bot._handle_inprogress_selection(interaction, select.values[0])
            else:
                await interaction.response.send_message(
                    "⚠️ No release selected.",
                    ephemeral=True
                )

        select.callback = select_callback
        select.options = select.options[:25]
        self.add_item(select)

class ReleaseActionView(PromptView):
    def __init__(self, discord_bot: "DiscordBot", release_id: str):
        super().__init__(discord_bot)
        self.release_id = release_id

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
    def __init__(self, discord_bot: "DiscordBot", playback_state):
        super().__init__(discord_bot)
        self.playback_state = playback_state

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

        @self.bot.event
        async def on_ready():
            await self.tree.sync()
            self.ready_event.set()
            logger.info(f"Discord bot logged in as {self.bot.user}")

    def _register_views(self):
        self.bot.add_view(SeventyFivePromptView(self))
        self.bot.add_view(RelistenPromptView(self))
        self.bot.add_view(UndoPromptView(self))

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
            public_base = urlparse(self.config.wordpress_url.rstrip("/"))

            if not parsed_link.path:
                return raw_link

            return urlunparse((public_base.scheme, public_base.netloc, parsed_link.path, parsed_link.params, parsed_link.query, parsed_link.fragment))
        except Exception:
            return raw_link

    async def update_presence(self, state) -> None:
        """Update the bot presence to reflect current listening state."""
        if not self.bot.is_ready() or state is None or not state.item:
            try:
                await self.bot.change_presence(activity=None)
            except Exception:
                pass
            return

        item = state.item
        track_name = item.get("name", "Unknown")
        artists = [a.get("name") for a in item.get("artists", []) if a.get("name")]
        artist_text = ", ".join(artists[:2]) if artists else "Unknown"
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{track_name} by {artist_text}"
        )

        try:
            await self.bot.change_presence(activity=activity)
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

    async def send_relisten_prompt(self, release: Release) -> Optional[discord.Message]:
        """Send a relisten prompt for duplicate detection."""
        embed = discord.Embed(
            title="Duplicate release detected",
            description=f"{release.title} by {', '.join([a.name for a in release.artists])}",
            color=0xE67E22
        )
        embed.add_field(
            name="Status",
            value="A duplicate WordPress post was detected for this release.",
            inline=False
        )
        embed.set_thumbnail(url=release.cover_url)

        view = RelistenPromptView(self)
        return await self._send_dm(
            "This release appears to already exist on WordPress. Would you like to post it as a Relisten or ignore it?",
            embed=embed,
            view=view
        )

    async def send_publish_notification(self, release: Release, post: dict) -> Optional[discord.Message]:
        """Send notification after a release is published."""
        embed = discord.Embed(
            title="Release published",
            description=f"{release.title} by {', '.join([a.name for a in release.artists])}",
            color=0x1DB954
        )
        embed.add_field(name="Post ID", value=str(post["id"]), inline=True)
        wordpress_link = self._get_public_wordpress_link(post.get("link") or post.get("guid", ""))
        embed.add_field(name="WordPress link", value=wordpress_link or "Unavailable", inline=False)
        embed.add_field(name="Release type", value=release.release_type.value, inline=True)
        embed.add_field(name="Progress", value=f"{int(release.progress * 100)}%", inline=True)
        embed.set_thumbnail(url=release.cover_url)

        view = None
        if release.wordpress_post_id:
            view = UndoPromptView(self)

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

    async def handle_prompt_action(self, interaction: discord.Interaction, action: str):
        await interaction.response.defer(ephemeral=True)

        prompt = await self.db.get_discord_prompt(str(interaction.message.id))
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

        release = await self.db.get_release(prompt.release_id)
        if release is None:
            await interaction.followup.send(
                "⚠️ Unable to find the release associated with this prompt.",
                ephemeral=True
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
            elif prompt.prompt_type == PromptType.PROMPT_RELISTEN.value:
                if action == "post_relisten":
                    await self._handle_relisten_publish(interaction, release, prompt)
                elif action == "ignore_relisten":
                    await self._handle_relisten_ignore(interaction, release, prompt)
                else:
                    await self._unknown_prompt_action(interaction)
            elif prompt.prompt_type == PromptType.PROMPT_UNDO.value:
                if action == "undo_post":
                    await self._handle_undo_post(interaction, release, prompt)
                elif action == "keep_post":
                    await self._handle_keep_post(interaction, release, prompt)
                else:
                    await self._unknown_prompt_action(interaction)
            else:
                await self._unknown_prompt_action(interaction)
        except Exception as e:
            logger.error(f"Error handling prompt action {action}: {e}")
            await interaction.followup.send(
                f"❌ Error handling prompt action: {str(e)[:100]}",
                ephemeral=True
            )

    async def _unknown_prompt_action(self, interaction: discord.Interaction):
        await interaction.followup.send(
            "⚠️ Unknown action for this prompt.",
            ephemeral=True
        )

    async def _handle_75_publish(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.ACCEPTED.value)
        await self.tracker.publish_release_now(release)
        await interaction.followup.send(
            "✅ Published early. A notification has been sent.",
            ephemeral=True
        )

    async def _handle_75_wait(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.DECLINED.value)
        release.status = LifecycleStatus.ACTIVE
        await self.db.save_release(release)
        await interaction.followup.send(
            "✅ Okay — continuing to track the release until it completes.",
            ephemeral=True
        )

    async def _handle_relisten_publish(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.ACCEPTED.value)
        await self.tracker.publish_release_now(release, as_relisten=True)
        await interaction.followup.send(
            "✅ Published as Relisten. A notification has been sent.",
            ephemeral=True
        )

    async def _handle_relisten_ignore(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.DECLINED.value)
        release.status = LifecycleStatus.IGNORED_SINGLE
        await self.db.save_release(release)
        await interaction.followup.send(
            "✅ Ignored the duplicate release. It will not be published.",
            ephemeral=True
        )

    async def _handle_undo_post(self, interaction: discord.Interaction, release: Release, prompt: DiscordPrompt):
        await self.db.update_discord_prompt_state(prompt.discord_message_id, PromptState.ACCEPTED.value)
        if not release.wordpress_post_id:
            await interaction.followup.send(
                "⚠️ Unable to undo because the WordPress post ID is missing.",
                ephemeral=True
            )
            return

        success = await self.tracker.publisher.trash_post(release.wordpress_post_id)
        if success:
            release.status = LifecycleStatus.TRASHED_POST
            await self.db.save_release(release)
            await interaction.followup.send(
                "✅ The post has been moved to trash.",
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

    async def _handle_inprogress_selection(self, interaction: discord.Interaction, release_id: str):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        embed = self._build_release_summary_embed(release, title="Manage Release")
        await interaction.response.edit_message(embed=embed, view=ReleaseActionView(self, release_id))

    async def _handle_publish_release(self, interaction: discord.Interaction, release_id: str):
        release = await self.db.get_release(release_id)
        if release is None:
            await interaction.response.send_message(
                "⚠️ Unable to find the selected release.",
                ephemeral=True
            )
            return

        if release.status == LifecycleStatus.PUBLISHED and release.wordpress_post_id:
            await interaction.response.send_message(
                f"✅ This release is already published as post {release.wordpress_post_id}.",
                ephemeral=True
            )
            return

        await self.tracker.publish_release_now(release)
        await interaction.response.send_message(
            "✅ Release published successfully. A notification has been sent.",
            ephemeral=True
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
        countable = sum(1 for t in release.tracks if t.is_countable)
        listened = sum(1 for t in release.tracks if t.is_countable and t.listened)

        embed = discord.Embed(
            title=title,
            description=f"{release.title} by {artist_names}",
            color=0x1DB954
        )
        embed.add_field(name="Release type", value=release.release_type.value, inline=True)
        embed.add_field(name="Progress", value=f"{listened}/{countable} ({int(release.progress * 100)}%)", inline=True)
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

        embed = self._build_current_preview_embed(playback_state)
        view = ConfirmCurrentPostView(self, playback_state)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _build_current_preview_embed(self, state) -> discord.Embed:
        item = state.item
        album = item.get("album", {})
        artists = [a["name"] for a in item.get("artists", [])]

        embed = discord.Embed(
            title="Post current playback",
            description=f"This would publish the current listening target to WordPress.",
            color=0x1DB954
        )
        embed.add_field(name="Track", value=item.get("name", "Unknown"), inline=False)
        embed.add_field(name="Album", value=album.get("name", "Unknown"), inline=False)
        embed.add_field(name="Artists", value=", ".join(artists[:5]) or "Unknown", inline=False)
        embed.add_field(name="Status", value="▶ Playing" if state.is_playing else "⏸ Paused", inline=True)
        embed.add_field(name="Shuffle", value="On" if state.shuffle_state else "Off", inline=True)
        embed.add_field(name="Album type", value=album.get("album_type", "Unknown"), inline=True)
        if album.get("images"):
            embed.set_thumbnail(url=album.get("images")[0].get("url", ""))
        return embed

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

        release = await self.tracker._get_or_create_release(album_id)
        if release.status == LifecycleStatus.PUBLISHED and release.wordpress_post_id:
            await interaction.response.send_message(
                f"✅ This content has already been published as post {release.wordpress_post_id}.",
                ephemeral=True
            )
            return

        await self.tracker.publish_release_now(release)
        await interaction.response.send_message(
            "✅ Current content has been posted to WordPress. A notification has been sent.",
            ephemeral=True
        )

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

            # Create embed with releases
            embed = discord.Embed(
                title=f"Active Releases ({len(releases)})",
                color=0x1DB954
            )

            for release in releases[:10]:  # Limit to 10
                artist_names = [a.name for a in release.artists]
                progress_percent = int(release.progress * 100)
                countable = sum(1 for t in release.tracks if t.is_countable)
                listened = sum(1 for t in release.tracks if t.is_countable and t.listened)

                status_emoji = "▶" if release.status.value == "active" else "⏸"
                embed.add_field(
                    name=f"{status_emoji} {release.title[:50]}",
                    value=(
                        f"Artists: {', '.join(artist_names[:2])}\n"
                        f"Type: {release.release_type.value} | Progress: {listened}/{countable} ({progress_percent}%)\n"
                        f"Status: {release.status.value}"
                    ),
                    inline=False
                )

            await interaction.followup.send(embed=embed, view=InProgressSelectView(self, releases), ephemeral=True)

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

            # Get album info
            item = state.item
            album = item.get("album", {})
            artists = [a["name"] for a in item.get("artists", [])]

            # Create embed
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

            # Check if qualifies
            qualifies = self.tracker._qualifies_for_tracking(state)
            embed.add_field(
                name="Counts for Tracking",
                value="✅ Yes" if qualifies else "❌ No",
                inline=True
            )

            view = CurrentPlaybackActionView(self, state)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in /current: {e}")
            await interaction.followup.send(
                f"❌ Error fetching playback: {str(e)[:100]}",
                ephemeral=True
            )

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

            embed.add_field(
                name="Commands",
                value="/inprogress, /current, /service",
                inline=False
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in /service: {e}")
            await interaction.followup.send(
                f"❌ Error fetching status: {str(e)[:100]}",
                ephemeral=True
            )