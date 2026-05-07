#!/usr/bin/env python3
"""
Run database migrations.
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import Config
from database import Database

async def main():
    config = Config()
    db = Database(config)

    try:
        await db.initialize()
        print("Migrations completed successfully.")
    except Exception as e:
        print(f"Migration error: {e}")
        sys.exit(1)
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())