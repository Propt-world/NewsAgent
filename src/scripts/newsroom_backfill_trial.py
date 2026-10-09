"""
Backfill trial: what the step-8 backfill would link, measured on the labelled
articles. Read-only: it uses no database and writes only report files to --out.

Pass 1 (free): the workbook's names (labelled by Claude) through the matcher.
Pass 2 (optional, paid, capped): the real name step (gpt-4o-mini, the
country_extraction prompts from init_db.py) on a repeatable sample, compared
with pass 1. Results are cached in --out, so a re-run pays only for new articles.

    python src/scripts/newsroom_backfill_trial.py --articles approved_articles.json \
        --labels golden_labels.csv --out trial
    NEWSROOM_ENABLED=true python src/scripts/newsroom_backfill_trial.py ... \
        --model-sample 50 --max-usd 0.10
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.newsroom import backfill_trial as bt
from src.newsroom.seed import SEED_DIR, build_plan

MODEL_CACHE = "model_names.jsonl"


def write_csv(path: Path, rows: list) -> None:
    if not rows:
        path.write_text("")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def labels_pass(index, labels, articles, out: Path) -> dict:
    name_rows, link_rows = [], []
    per_entity = defaultdict(lambda: {"links": 0, bt.EXACT_CASE: 0, bt.OTHER_CASE: 0, bt.ABSENT: 0, "examples": []})
    outcomes, unknown, other_list = Counter(), Counter(), Counter()
    with_links = 0
    for row in labels:
        article_id, title = row["Article ID"].strip(), row.get("Article Title") or ""
        text = bt.article_text(articles.get(article_id, {}))
        results = bt.resolve_item(index, *bt.label_names(row))
        for r in results:
            outcomes[r.outcome] += 1
            if r.outcome == "unknown":
                unknown[(r.field, r.name)] += 1
            if r.given_as:
                other_list[(r.given_as, r.name, r.entity_name)] += 1
            name_rows.append({"article_id": article_id, "title": title, **asdict(r)})
        links = bt.auto_links(results)
        with_links += bool(links)
        for link in links.values():
            row_out = bt.result_row(article_id, title, link, text)
            link_rows.append(row_out)
            stats = per_entity[link.entity_name]
            stats["links"] += 1
            stats[row_out["in_text"]] += 1
            if row_out["in_text"] != bt.EXACT_CASE and len(stats["examples"]) < 3:
                stats["examples"].append(title[:90])

    entity_rows = [
        {"entity": name, "would_link": s["links"], "name_in_text_exact_case": s[bt.EXACT_CASE],
         "only_other_case": s[bt.OTHER_CASE], "not_in_text": s[bt.ABSENT],
         "share_not_exact": round((s["links"] - s[bt.EXACT_CASE]) / s["links"], 2),
         "examples_not_exact": " | ".join(s["examples"])}
        for name, s in sorted(per_entity.items(), key=lambda kv: -kv[1]["links"])
    ]
    write_csv(out / "labels_names.csv", name_rows)
    write_csv(out / "labels_links.csv", link_rows)
    write_csv(out / "labels_entities.csv", entity_rows)
    in_text = Counter(r["in_text"] for r in link_rows)
    return {
        "articles": len(labels),
        "articles_missing_from_export": sum(1 for r in labels if r["Article ID"].strip() not in articles),
        "articles_with_a_link": with_links,
        "links": len(link_rows),
        "links_name_in_text_exact_case": in_text[bt.EXACT_CASE],
        "links_name_only_in_other_case": in_text[bt.OTHER_CASE],
        "links_name_not_in_text": in_text[bt.ABSENT],
        "name_outcomes": dict(outcomes),
        "top_unknown_names": [f"{f}: {n} ({c})" for (f, n), c in unknown.most_common(25)],
        "found_in_the_other_list": [f"{f}: {n} -> {m} ({c})" for (f, n, m), c in other_list.most_common(25)],
    }


def init_db_prompts():
    """The prompts init_db.py installs (what the pipeline uses after `init_db.py --mode upsert`)."""
    from src.graph.nodes.load_agent_configuration import REQUIRED_PROMPTS
    from src.models.AgentPromptsModel import AgentPromptsModel
    from src.scripts.init_db import INITIAL_DATA
    texts = {p["name"]: p["content"] for p in INITIAL_DATA if p["name"] in REQUIRED_PROMPTS}
    return AgentPromptsModel(**texts)


async def run_model(ids, articles, cache_path: Path, max_usd: float, concurrency: int) -> dict:
    """The real extract_country step on articles not yet in the cache, until max_usd is reached."""
    from langchain_community.callbacks.manager import get_openai_callback
    from src.configs.settings import settings
    from src.graph.nodes.extract_country import extract_country
    from src.models.ArticleModel import ArticleModel
    from src.models.MainWorkflowState import MainWorkflowState

    if not settings.newsroom_enabled:
        raise SystemExit("Pass 2 needs NEWSROOM_ENABLED=true (the name step only returns names when it is on).")
    prompts = init_db_prompts()
    cached = {json.loads(line)["article_id"] for line in cache_path.read_text().splitlines()} if cache_path.exists() else set()
    todo = [i for i in ids if i not in cached and i in articles]
    stats = {"already_cached": len(ids) - len(todo), "called": 0, "failed": 0, "stopped_by_cap": False, "cost_usd": 0.0}
    semaphore = asyncio.Semaphore(concurrency)

    with get_openai_callback() as usage, open(cache_path, "a", encoding="utf-8") as cache:
        async def one(article_id: str) -> None:
            async with semaphore:
                if usage.total_cost >= max_usd:
                    stats["stopped_by_cap"] = True
                    return
                final = articles[article_id].get("final_output") or {}
                content = str(final.get("content") or "")
                state = MainWorkflowState(
                    source_url=articles[article_id].get("url") or article_id,
                    cleaned_article_text=content,
                    news_article=ArticleModel(title=str(final.get("title") or ""), content=content,
                                              summary=str(final.get("summary") or "")),
                    active_prompts=prompts,
                )
                result = await extract_country(state)
                stats["called"] += 1
                record = {"article_id": article_id, "names": result.newsroom_names, "error": result.error_message}
                stats["failed"] += bool(result.error_message)
                cache.write(json.dumps(record) + "\n")
                cache.flush()

        await asyncio.gather(*(one(i) for i in todo))
        stats["cost_usd"] = round(float(usage.total_cost), 4)
    return stats


def model_pass(index, labels, articles, ids, out: Path) -> dict:
    cache_path = out / MODEL_CACHE
    records = {}
    if cache_path.exists():
        for line in cache_path.read_text().splitlines():
            record = json.loads(line)
            records[record["article_id"]] = record
    by_id = {r["Article ID"].strip(): r for r in labels}
    rows, counts, model_only_in_text = [], Counter(), Counter()
    compared = 0
    for article_id in ids:
        record = records.get(article_id)
        if record is None or record.get("error"):
            continue
        compared += 1
        label_row = by_id[article_id]
        title = label_row.get("Article Title") or ""
        text = bt.article_text(articles.get(article_id, {}))
        model_links = bt.auto_links(bt.resolve_item(index, *bt.model_names(record.get("names"))))
        label_links = bt.auto_links(bt.resolve_item(index, *bt.label_names(label_row)))
        comparison = bt.compare(model_links, label_links)
        for side, entity_ids, source in (("agree", comparison.agree, model_links),
                                         ("model_only", comparison.model_only, model_links),
                                         ("labels_only", comparison.labels_only, label_links)):
            for entity_id in entity_ids:
                row = bt.result_row(article_id, title, source[entity_id], text, side=side)
                rows.append(row)
                counts[side] += 1
                if side == "model_only":
                    model_only_in_text[row["in_text"]] += 1
    rows.sort(key=lambda r: (r["side"] == "agree", r["side"], r["entity_name"] or ""))
    write_csv(out / "model_comparison.csv", rows)
    model_links_total = counts["agree"] + counts["model_only"]
    return {
        "articles_compared": compared,
        "links_both_agree": counts["agree"],
        "links_only_model": counts["model_only"],
        "links_only_labels": counts["labels_only"],
        "model_links_confirmed_by_labels": round(counts["agree"] / model_links_total, 3) if model_links_total else None,
        "model_only_name_in_text": dict(model_only_in_text),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--articles", type=Path, required=True, help="approved_articles.json (export of processed_articles)")
    parser.add_argument("--labels", type=Path, required=True, help="golden_labels.csv (the workbook's Classified Articles sheet)")
    parser.add_argument("--out", type=Path, required=True, help="folder for the report files")
    parser.add_argument("--seed-dir", type=Path, default=SEED_DIR, help="starting lists (default: the repo copy)")
    parser.add_argument("--model-sample", type=int, default=0, help="articles for pass 2 (0 = skip; paid)")
    parser.add_argument("--sample-seed", type=int, default=8, help="same seed = same sample (re-runs reuse the cache)")
    parser.add_argument("--max-usd", type=float, default=0.10, help="pass 2 stops starting calls at this cost")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    plan = build_plan(args.seed_dir)
    if plan.errors:
        print("The starting lists have errors; fix them first:\n  " + "\n  ".join(plan.errors))
        return 1
    index = bt.ListIndex.from_plan(plan)
    articles = {a["_id"]: a for a in json.loads(args.articles.read_text(encoding="utf-8"))}
    labels = bt.read_labels(args.labels)
    args.out.mkdir(parents=True, exist_ok=True)

    summary = {"labels_pass": labels_pass(index, labels, articles, args.out)}
    if args.model_sample > 0:
        ids = bt.sample_ids([r["Article ID"].strip() for r in labels], args.model_sample, args.sample_seed)
        summary["model_run"] = asyncio.run(run_model(ids, articles, args.out / MODEL_CACHE, args.max_usd, args.concurrency))
        summary["model_pass"] = model_pass(index, labels, articles, ids, args.out)

    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nReports in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
