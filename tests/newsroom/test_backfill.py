"""Backfill command end to end on a throwaway database, with a fake name step (needs TEST_MONGO_URL)."""
import json
import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from src.configs.settings import settings
from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

ORG, LOC = "organisation", "location"
SIGNAL_ID = ObjectId()


def _article(article_id, status, title, content):
    return {"_id": article_id, "url": f"https://x.test/{article_id}", "status": status,
            "processed_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "final_output": {"title": title, "summary": title, "content": content}}


@pytest.fixture
def world(mongo_url, db_name, sync_db, monkeypatch, tmp_path):
    """Starting lists, three articles, one signal and a names cache; settings point at the throwaway database."""
    apply_plan(build_plan(), sync_db)
    sync_db["processed_articles"].insert_many([
        # Names from the cache
        _article("A1", "approved", "Emaar and Aldar team up on Yas Island",
                 "Emaar and Aldar will build in Abu Dhabi. Aldar bought a 30% stake. Brand New Co advised."),
        # Names from the (fake) AI step
        _article("A2", "approved", "DAMAC launches a tower", "DAMAC said sales were strong."),
        # Pending: never touched
        _article("A3", "processed", "Emaar pending story", "Emaar again."),
        # A reviewer removed Emaar before: must never come back
        _article("A4", "approved", "Emaar in Dubai", "Emaar opened a mall in Dubai."),
    ])
    emaar = seed_entity_id(ORG, "Emaar Properties")
    sync_db["entity_mentions"].insert_one({
        "_id": "m-removed", "entity_id": emaar, "source_type": "article", "source_id": "A4", "kind": ORG,
        "status": "removed", "link_source": "reviewer", "topics": [], "location_ids": []})
    sync_db["entity_suggestions"].insert_many([
        {"_id": "s-emaar", "source_type": "article", "source_id": "A4", "field": ORG, "name": "Emaar", "name_norm": "emaar",
         "entity_id": emaar, "entity_status": "active", "status": "dismissed", "in_new_names_queue": False},
        {"_id": "s-dubai", "source_type": "article", "source_id": "A4", "field": LOC, "name": "Dubai", "name_norm": "dubai",
         "entity_id": seed_entity_id(LOC, "Dubai"), "entity_status": "active", "status": "open", "in_new_names_queue": False},
    ])
    sync_db["signals"].insert_one({
        "_id": SIGNAL_ID, "status": "approved", "summary_en": "Nakheel launches villas on Palm Jebel Ali.",
        "entities_involved": ["Nakheel"], "areas_mentioned": ["Palm Jebel Ali"], "regions": ["United Arab Emirates"],
        "generated_at": datetime(2026, 3, 2, tzinfo=timezone.utc)})

    cache = tmp_path / "names.jsonl"
    cache.write_text(json.dumps({"article_id": "A1", "error": None, "names": {
        "companies": ["Emaar", "Aldar", "Stake", "Brand New Co"], "projects": ["Yas Island"], "places": ["Abu Dhabi"]}}) + "\n")

    import src.graph.nodes.extract_country as node
    calls = []

    async def fake_extract_country(state):
        calls.append(state.news_article.title)
        return state.model_copy(update={"newsroom_names": {"companies": ["DAMAC"], "projects": [], "places": []}})

    monkeypatch.setattr(node, "extract_country", fake_extract_country)
    for key, value in {"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_DB_NAME": db_name,
                       "NEWSROOM_SIGNALS_COLLECTION": "signals", "NEWSROOM_ENABLED": "true"}.items():
        monkeypatch.setattr(settings, key, value)

    def run(*extra):
        from src.scripts import newsroom_backfill as script
        out = tmp_path / "out"
        monkeypatch.setattr(sys, "argv", ["backfill", "--out", str(out), "--names-cache", str(cache), "--yes", *extra])
        assert script.main() == 0
        name = "undo_summary.json" if "--undo" in extra else "backfill_summary.json"
        return json.loads((out / name).read_text())

    run.calls, run.cache, run.out = calls, cache, tmp_path / "out"
    return run


def _links(sync_db, source_id, source="backfill"):
    return sorted(m["entity_id"] for m in sync_db["entity_mentions"].find(
        {"source_id": source_id, "status": "linked", "link_source": source}))


def _plan(world):
    import csv
    return {(r["source_id"], r["name"]): r for r in csv.DictReader(open(world.out / "backfill_plan.csv"))}


def test_dry_run_writes_nothing(world, sync_db):
    before = {c: sync_db[c].count_documents({}) for c in ("entity_mentions", "entity_suggestions", "entity_audit")}
    summary = world()
    assert summary["mode"] == "dry run: nothing was written"
    assert {c: sync_db[c].count_documents({}) for c in before} == before
    assert summary["article"]["approved_items"] == 3 and summary["signal"]["approved_items"] == 1
    assert (summary["article"]["from_cache"], summary["article"]["from_existing_suggestions"], summary["article"]["ai_called"]) == (1, 1, 1)

    plan = _plan(world)
    assert plan[("A1", "Emaar")]["action"] == "auto_link"
    assert plan[("A1", "Stake")]["action"] == "suggest_not_written_exactly"  # only "a 30% stake" in the text
    assert plan[("A1", "Brand New Co")]["action"] == "new_name"
    yas = plan[("A1", "Yas Island")]
    assert (yas["action"], yas["field"], yas["given_as"]) == ("auto_link", LOC, ORG)  # found in the other list
    assert plan[("A2", "DAMAC")]["action"] == "auto_link"
    assert plan[("A4", "Emaar")]["action"] == "already_linked_removed_or_decided"
    assert plan[("A4", "Dubai")]["action"] == "auto_link"
    assert plan[(str(SIGNAL_ID), "United Arab Emirates")]["action"] == "suggest_not_written_exactly"
    assert not any(source_id == "A3" for source_id, _ in plan)
    # The AI result was saved, so the next run does not pay for it again
    assert len(world.cache.read_text().splitlines()) == 2


def test_apply_links_only_safe_names_and_is_repeatable(world, sync_db):
    summary = world("--apply")
    assert summary["mode"] == "applied"
    assert _links(sync_db, "A1") == sorted([seed_entity_id(ORG, "Emaar Properties"), seed_entity_id(ORG, "Aldar Properties"),
                                            seed_entity_id(LOC, "Yas Island"), seed_entity_id(LOC, "Abu Dhabi")])
    assert _links(sync_db, "A2") == [seed_entity_id(ORG, "DAMAC Properties")]
    assert _links(sync_db, "A4") == [seed_entity_id(LOC, "Dubai")]
    assert _links(sync_db, str(SIGNAL_ID)) == sorted([seed_entity_id(ORG, "Nakheel"), seed_entity_id(LOC, "Palm Jebel Ali")])
    assert _links(sync_db, "A3") == []

    stake = sync_db["entity_suggestions"].find_one({"source_id": "A1", "name": "Stake"})
    assert stake["status"] == "open"  # a reviewer decides
    new_name = sync_db["entity_suggestions"].find_one({"source_id": "A1", "name": "Brand New Co"})
    assert new_name["in_new_names_queue"] is True  # approved item: goes to the New Names queue
    assert sync_db["entity_mentions"].find_one({"_id": "m-removed"})["status"] == "removed"
    assert sync_db["entity_suggestions"].find_one({"_id": "s-emaar"})["status"] == "dismissed"

    links_before = sync_db["entity_mentions"].count_documents({})
    again = world("--apply")
    assert again["applied"].get("auto_linked", 0) == 0
    assert sync_db["entity_mentions"].count_documents({}) == links_before
    assert len(world.calls) == 1  # the cached AI result was reused


def test_undo_removes_only_backfill_links(world, sync_db):
    world("--apply")
    reviewer_link = {"_id": "m-reviewer", "entity_id": seed_entity_id(ORG, "Nakheel"), "source_type": "article",
                     "source_id": "A2", "kind": ORG, "status": "linked", "link_source": "reviewer", "topics": [], "location_ids": []}
    sync_db["entity_mentions"].insert_one(reviewer_link)
    backfilled = sync_db["entity_mentions"].count_documents({"link_source": "backfill", "status": "linked"})

    dry = world("--undo")
    assert (dry["mode"], dry["backfill_links"]) == ("dry run: nothing was removed", backfilled)
    assert sync_db["entity_mentions"].count_documents({"link_source": "backfill"}) == backfilled

    done = world("--undo", "--apply")
    assert done["removed"] == backfilled
    assert sync_db["entity_mentions"].count_documents({"link_source": "backfill"}) == 0
    assert sync_db["entity_mentions"].find_one({"_id": "m-reviewer"})["status"] == "linked"
    assert sync_db["entity_mentions"].find_one({"_id": "m-removed"})["status"] == "removed"
    emaar = sync_db["entity_suggestions"].find_one({"source_id": "A1", "name": "Emaar"})
    assert (emaar["status"], emaar["decided_by"]) == ("open", None)  # back to a suggestion


def test_cost_cap_stops_the_ai_step(world):
    summary = world("--max-usd", "0")
    assert summary["article"]["no_names_cost_cap"] == 1 and summary["article"]["ai_called"] == 0
    assert world.calls == []


def test_ai_step_needs_the_newsroom_switch(world, monkeypatch):
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", "")
    with pytest.raises(SystemExit):
        world()
