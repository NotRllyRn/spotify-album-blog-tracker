#!/usr/bin/env python3
"""
Main entry point for the Spotify WordPress Album Tracker service.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from config import Config
from database import Database
from tracker import Tracker
from discord_bot import DiscordBot
from publisher import Publisher

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class Service:
    def __init__(self):
        self.config = Config()
        self.db = Database(self.config)
        self.publisher = Publisher(self.config)
        self.tracker = Tracker(self.config, self.db, self.publisher)
        self.discord_bot = DiscordBot(self.config, self.db, self.tracker)

    async def start(self):
        logger.info("Starting Spotify WordPress Album Tracker...")

        # Initialize database
        await self.db.initialize()

        # Refresh WordPress post cache for duplicate detection
        await self.publisher.refresh_post_cache()

        # Start tracker
        tracker_task = asyncio.create_task(self.tracker.run())

        # Start Discord bot
        discord_task = asyncio.create_task(self.discord_bot.start())

        # Wait for both (they run indefinitely)
        await asyncio.gather(tracker_task, discord_task)

    async def stop(self):
        logger.info("Stopping service...")
        await self.tracker.stop()
        await self.discord_bot.stop()
        await self.publisher.close()
        await self.db.close()

async def main():
    service = Service()

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        asyncio.create_task(service.stop())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await service.start()
    except Exception as e:
        logger.error(f"Service error: {e}", exc_info=True)
        await service.stop()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())