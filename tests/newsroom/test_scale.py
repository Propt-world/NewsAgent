"""
Bulk paths at size (needs TEST_MONGO_URL): changes that touch many items work in batches,
give the same result as item-by-item, and send a bounded number of database commands.
The batch size is made tiny so batch edges are crossed.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.newsroom import entities as ent
from src.newsroom import links as lnk
from src.newsroom import locations
from src.newsroom.entities import Conflict, Invalid
from src.newsroom.models import EntityUpdate
from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

ORG, LOC = "organisation", "location"
NOW = datetime.now(timezone.utc)
UAE = seed_entity_id(LOC, "United Arab Emirates")
DUBAI = seed_entity_id(LOC, "Dubai")
ABU_DHABI = seed_entity_id(LOC, "Abu Dhabi")
CREEK = seed_entity_id(LOC, "Dubai Creek Harbour")
PALM = seed_entity_id(LOC, "Palm Jumeirah")
EMAAR = seed_entity_id(ORG, "Emaar Properties")
ITEMS = 40


def _mention(entity_id, kind, source_id, location_ids, status="linked"):
    return {"_id": str(uuid.uuid4()), "entity_id": entity_id, "kind": kind, "source_type": "article",
            "source_id": source_id, "status": status, "item_status": "approved", "title": source_id,
            "timestamp": NOW - timedelta(minutes=len(source_id)), "topics": [], "location_ids": location_ids}


@pytest.fixture
def many(api, sync_db, monkeypatch):
    """40 articles linked to Emaar and Dubai Creek Harbour; 5 linked only to Abu Dhabi."""
    monkeypatch.setattr(locations, "BATCH_SIZE", 7)
    apply_plan(build_plan(), sync_db)
    creek_path = sorted([UAE, DUBAI, CREEK])
    docs = []
    for n in range(ITEMS):
        docs += [_mention(CREEK, LOC, f"c{n}", creek_path), _mention(EMAAR, ORG, f"c{n}", creek_path)]
    for n in range(5):
        docs.append(_mention(ABU_DHABI, LOC, f"a{n}", sorted([UAE, ABU_DHABI])))
    sync_db["entity_mentions"].insert_many(docs)
    return sync_db


def _location_ids(sync_db, prefix):
    return {tuple(m["location_ids"]) for m in sync_db["entity_mentions"].find({"source_id": {"$regex": f"^{prefix}"}})}


def test_moving_a_place_recomputes_all_its_items_in_batches(many, run_counted):
    _, queries = run_counted(lambda db, sources: ent.update_entity(db, CREEK, EntityUpdate(parent_id=ABU_DHABI), "t"))
    assert _location_ids(many, "c") == {tuple(sorted([UAE, ABU_DHABI, CREEK]))}  # company links too
    assert _location_ids(many, "a") == {tuple(sorted([UAE, ABU_DHABI]))}
    assert queries <= 30, queries  # was 3 per item (120+ here)


def test_moving_a_city_rewrites_its_communities_paths(many, run_counted):
    run_counted(lambda db, sources: ent.update_entity(db, DUBAI, EntityUpdate(parent_id=None), "t"))
    assert many["entities"].find_one({"_id": CREEK})["path_ids"] == [DUBAI, CREEK]
    assert many["entities"].find_one({"_id": PALM})["path_ids"] == [DUBAI, PALM]
    assert _location_ids(many, "c") == {tuple(sorted([DUBAI, CREEK]))}


def test_merging_a_place_moves_its_links_in_batches(many, run_counted):
    many["entity_mentions"].insert_one(_mention(PALM, LOC, "c0", sorted([UAE, DUBAI, CREEK]), status="removed"))
    many["entity_suggestions"].insert_one({
        "_id": "sug-1", "source_type": "article", "source_id": "c1", "field": LOC, "name": "Creek",
        "name_norm": "creek", "entity_id": CREEK, "status": "open"})
    _, queries = run_counted(lambda db, sources: ent.merge_entities(db, CREEK, PALM, "t"))
    assert many["entity_mentions"].count_documents({"entity_id": CREEK}) == 0
    assert many["entity_mentions"].count_documents({"entity_id": PALM, "status": "linked"}) == ITEMS - 1
    # c0: the kept place's "removed" decision wins, so c0 has no place left
    assert many["entity_mentions"].find_one({"source_id": "c0", "entity_id": EMAAR})["location_ids"] == []
    assert many["entity_mentions"].find_one({"source_id": "c1", "entity_id": EMAAR})["location_ids"] == sorted([UAE, DUBAI, PALM])
    assert many["entity_suggestions"].find_one({"_id": "sug-1"})["status"] == "linked"
    assert queries <= 40, queries  # was 3+ per item


def test_deleting_a_place_recomputes_its_items_in_batches(many, run_counted):
    _, queries = run_counted(lambda db, sources: ent.delete_entity(db, CREEK, "t", "delete"))
    assert _location_ids(many, "c") == {()}
    assert many["entity_mentions"].count_documents({"source_id": {"$regex": "^c"}}) == ITEMS  # Emaar links stay
    assert queries <= 30, queries


# --- Bulk linking (New Names "Link", "Link all") ---------------------------------

STAGED = seed_entity_id(LOC, "Dubai Creek Harbour")  # switched off in the starting list


def _queued(sync_db, source_id, name="Big Name Co", field=ORG):
    sync_db["entity_suggestions"].insert_one({
        "_id": f"q-{source_id}-{field}", "source_type": "article", "source_id": source_id, "field": field,
        "name": name, "name_norm": name.lower(), "entity_id": None, "status": "open", "in_new_names_queue": True,
        "created_at": NOW})


@pytest.fixture
def queue(api, sync_db):
    """'Big Name Co' queued on 30 approved articles; n0 had Emaar removed by a reviewer, n1 already has it,
    n2 was deleted since. Every article is in Dubai."""
    apply_plan(build_plan(), sync_db)
    sync_db["processed_articles"].insert_many([
        {"_id": f"n{n}", "status": "approved", "final_output": {"title": f"Story {n}", "summary": "..."},
         "processed_at": NOW} for n in range(30) if n != 2])
    sync_db["entity_mentions"].insert_many(
        [_mention(DUBAI, LOC, f"n{n}", sorted([UAE, DUBAI])) for n in range(30)]
        + [_mention(EMAAR, ORG, "n0", sorted([UAE, DUBAI]), status="removed"),
           _mention(EMAAR, ORG, "n1", sorted([UAE, DUBAI]))])
    for n in range(30):
        _queued(sync_db, f"n{n}")
    return sync_db


def test_new_name_link_is_bulk_with_the_same_rules(queue, run_counted):
    count, queries = run_counted(lambda db, sources: lnk.link_new_name(db, sources, ORG, "big name co", EMAAR, "t"))
    assert count == 28  # 27 new + n1 already linked; n0 stays removed, n2 no longer exists
    linked = {m["source_id"]: m for m in queue["entity_mentions"].find({"entity_id": EMAAR})}
    assert linked["n0"]["status"] == "removed"
    assert linked["n5"]["status"] == "linked" and linked["n5"]["title"] == "Story 5"
    assert linked["n5"]["location_ids"] == sorted([UAE, DUBAI])  # the item's places are copied onto the new link
    assert "n2" not in linked
    by_item = {s["source_id"]: s for s in queue["entity_suggestions"].find()}
    assert by_item["n0"]["status"] == "dismissed"
    assert by_item["n5"]["status"] == "linked" and by_item["n5"]["entity_id"] == EMAAR
    assert by_item["n2"]["status"] == "open" and by_item["n2"]["in_new_names_queue"] is False
    assert queue["entity_audit"].count_documents({"action": "link", "entity_id": EMAAR}) == 27
    assert queries <= 25, queries  # was about 14 per item (400+ here)


def test_bulk_link_refuses_before_writing(queue, run_counted):
    with pytest.raises(Invalid):  # a company name cannot become a place
        run_counted(lambda db, sources: lnk.link_new_name(db, sources, ORG, "big name co", DUBAI, "t"))
    _queued(queue, "n3", name="Creek", field=LOC)
    with pytest.raises(Conflict):  # switched off, not asked to switch on
        run_counted(lambda db, sources: lnk.link_new_name(db, sources, LOC, "creek", STAGED, "t"))
    assert queue["entity_mentions"].count_documents({"entity_id": {"$in": [DUBAI, STAGED]}, "source_id": "n3"}) == 1
    assert queue["entity_audit"].count_documents({"action": "link"}) == 0

    count, _ = run_counted(lambda db, sources: lnk.link_new_name(db, sources, LOC, "creek", STAGED, "t", activate=True))
    assert count == 1
    assert queue["entities"].find_one({"_id": STAGED})["status"] == "active"
    assert queue["entity_mentions"].find_one({"source_id": "n3", "entity_id": EMAAR}) is None
    assert queue["entity_mentions"].find_one({"source_id": "n3", "entity_id": DUBAI})["location_ids"] == \
        sorted([UAE, DUBAI, STAGED])  # a new place link changes the item's places


def test_link_all_is_one_bulk_step(queue, run_counted):
    companies = [e["_id"] for e in queue["entities"].find({"kind": ORG, "status": "active"}).limit(10)]
    queue["entity_suggestions"].insert_many([{
        "_id": f"s{n}", "source_type": "article", "source_id": "n7", "field": ORG, "name": f"c{n}",
        "name_norm": f"c{n}", "entity_id": eid, "status": "open"} for n, eid in enumerate(companies)])
    result, queries = run_counted(lambda db, sources: lnk.link_all(db, sources, "article", "n7", "t"))
    assert {l["entity_id"] for l in result["links"]} >= set(companies)
    assert [s for s in result["suggestions"] if s["entity_id"]] == []
    assert queries <= 20, queries  # was about 14 per suggestion
