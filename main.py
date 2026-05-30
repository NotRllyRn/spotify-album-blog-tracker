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

SAVED_LIBRARY_SYNC_INTERVAL = timedelta(hours=24)

class Service:
    def __init__(self):
        self.config = Config()
        self.db = Database(self.config)
        self.publisher = Publisher(self.config, self.db)
        self.tracker = Tracker(self.config, self.db, self.publisher)
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
        await self.tracker.stop()
        await self.discord_bot.stop()
        await self.publisher.close()
        await self.db.close()

async def main():
    service = None

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        if service is not None:
            asyncio.create_task(service.stop())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        service = Service()
        await service.start()
    except Exception as e:
        logger.error(f"Service error: {e}", exc_info=True)
        if service is not None:
            await service.stop()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
