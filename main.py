#!/usr/bin/env python3
"""
Main entry point for the Spotify WordPress Album Tracker service.
"""

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from datetime import timedelta

PROJECT_ROOT = Path(__file__).parent

# Add src to path
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from logging_config import configure_logging

configure_logging(PROJECT_ROOT)
logger = logging.getLogger(__name__)

from config import Config
from database import Database
from tracker import Tracker
from discord_bot import DiscordBot
from publisher import Publisher
from saved_library import SavedLibraryService
from album_metadata_cache import AlbumMetadataCacheService

SAVED_LIBRARY_SYNC_INTERVAL = timedelta(hours=24)

class Service:
    def __init__(self):
        self.config = Config()
        self.db = Database(self.config)
        self.metadata_cache = (
            AlbumMetadataCacheService(self.config, self.db)
            if self.config.fill_scf_enabled else None)
        self.tracker = Tracker(
            self.config, self.db, metadata_cache=self.metadata_cache)
        self.publisher = Publisher(
            self.config, self.db, self.metadata_cache, spotify=self.tracker.spotify)
        self.tracker.publisher = self.publisher
        self.saved_library = SavedLibraryService(self.db, self.tracker.spotify)
        self.discord_bot = DiscordBot(self.config, self.db, self.tracker)
        self.tracker.set_discord_bot(self.discord_bot)
        self.saved_library_sync_task = None

    async def start(self):
        logger.info("Starting Spotify WordPress Album Tracker...")

        # Ensure Spotify authorization
        await self.tracker.spotify.ensure_authorized()

        # Initialize database
        await self.db.initialize()

        # Refresh WordPress duplicate data, then synchronize the saved Spotify library.
        await self._refresh_saved_library()

        # Start Discord bot and wait until ready before tracking begins
        discord_task = asyncio.create_task(self.discord_bot.start())
        await self.discord_bot.wait_until_ready()

        # Start tracker
        tracker_task = asyncio.create_task(self.tracker.run())
        self.saved_library_sync_task = asyncio.create_task(self._run_saved_library_sync_loop())

        # Wait for both (they run indefinitely)
        await asyncio.gather(tracker_task, discord_task, self.saved_library_sync_task)

    async def _refresh_saved_library(self):
        try:
            await self.publisher.refresh_post_cache()
            await self.saved_library.sync()
            if self.metadata_cache is not None:
                self.metadata_cache.trigger_saved_library_backfill()
        except Exception as e:
            logger.error(f"Saved library sync failed: {e}", exc_info=True)

    async def _run_saved_library_sync_loop(self):
        while True:
            await asyncio.sleep(SAVED_LIBRARY_SYNC_INTERVAL.total_seconds())
            await self._refresh_saved_library()

    async def stop(self):
        logger.info("Stopping service...")
        if self.saved_library_sync_task is not None:
            self.saved_library_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.saved_library_sync_task
        if self.metadata_cache is not None:
            await self.metadata_cache.stop()
        await self.tracker.stop()
        await self.discord_bot.stop()
        await self.publisher.close()
        await self.db.close()

async def main():
    service = None
    service_task = None
    stop_task = None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)

    try:
        service = Service()
        service_task = asyncio.create_task(service.start())
        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            (service_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            logger.info("Shutdown signal received.")
        else:
            await service_task
    except Exception as e:
        logger.error(f"Service error: {e}", exc_info=True)
        return 1
    finally:
        for task in (service_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if service is not None:
            await service.stop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
