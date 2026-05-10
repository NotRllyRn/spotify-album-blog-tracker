#!/usr/bin/env python3
"""
Run database migrations.
"""

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

# Add src to path
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from logging_config import configure_logging

configure_logging(PROJECT_ROOT)
logger = logging.getLogger(__name__)

from config import Config
from database import Database

async def main():
    config = Config()
    db = Database(config)

    try:
        await db.initialize()
        logger.info("Migrations completed successfully.")
    except Exception as e:
        logger.error("Migration error: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())
