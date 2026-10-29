"""
Gives the articles already waiting in Pending their small vector (src/newsroom/article_vectors.py): articles
processed from now on get one by themselves. Newest first, a few at a time (--limit, 20 unless given); pending
articles that already have one are never picked, so running it again carries on and never pays twice.
One text-embedding-3-large call per article (title + summary, about $0.00004 each); each is its own Phoenix trace.

Default is a dry run (lists them, no AI call):
    python src/scripts/newsroom_pending_vectors.py
    python src/scripts/newsroom_pending_vectors.py --apply
    python src/scripts/newsroom_pending_vectors.py --apply --limit 200
"""
import argparse
import asyncio
import os
import sys

from pymongo import AsyncMongoClient

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings
from src.newsroom import article_vectors
from src.newsroom import db as ndb
from src.scheduler.db import _database_target, _mongo_client_options
from src.utils import tracing

DEFAULT_LIMIT = 20


async def pending_without_vector(db, articles, limit: int):
    """(how many pending articles have no vector, the newest `limit` of them)."""
    have = set(await db[ndb.ARTICLE_VECTORS].distinct("_id"))
    rows = await articles.find({"status": article_vectors.PENDING}, {"final_output.title": 1, "processed_at": 1}) \
        .sort("processed_at", -1).to_list(length=None)
    missing = [r for r in rows if r["_id"] not in have]
    return len(missing), missing[:limit]


async def run(db, articles, apply: bool, limit: int) -> dict:
    waiting, picked = await pending_without_vector(db, articles, limit)
    counts = {"without_vector": waiting, "picked": len(picked), "saved": 0, "not_saved": 0}
    for row in picked:
        title = ((row.get("final_output") or {}).get("title") or "")[:70]
        if not apply:
            print(f"  {row['_id']}  {title}")
            continue
        outcome = await article_vectors.refresh(db, articles, row["_id"])
        counts["saved" if outcome == "saved" else "not_saved"] += 1
        print(f"  {row['_id']}  {title}\n      -> {outcome}")
    return counts


async def main_async(apply: bool, limit: int) -> dict:
    client = AsyncMongoClient(settings.DATABASE_URL, **_mongo_client_options())
    try:
        db = client[settings.MONGO_DB_NAME]
        return await run(db, db["processed_articles"], apply, limit)
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Small vectors for articles already in Pending (dry run by default).")
    parser.add_argument("--apply", action="store_true", help="Make them (one AI call each); without it, only list them")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"How many articles (default {DEFAULT_LIMIT})")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.apply and not settings.NEWSROOM_PENDING_VECTORS_ENABLED:
        sys.exit("NEWSROOM_PENDING_VECTORS_ENABLED is off in .env: nothing would be saved.")
    if args.apply:
        tracing.init_tracing()
    print(f"--- Database: {_database_target()} / {settings.MONGO_DB_NAME} | {'APPLY' if args.apply else 'DRY RUN'} ---")
    counts = asyncio.run(main_async(args.apply, args.limit))
    print()
    for key, value in counts.items():
        print(f"  {key:16} {value}")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to make these.")
    elif counts["without_vector"] > counts["saved"]:
        print(f"\n{counts['without_vector'] - counts['saved']} pending articles still have no vector: run again to carry on.")
    tracing.flush_tracing()
    return 0


if __name__ == "__main__":
    sys.exit(main())
