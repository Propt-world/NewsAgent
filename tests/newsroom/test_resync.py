"""The link repair (nightly job and script) fixes what missed an update (needs TEST_MONGO_URL)."""
import asyncio
from datetime import datetime, timezone

import pytest
from pymongo import AsyncMongoClient

from src.newsroom.db import ItemSources
from src.newsroom.resync import resync
from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

EMAAR = seed_entity_id("organisation", "Emaar Properties")
DUBAI = seed_entity_id("location", "Dubai")
UAE = seed_entity_id("location", "United Arab Emirates")
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _article(sync_db, article_id, **extra):
    doc = {"_id": article_id, "url": f"https://example.com/{article_id}", "status": "processed",
           "processed_at": NOW, "final_output": {"title": f"Story {article_id}", "summary": "s"}}
    doc.update(extra)
    sync_db["processed_articles"].insert_one(doc)


@pytest.fixture
def scene(api, sync_db):
    apply_plan(build_plan(), sync_db)
    for n in (1, 2, 3):
        _article(sync_db, f"art-{n}")
    _article(sync_db, "composed", url="newsroom://compose/run-1", source_id="newsroom_compose",
             final_output={"title": "Emaar and DAMAC in Dubai", "summary": "s"})
    for item in ("art-1", "art-2", "art-3", "composed"):
        assert api.post(f"/newsroom/items/article/{item}/links", json={"entity_id": EMAAR}).status_code == 200
    assert api.post("/newsroom/items/article/art-1/links", json={"entity_id": DUBAI}).status_code == 200
    return api


def _run(mongo_url, db_name, apply):
    async def go():
        client = AsyncMongoClient(mongo_url)
        try:
            db = client[db_name]
            sources = ItemSources(articles=db["processed_articles"], signals=db["signals"],
                                  archived=db["archived_articles"], deleted=db["deleted_articles"])
            return await resync(db, sources, apply=apply)
        finally:
            await client.close()
    return asyncio.run(go())


def _mention(sync_db, item, entity=EMAAR):
    return sync_db["entity_mentions"].find_one({"source_id": item, "entity_id": entity})


def test_repair_fixes_what_missed_an_update_and_is_then_quiet(scene, sync_db, mongo_url, db_name):
    # Changes the Newsroom never heard about:
    sync_db["processed_articles"].update_one({"_id": "art-1"}, {"$set": {
        "status": "approved", "final_output.title": "Edited elsewhere"}})
    archived = sync_db["processed_articles"].find_one_and_delete({"_id": "art-2"})
    sync_db["archived_articles"].insert_one(archived)
    sync_db["processed_articles"].delete_one({"_id": "art-3"})  # gone entirely
    sync_db["entity_mentions"].update_many({"source_id": "art-1"}, {"$set": {"location_ids": ["wrong"]}})
    # A composed article linked before public links existed:
    sync_db["entity_mentions"].update_many({"source_id": "composed"}, {"$set": {"url": "newsroom://compose/run-1"}})
    before = list(sync_db["entity_mentions"].find())

    dry = _run(mongo_url, db_name, apply=False)
    assert dry["items_fixed"] == 4 and dry["items_gone"] == 1
    assert (dry["status"], dry["title_or_text"], dry["places"], dry["link"]) == (2, 1, 1, 1)
    assert list(sync_db["entity_mentions"].find()) == before  # a dry run writes nothing

    done = _run(mongo_url, db_name, apply=True)
    assert done == dry
    m1 = _mention(sync_db, "art-1")
    assert (m1["item_status"], m1["title"]) == ("approved", "Edited elsewhere")
    assert set(m1["location_ids"]) == {DUBAI, UAE}
    assert _mention(sync_db, "art-2")["item_status"] == "archived"
    assert _mention(sync_db, "art-3")["item_status"] == "missing"
    assert _mention(sync_db, "composed")["url"] == "https://propt.global/news/emaar-and-damac-in-dubai/composed"

    assert _run(mongo_url, db_name, apply=True)["items_fixed"] == 0  # nothing left to fix


def test_repair_keeps_the_new_names_queue_in_step_with_approval(scene, sync_db, mongo_url, db_name):
    _article(sync_db, "art-9")
    scene.post("/newsroom/suggest", json={"source_type": "article", "source_id": "art-9",
                                          "organisations": ["Brand New Developer"], "locations": []})
    sync_db["processed_articles"].update_one({"_id": "art-9"}, {"$set": {"status": "approved"}})  # missed
    assert not sync_db["entity_suggestions"].find_one({"source_id": "art-9"})["in_new_names_queue"]

    assert _run(mongo_url, db_name, apply=True)["items_fixed"] >= 1
    assert sync_db["entity_suggestions"].find_one({"source_id": "art-9"})["in_new_names_queue"] is True


def test_repair_copies_topics_onto_links(scene, sync_db, mongo_url, db_name):
    """Links made before topics were copied (or a missed signal tagging) get them from the nightly repair."""
    sync_db["processed_articles"].update_one(
        {"_id": "art-1"}, {"$set": {"final_output.category": ["Off-Plan & New Launches", "Developer News"]}})
    assert _mention(sync_db, "art-1")["topics"] == []

    dry = _run(mongo_url, db_name, apply=False)
    assert (dry["items_fixed"], dry["topics"]) == (1, 1)
    _run(mongo_url, db_name, apply=True)
    assert _mention(sync_db, "art-1")["topics"] == ["Off-Plan & New Launches", "Developer News"]
    assert _mention(sync_db, "art-1", DUBAI)["topics"] == ["Off-Plan & New Launches", "Developer News"]
    assert _run(mongo_url, db_name, apply=False)["items_fixed"] == 0
