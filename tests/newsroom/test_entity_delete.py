"""Deleting entities (needs TEST_MONGO_URL)."""
from datetime import datetime, timezone

import pytest

from src.newsroom.seed import apply_plan, build_plan, compare_with_db, seed_entity_id

ORG, LOC = "organisation", "location"


def _id(kind, name):
    return seed_entity_id(kind, name)


def _article(sync_db, article_id):
    sync_db["processed_articles"].insert_one({
        "_id": article_id,
        "url": f"https://example.com/{article_id}",
        "status": "processed",
        "processed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "final_output": {"title": "A story", "summary": "...", "published_date": None},
    })
    return f"/newsroom/items/article/{article_id}"


@pytest.fixture
def seeded(api, sync_db):
    apply_plan(build_plan(), sync_db)
    return api


def _create(api, **body):
    resp = api.post("/newsroom/entities", json={"kind": ORG, "type": "developer", "status": "active", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _delete(api, entity_id, confirm="delete", dry_run=False):
    return api.post(f"/newsroom/entities/{entity_id}/delete", json={"confirm": confirm, "dry_run": dry_run})


def test_delete_needs_the_word(seeded):
    entity = _create(seeded, canonical_name="Nshama")
    for word in (None, "", "yes", "Nshama"):
        resp = _delete(seeded, entity["id"], confirm=word)
        assert resp.status_code == 422 and 'Type "delete"' in resp.json()["detail"]
    assert seeded.get(f"/newsroom/entities/{entity['id']}").status_code == 200
    assert _delete(seeded, entity["id"], confirm=" DELETE ").status_code == 200
    assert seeded.get(f"/newsroom/entities/{entity['id']}").status_code == 404


def test_delete_removes_names_and_links_but_not_items(seeded, sync_db):
    entity = _create(seeded, canonical_name="Nshama", aliases=["Nshama Developments"])
    linked, removed = _article(sync_db, "art-1"), _article(sync_db, "art-2")
    for item in (linked, removed):
        assert seeded.post(f"{item}/links", json={"entity_id": entity["id"]}).status_code == 200
    assert seeded.delete(f"{removed}/links/{entity['id']}").status_code == 200
    seeded.post("/newsroom/suggest", json={"source_type": "article", "source_id": "art-3",
                                            "organisations": ["Nshama Developments"], "locations": []})

    preview = _delete(seeded, entity["id"], confirm=None, dry_run=True).json()
    assert (preview["aliases_removed"], preview["links_removed"], preview["decisions_removed"],
            preview["suggestions_reset"]) == (2, 1, 1, 1)
    assert seeded.get(f"/newsroom/entities/{entity['id']}").status_code == 200  # dry run wrote nothing

    assert _delete(seeded, entity["id"]).status_code == 200
    assert sync_db["entity_aliases"].count_documents({"entity_id": entity["id"]}) == 0
    assert sync_db["entity_mentions"].count_documents({"entity_id": entity["id"]}) == 0
    assert sync_db["processed_articles"].count_documents({}) == 2  # articles untouched
    suggestion = sync_db["entity_suggestions"].find_one({"source_id": "art-3"})
    assert (suggestion["entity_id"], suggestion["status"]) == (None, "open")
    assert suggestion["reason"] == "not in the list (entry was deleted)"

    record = sync_db["entity_audit"].find_one({"entity_id": entity["id"], "action": "delete"})
    assert record["actor"] == "tester@propt.global"
    assert record["before"]["canonical_name"] == "Nshama"
    assert sorted(a["alias"] for a in record["before"]["aliases"]) == ["Nshama", "Nshama Developments"]

    # The name can be added again on purpose.
    assert _create(seeded, canonical_name="Nshama")["id"] != entity["id"]


def test_deleting_a_place_recomputes_items_places(seeded, sync_db):
    dubai = _id(LOC, "Dubai")
    place = _create(seeded, kind=LOC, type="community", canonical_name="Creek Harbour", parent_id=dubai)
    item = _article(sync_db, "art-1")
    assert seeded.post(f"{item}/links", json={"entity_id": _id(ORG, "Emaar Properties")}).status_code == 200
    assert seeded.post(f"{item}/links", json={"entity_id": place["id"]}).status_code == 200
    assert dubai in sync_db["entity_mentions"].find_one({"source_id": "art-1"})["location_ids"]

    assert _delete(seeded, place["id"]).status_code == 200
    assert sync_db["entity_mentions"].find_one({"source_id": "art-1"})["location_ids"] == []


def test_delete_refused_while_others_depend_on_it(seeded):
    resp = _delete(seeded, _id(ORG, "Dubai Holding"))  # Meraas sits under it
    assert resp.status_code == 422 and "inside" in resp.json()["detail"]
    resp = _delete(seeded, _id(LOC, "United Arab Emirates"), dry_run=True)
    assert resp.status_code == 422  # places inside it, and companies use it as their country

    country = _create(seeded, kind=LOC, type="country", canonical_name="Testland")
    _create(seeded, canonical_name="Nshama", country_id=country["id"])
    resp = _delete(seeded, country["id"])
    assert resp.status_code == 422 and "country" in resp.json()["detail"]


def test_merged_record_cannot_be_deleted(seeded):
    assert seeded.post(f"/newsroom/entities/{_id(ORG, 'Deyaar')}/merge",
                       json={"into_id": _id(ORG, "Arada")}).status_code == 200
    assert _delete(seeded, _id(ORG, "Deyaar")).status_code == 422


def test_seed_never_brings_a_deleted_entity_back(seeded, sync_db):
    deyaar = _id(ORG, "Deyaar")
    assert _delete(seeded, deyaar).status_code == 200
    assert compare_with_db(build_plan(), sync_db)["entities_deleted"] == 1
    result = apply_plan(build_plan(), sync_db)
    assert (result["entities_deleted"], result["entities_inserted"], result["aliases_inserted"]) == (1, 0, 0)
    assert sync_db["entities"].find_one({"_id": deyaar}) is None
    assert sync_db["entity_aliases"].count_documents({"entity_id": deyaar}) == 0
