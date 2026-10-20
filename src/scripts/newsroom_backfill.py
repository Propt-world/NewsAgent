"""
Backfill (step 8): links for items approved before the Newsroom existed.

Default is a dry run: it writes nothing to the database and lists every planned
change in --out/backfill_plan.csv. --apply makes the changes (asks you to type
APPLY unless --yes). --undo removes every link the backfill made (dry run first,
then --undo --apply). Safe to re-run: done items are not linked twice, and
nothing a reviewer removed or dismissed is linked again.

Articles need names: existing suggestions, else --names-cache (e.g. the trial's
model_names.jsonl), else the real name step (paid; needs NEWSROOM_ENABLED=true;
stops starting calls at --max-usd; results are appended to the cache).

    python src/scripts/newsroom_backfill.py --out backfill
    python src/scripts/newsroom_backfill.py --out backfill --apply
    python src/scripts/newsroom_backfill.py --out backfill --undo [--apply]
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

from pymongo import AsyncMongoClient

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings
from src.newsroom import backfill as bf
from src.newsroom import db as ndb
from src.newsroom.pipeline import split_names
from src.scheduler.db import _database_target, _mongo_client_options

SOURCE_TYPES = {"article": ["article"], "signal": ["signal"], "both": ["article", "signal"]}


def write_csv(path: Path, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{question}\nType APPLY to continue: ").strip() == "APPLY"


async def article_names(db, articles, cache_path: Path, max_usd: float, concurrency: int) -> tuple:
    """id -> (organisations, locations) for every article that has names; plus run statistics."""
    names, todo = {}, []
    cache = bf.read_names_cache(cache_path)
    stats = {"from_existing_suggestions": 0, "from_cache": 0, "ai_called": 0, "ai_failed": 0,
             "no_names_cost_cap": 0, "cost_usd": 0.0}
    for doc in articles:
        article_id = bf.item_id(doc)
        existing = await bf.existing_names(db, "article", article_id)
        if existing is not None:
            names[article_id] = existing
            stats["from_existing_suggestions"] += 1
        elif article_id in cache:
            names[article_id] = split_names(cache[article_id])
            stats["from_cache"] += 1
        else:
            todo.append(doc)
    if not todo:
        return names, stats
    if not settings.newsroom_enabled:
        raise SystemExit(f"{len(todo)} article(s) need the AI name step: set NEWSROOM_ENABLED=true (or give --names-cache).")

    from langchain_community.callbacks.manager import get_openai_callback
    prompts = bf.init_db_prompts()
    semaphore = asyncio.Semaphore(concurrency)
    with get_openai_callback() as usage, open(cache_path, "a", encoding="utf-8") as out:
        async def one(doc):
            async with semaphore:
                if usage.total_cost >= max_usd:
                    stats["no_names_cost_cap"] += 1
                    return
                found, error = await bf.ai_article_names(doc, prompts)
                stats["ai_called"] += 1
                out.write(json.dumps({"article_id": bf.item_id(doc), "names": found, "error": error}) + "\n")
                out.flush()
                if error or found is None:
                    stats["ai_failed"] += 1
                else:
                    names[bf.item_id(doc)] = split_names(found)
        await asyncio.gather(*(one(d) for d in todo))
        stats["cost_usd"] = round(float(usage.total_cost), 4)
    return names, stats


async def run_backfill(args, db, sources) -> dict:
    index = await bf.load_index(db)
    if not index.entities:
        raise SystemExit("The starting lists are not in this database yet: run newsroom_seed.py --apply first.")
    summary, plan_rows, work = {}, [], []
    for source_type in SOURCE_TYPES[args.type]:
        items = await bf.approved_items(sources, source_type, args.limit)
        if source_type == "article":
            names, stats = await article_names(db, items, args.names_cache or args.out / "model_names.jsonl",
                                               args.max_usd, args.concurrency)
        else:
            names, stats = {bf.item_id(d): bf.signal_names(d) for d in items}, {}
        for doc in items:
            if bf.item_id(doc) in names:
                organisations, locations = names[bf.item_id(doc)]
                plan_rows += await bf.plan_item(db, index, source_type, doc, organisations, locations)
                work.append((source_type, doc, organisations, locations))
        summary[source_type] = {"approved_items": len(items), "items_with_names": sum(1 for d in items if bf.item_id(d) in names),
                                **stats}
    actions = Counter(r["action"] for r in plan_rows)
    summary["plan"] = {
        "names": len(plan_rows),
        "by_action": dict(actions),
        "items_getting_a_link": len({(r["source_type"], r["source_id"]) for r in plan_rows if r["action"] == bf.AUTO_LINK}),
        "top_auto_linked_entities": Counter(r["entity"] for r in plan_rows if r["action"] == bf.AUTO_LINK).most_common(15),
        "top_new_names": Counter(r["name"] for r in plan_rows if r["action"] == bf.NEW_NAME).most_common(15),
    }
    write_csv(args.out / "backfill_plan.csv", plan_rows)
    if not args.apply:
        summary["mode"] = "dry run: nothing was written"
        return summary
    if not confirm(f"Write {actions[bf.AUTO_LINK]} links and the suggestions to {_database_target()} "
                   f"({settings.MONGO_DB_NAME})?", args.yes):
        summary["mode"] = "cancelled: nothing was written"
        return summary
    errors = await ndb.ensure_indexes_async(db)
    if errors:
        raise SystemExit("Could not make sure the Newsroom indexes exist: " + "; ".join(errors))
    applied, totals = [], Counter()
    for source_type, doc, organisations, locations in work:
        counts = await bf.apply_item(db, sources, source_type, doc, organisations, locations)
        totals.update(counts)
        applied.append({"source_type": source_type, "source_id": bf.item_id(doc), **counts})
    write_csv(args.out / "backfill_applied.csv", applied)
    summary["mode"] = "applied"
    summary["applied"] = dict(totals)
    return summary


async def run_undo(args, db) -> dict:
    report = await bf.undo(db, apply=False)
    write_csv(args.out / "undo_plan.csv", report.rows)
    summary = {"backfill_links": report.links, "items": report.items,
               "removed_by_reviewers_kept": report.removed_by_reviewers_kept}
    if not args.apply:
        summary["mode"] = "dry run: nothing was removed"
        return summary
    if not confirm(f"Remove {report.links} backfill links from {_database_target()} ({settings.MONGO_DB_NAME})?", args.yes):
        summary["mode"] = "cancelled: nothing was removed"
        return summary
    done = await bf.undo(db, apply=True)
    summary.update({"mode": "undone", "removed": done.links})
    return summary


async def main_async(args) -> dict:
    client = AsyncMongoClient(settings.DATABASE_URL, **_mongo_client_options())
    try:
        db = client[settings.MONGO_DB_NAME]
        sources = ndb.ItemSources(
            articles=client[settings.MONGO_DB_NAME]["processed_articles"],
            signals=client[settings.NEWSROOM_SIGNALS_DB_NAME][settings.NEWSROOM_SIGNALS_COLLECTION],
            chunks=client[settings.MONGO_DB_NAME][settings.MONGO_DB_COLLECTION],
        )
        return await (run_undo(args, db) if args.undo else run_backfill(args, db, sources))
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="folder for the plan and report files")
    parser.add_argument("--type", choices=sorted(SOURCE_TYPES), default="both")
    parser.add_argument("--limit", type=int, default=None, help="only the first N approved items of each type (for a try-out)")
    parser.add_argument("--names-cache", type=Path, default=None, help="article names JSONL (default: <out>/model_names.jsonl)")
    parser.add_argument("--max-usd", type=float, default=1.0, help="the AI name step stops starting calls at this cost")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--undo", action="store_true", help="remove every link the backfill made")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Database: {_database_target()} | articles and Newsroom: {settings.MONGO_DB_NAME} | signals: {settings.NEWSROOM_SIGNALS_DB_NAME}")
    summary = asyncio.run(main_async(args))
    (args.out / ("undo_summary.json" if args.undo else "backfill_summary.json")).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\nFiles in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
