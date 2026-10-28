"""
Country check (handoff 27.1 D3): do the countries stay the same with the Newsroom's prompt change?

The Newsroom added rules 7-11 (company/project/place names) to country_extraction_system and,
when NEWSROOM_ENABLED is on, the country step also returns those names. This runs the real
country step twice on the same approved articles and compares the countries:
  old = rules 1-6 with the Newsroom off (what production runs today)
  new = rules 1-11 with the Newsroom on (what dev/production will run)
Read-only: it never writes to the database; it writes one CSV to --out. Paid (OpenAI, the
pipeline's model), stopped at --max-usd (about 200 small calls for 100 articles).

    python src/scripts/newsroom_country_check.py --out country_check --sample 100 --max-usd 0.20
"""
import argparse
import asyncio
import csv
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from pymongo import AsyncMongoClient

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings
from src.scheduler.db import _database_target, _mongo_client_options
from src.utils import tracing

NAMES_MARKER = "NAMES (only when the response has the fields companies, projects and places):"


def prompts_for(phase: str):
    """The code's prompts; 'old' drops the Newsroom's rules 7-11 from the country prompt."""
    from src.newsroom.backfill import init_db_prompts
    prompts = init_db_prompts()
    if phase == "old":
        system = prompts.country_extraction_system
        prompts = prompts.model_copy(update={"country_extraction_system": system[:system.index(NAMES_MARKER)].rstrip()})
    return prompts


async def countries_for(article: dict, prompts) -> List[str]:
    from src.graph.nodes import extract_country as node
    from src.models.ArticleModel import ArticleModel
    from src.models.MainWorkflowState import MainWorkflowState

    final = article.get("final_output") or {}
    content = str(final.get("cleaned_article_text") or final.get("content") or "")
    state = MainWorkflowState(
        source_url=article.get("url") or str(article.get("_id")),
        cleaned_article_text=content,
        news_article=ArticleModel(title=str(final.get("title") or ""), content=content, summary=str(final.get("summary") or "")),
        active_prompts=prompts,
    )
    result = await node.extract_country(state)
    if result.error_message:
        raise RuntimeError(result.error_message)
    return list(result.news_article.countries or [])


async def run_phase(phase: str, articles: List[dict], usage, max_usd: float, concurrency: int) -> Dict[str, dict]:
    """One phase on every article (until the money cap). The Newsroom switch is set for the whole phase."""
    settings.NEWSROOM_ENABLED = "true" if phase == "new" else "false"
    prompts = prompts_for(phase)
    semaphore = asyncio.Semaphore(concurrency)
    results: Dict[str, dict] = {}

    async def one(article: dict) -> None:
        async with semaphore:
            if usage.total_cost >= max_usd:
                return
            try:
                with tracing.job_span(f"country_check_{phase}", tags=["country_check", "trial"],
                                      metadata={"article_id": article["_id"], "phase": phase}):  # one trace per article
                    results[article["_id"]] = {"countries": await countries_for(article, prompts)}
            except Exception as e:
                results[article["_id"]] = {"error": str(e)[:200]}

    await asyncio.gather(*(one(a) for a in articles))
    return results


def compare(articles: List[dict], old: Dict[str, dict], new: Dict[str, dict]) -> List[dict]:
    rows = []
    for a in articles:
        o, n = old.get(a["_id"]), new.get(a["_id"])
        if o is None or n is None:
            outcome = "not run (money cap reached)"
        elif o.get("error") or n.get("error"):
            outcome = "error"
        else:
            outcome = "same" if sorted(o["countries"]) == sorted(n["countries"]) else "DIFFERENT"
        rows.append({
            "article_id": a["_id"],
            "title": (a.get("final_output") or {}).get("title") or "",
            "stored_countries": " | ".join((a.get("final_output") or {}).get("countries") or []),
            "old_countries": " | ".join((o or {}).get("countries") or []),
            "new_countries": " | ".join((n or {}).get("countries") or []),
            "outcome": outcome,
            "error": (o or {}).get("error") or (n or {}).get("error") or "",
        })
    return rows


async def main_async(args) -> List[dict]:
    from langchain_community.callbacks.manager import get_openai_callback

    client = AsyncMongoClient(settings.DATABASE_URL, **_mongo_client_options())
    try:
        cursor = client[settings.MONGO_DB_NAME]["processed_articles"].find(
            {"status": "approved", "final_output.summary": {"$nin": [None, ""]}}).sort("processed_at", -1).limit(args.sample)
        articles = [doc async for doc in cursor]
    finally:
        await client.close()
    print(f"{len(articles)} approved articles (newest first). Running the country step twice on each...")
    with get_openai_callback() as usage:
        old = await run_phase("old", articles, usage, args.max_usd, args.concurrency)
        new = await run_phase("new", articles, usage, args.max_usd, args.concurrency)
        print(f"Cost: ${usage.total_cost:.4f} ({usage.total_tokens} tokens)")
    return compare(articles, old, new)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="folder for country_check.csv")
    parser.add_argument("--sample", type=int, default=100, help="newest N approved articles")
    parser.add_argument("--max-usd", type=float, default=0.20, help="stop starting calls at this cost")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    tracing.init_tracing()  # Phoenix, when PHOENIX_COLLECTOR_ENDPOINT is set
    print(f"--- Database: {_database_target()} / {settings.MONGO_DB_NAME} | read-only | model {settings.MODEL_NAME} ---")
    rows = asyncio.run(main_async(args))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "country_check.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["article_id"])
        writer.writeheader()
        writer.writerows(rows)
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print("Results:", counts)
    print(f"Every article, with the stored, old and new countries: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
