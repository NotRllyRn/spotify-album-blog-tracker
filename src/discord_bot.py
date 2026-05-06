"""
Discord bot for control plane.
"""

import discord
from discord import app_commands
import logging
from typing import Optional
from datetime import datetime

from config import Config
from database import Database
from tracker import Tracker

logger = logging.getLogger(__name__)

class DiscordBot:
    def __init__(self, config: Config, db: Database, tracker: Tracker):
        self.config = config
        self.db = db
        self.tracker = tracker

        intents = discord.Intents.default()
        intents.message_content = True
        self.bot = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.bot)

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
            logger.info(f"Discord bot logged in as {self.bot.user}")

    async def start(self):
        """Start the Discord bot."""
        await self.bot.start(self.config.discord_bot_token)

    async def stop(self):
        """Stop the Discord bot."""
        await self.bot.close()

    def _check_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        return user_id == self.config.discord_user_id

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

            await interaction.followup.send(embed=embed, ephemeral=True)

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

            await interaction.followup.send(embed=embed, ephemeral=True)

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