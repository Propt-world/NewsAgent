"""
Real-estate check trial: runs the check on stored articles before it is switched on.
Read-only: nothing is written to any database; results go to --out.

- Approved articles should nearly all be kept (each one skipped = a real story that would be lost).
- Rejected articles that are skipped = reviewer time saved (rejections also include duplicates
  and weak real-estate stories, so not every rejected article should be skipped).

Input: an export file (--articles, a JSON list of processed_articles records) and/or the database
(--from-db, read-only, newest first per status). Stored articles keep only the rewritten text, so the
trial reads the title and the start of that text (live checks read the fetched page).

    python src/scripts/real_estate_check_trial.py --from-db --statuses approved,rejected --per-status 300 --out re-trial
    python src/scripts/real_estate_check_trial.py --articles approved_articles.json --out re-trial --max-usd 0.30
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings
from src.graph.nodes import check_real_estate as rec

CACHE = "checks.jsonl"


def article_input(doc: dict) -> dict:
    final = doc.get("final_output") or {}
    return {"id": str(doc["_id"]), "status": doc.get("status") or "", "source_id": doc.get("source_id") or "",
            "url": doc.get("url") or "", "title": str(final.get("title") or ""),
            "text": str(final.get("content") or final.get("summary") or "")}


def load_from_db(statuses, per_status: int) -> list:
    from pymongo import MongoClient
    from src.scheduler.db import _mongo_client_options
    client = MongoClient(settings.DATABASE_URL, **_mongo_client_options())
    try:
        col = client[settings.MONGO_DB_NAME]["processed_articles"]
        docs = []
        for status in statuses:
            cursor = col.find({"status": status, "final_output": {"$ne": None}},
                              {"status": 1, "source_id": 1, "url": 1, "final_output.title": 1,
                               "final_output.content": 1, "final_output.summary": 1})
            docs += list(cursor.sort("processed_at", -1).limit(per_status))
        return docs
    finally:
        client.close()


async def run_checks(items: list, cache_path: Path, max_usd: float, concurrency: int) -> dict:
    from langchain_community.callbacks.manager import get_openai_callback
    done = set()
    if cache_path.exists():
        done = {json.loads(line)["id"] for line in cache_path.read_text().splitlines() if line.strip()}
    todo = [i for i in items if i["id"] not in done]
    stats = {"already_checked": len(items) - len(todo), "checked": 0, "failed": 0, "stopped_by_cap": 0, "cost_usd": 0.0}
    semaphore = asyncio.Semaphore(concurrency)
    with get_openai_callback() as usage, open(cache_path, "a", encoding="utf-8") as out:
        async def one(item):
            async with semaphore:
                if usage.total_cost >= max_usd:
                    stats["stopped_by_cap"] += 1
                    return
                try:
                    check, error = await rec.run_check(item["title"], item["text"]), None
                except Exception as e:
                    check, error = None, str(e)[:300]
                stats["checked"] += 1
                stats["failed"] += bool(error)
                out.write(json.dumps({"id": item["id"], "check": check, "error": error}) + "\n")
                out.flush()
        await asyncio.gather(*(one(i) for i in todo))
        stats["cost_usd"] = round(float(usage.total_cost), 4)
    return stats


def report(items: list, cache_path: Path, out: Path) -> dict:
    checks = {}
    for line in cache_path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("check"):
                checks[record["id"]] = record["check"]
    rows, counts = [], defaultdict(Counter)
    for item in items:
        check = checks.get(item["id"])
        if check is None:
            continue
        decision = "keep" if check["is_real_estate"] else "skip"
        counts[item["status"]][decision] += 1
        rows.append({"decision": decision, "status": item["status"], "title": item["title"][:200],
                     "reason": check["reason"], "source_id": item["source_id"], "url": item["url"], "id": item["id"]})
    # The cases a person should read first: approved but skipped, then rejected but skipped
    rows.sort(key=lambda r: (r["status"] != "approved" or r["decision"] != "skip", r["decision"] != "skip", r["status"]))
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    summary = {status: {"checked": sum(c.values()), "kept": c["keep"], "skipped": c["skip"],
                        "skipped_share": round(c["skip"] / max(sum(c.values()), 1), 3)}
               for status, c in counts.items()}
    summary["approved_but_skipped_titles"] = [r["title"] for r in rows if r["status"] == "approved" and r["decision"] == "skip"][:30]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--articles", type=Path, help="JSON export of processed_articles records")
    parser.add_argument("--from-db", action="store_true", help="read articles from DATABASE_URL (read-only)")
    parser.add_argument("--statuses", default="approved,rejected", help="with --from-db")
    parser.add_argument("--per-status", type=int, default=300, help="with --from-db: newest N per status")
    parser.add_argument("--limit", type=int, default=None, help="only the first N articles in total (a try-out)")
    parser.add_argument("--max-usd", type=float, default=0.30, help="stops starting checks at this cost")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if not args.articles and not args.from_db:
        parser.error("give --articles and/or --from-db")
    args.out.mkdir(parents=True, exist_ok=True)

    docs = json.loads(args.articles.read_text(encoding="utf-8")) if args.articles else []
    if args.from_db:
        docs += load_from_db([s.strip() for s in args.statuses.split(",") if s.strip()], args.per_status)
    items = list({i["id"]: i for i in (article_input(d) for d in docs) if i["title"] or i["text"]}.values())
    if args.limit:
        items = items[:args.limit]

    summary = {"run": asyncio.run(run_checks(items, args.out / CACHE, args.max_usd, args.concurrency))}
    summary.update(report(items, args.out / CACHE, args.out))
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nResults in {args.out} (results.csv lists approved-but-skipped first)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
