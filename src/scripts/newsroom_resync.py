"""
Repairs the Newsroom's copies of article and signal details on links (title, link, date,
status, places). The same repair runs every night in the scheduler when the Newsroom is on;
use this to run it now or to see what is out of date.

Default is a dry run (only counts):
    python src/scripts/newsroom_resync.py
    python src/scripts/newsroom_resync.py --apply
"""
import argparse
import asyncio
import os
import sys

from pymongo import AsyncMongoClient

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings
from src.newsroom import db as ndb
from src.newsroom.resync import resync
from src.scheduler.db import _database_target, _mongo_client_options


async def main_async(apply: bool) -> dict:
    client = AsyncMongoClient(settings.DATABASE_URL, **_mongo_client_options())
    try:
        app_db = client[settings.MONGO_DB_NAME]
        sources = ndb.ItemSources(
            articles=app_db["processed_articles"],
            signals=client[settings.NEWSROOM_DB_NAME][settings.NEWSROOM_SIGNALS_COLLECTION],
            chunks=app_db[settings.MONGO_DB_COLLECTION],
            archived=app_db["archived_articles"],
            deleted=app_db["deleted_articles"],
        )
        return await resync(client[settings.NEWSROOM_DB_NAME], sources, apply=apply)
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair the Newsroom's link details (dry run by default).")
    parser.add_argument("--apply", action="store_true", help="Fix what is out of date (default: only count).")
    apply = parser.parse_args().apply
    print(f"--- Database: {_database_target()} / {settings.NEWSROOM_DB_NAME} | {'APPLY' if apply else 'DRY RUN'} ---")
    counts = asyncio.run(main_async(apply))
    for key, value in counts.items():
        print(f"  {key:16} {value}")
    if not apply:
        print("\nDry run only. Re-run with --apply to fix these (the scheduler also does it every night).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
