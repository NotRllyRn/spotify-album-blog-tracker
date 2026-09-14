import asyncio
import hashlib
import json
import signal
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import httpx
import main
from discord_bot import DiscordBot, ReleaseActionView
from models import DiscordPrompt, LifecycleStatus, PromptState, PromptType
from tests.test_unit import make_release_for_test
from tracker import Tracker
from wordpress_client import WordPressClient


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_path_awaits_service_stop_once(self):
        started = asyncio.Event()

        class FakeService:
            def __init__(self):
                self.stop = AsyncMock()

            async def start(self):
                started.set()
                await asyncio.Event().wait()

        service = FakeService()
        callbacks = {}
        loop = asyncio.get_running_loop()

        def add_handler(signum, callback):
            callbacks[signum] = callback

        with (
            patch.object(main, "Service", return_value=service),
            patch.object(loop, "add_signal_handler", side_effect=add_handler),
            patch.object(loop, "remove_signal_handler", return_value=True),
        ):
            task = asyncio.create_task(main.main())
            await started.wait()
            callbacks[signal.SIGTERM]()
            self.assertEqual(await task, 0)

        service.stop.assert_awaited_once_with()


class PersistentReleaseActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_view_recovers_release_id_from_message_embed(self):
        bot = DiscordBot.__new__(DiscordBot)
        bot._handle_publish_release = AsyncMock()
        bot._handle_edit_metadata_pre_publish = AsyncMock()
        bot._handle_remove_release_prompt = AsyncMock()
        bot._handle_missing_songs = AsyncMock()
        embed = discord.Embed(title="Manage Release")
        embed.add_field(name="Spotify ID", value="real-release-id")
        interaction = SimpleNamespace(
            message=SimpleNamespace(embeds=[embed]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        view = ReleaseActionView(bot, "*")

        for label, handler in (
            ("Publish early", bot._handle_publish_release),
            ("Edit metadata", bot._handle_edit_metadata_pre_publish),
            ("Remove from database", bot._handle_remove_release_prompt),
            ("Show missing songs", bot._handle_missing_songs),
        ):
            button = next(child for child in view.children if child.label == label)
            await button.callback(interaction)
            handler.assert_awaited_once_with(interaction, "real-release-id")


class SeventyFivePromptReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def make_release(self):
        release = make_release_for_test("album-75", "Album 75", datetime(2024, 1, 1))
        release.progress = 0.75
        return release

    async def test_failed_delivery_leaves_release_retryable(self):
        tracker = Tracker.__new__(Tracker)
        tracker.db = SimpleNamespace(
            save_release=AsyncMock(), save_discord_prompt=AsyncMock(),
            log_audit_event=AsyncMock())
        tracker.discord_bot = SimpleNamespace(
            send_75_percent_prompt=AsyncMock(return_value=None))
        release = self.make_release()

        await tracker._send_75_prompt(release)

        self.assertEqual(release.status, LifecycleStatus.ACTIVE)
        tracker.db.save_release.assert_not_awaited()
        tracker.db.save_discord_prompt.assert_not_awaited()

    async def test_failed_publish_keeps_prompt_pending_for_retry(self):
        bot = DiscordBot.__new__(DiscordBot)
        bot.db = SimpleNamespace(update_discord_prompt_state=AsyncMock())
        bot._publish_release_with_feedback = AsyncMock(return_value=None)
        prompt = DiscordPrompt(
            id=1, prompt_type=PromptType.PROMPT_75_PERCENT.value,
            release_id="album-75", wordpress_post_id=None,
            discord_message_id="message-75", state=PromptState.PENDING.value)

        await bot._handle_75_publish(SimpleNamespace(), self.make_release(), prompt)

        bot.db.update_discord_prompt_state.assert_not_awaited()

    async def test_successful_publish_accepts_prompt(self):
        bot = DiscordBot.__new__(DiscordBot)
        bot.db = SimpleNamespace(update_discord_prompt_state=AsyncMock())
        bot._publish_release_with_feedback = AsyncMock(return_value="published")
        prompt = DiscordPrompt(
            id=1, prompt_type=PromptType.PROMPT_75_PERCENT.value,
            release_id="album-75", wordpress_post_id=None,
            discord_message_id="message-75", state=PromptState.PENDING.value)

        await bot._handle_75_publish(SimpleNamespace(), self.make_release(), prompt)

        bot.db.update_discord_prompt_state.assert_awaited_once_with(
            "message-75", PromptState.ACCEPTED.value)


class MultiPageWordPressCacheTests(unittest.IsolatedAsyncioTestCase):
    def response(self, rows, *, total="101", pages="2"):
        return httpx.Response(
            200,
            content=json.dumps(rows).encode(),
            headers={"X-WP-Total": total, "X-WP-TotalPages": pages},
            request=httpx.Request("GET", "https://example.test/wp-json/wp/v2/posts"),
        )

    async def test_matching_page_one_does_not_hide_page_two_post_edit(self):
        page_one = self.response([{"id": 101, "title": "Newest"}])
        changed_page_two = self.response([{"id": 1, "title": "Edited"}])
        http = SimpleNamespace(get=AsyncMock(side_effect=[page_one, changed_page_two]))
        client = WordPressClient.__new__(WordPressClient)
        client.api_url = "https://example.test/wp-json/wp/v2"
        client.client = http

        result = await client.get_posts(
            validate_first_page=True,
            previous_x_wp_total="101",
            previous_first_page_hash=hashlib.sha256(page_one.content).hexdigest(),
        )

        self.assertFalse(result.cache_unchanged)
        self.assertEqual(result.posts[-1]["title"], "Edited")
        self.assertEqual(http.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
