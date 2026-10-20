"""Merging duplicate entities (needs TEST_MONGO_URL)."""
from datetime import datetime, timezone

import pytest

from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

ORG, LOC = "organisation", "location"


def _id(kind, name):
    return seed_entity_id(kind, name)


def _article(sync_db, article_id, title="A story"):
    sync_db["processed_articles"].insert_one({
        "_id": article_id,
        "url": f"https://example.com/{article_id}",
        "status": "processed",
        "processed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "final_output": {"title": title, "summary": "...", "published_date": None},
    })
    return f"/newsroom/items/article/{article_id}"


@pytest.fixture
def seeded(api, sync_db):
    apply_plan(build_plan(), sync_db)
    return api


def _create(api, **body):
    payload = {"kind": ORG, "type": "developer", "status": "active", **body}
    resp = api.post("/newsroom/entities", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _merge(api, entity_id, into_id, dry_run=False):
    return api.post(f"/newsroom/entities/{entity_id}/merge", json={"into_id": into_id, "dry_run": dry_run})


def _link(api, item, entity_id):
    resp = api.post(f"{item}/links", json={"entity_id": entity_id})
    assert resp.status_code == 200, resp.text


def _links(api, item):
    return {l["entity_id"] for l in api.get(f"{item}/entities").json()["links"]}


def test_merge_moves_names_links_and_suggestions(seeded, sync_db):
    keep = _create(seeded, canonical_name="Nshama", ambiguous_aliases=["Nshama Group"])
    dup = _create(seeded, canonical_name="Nshama Developments LLC", aliases=["Nshama Developments"],
                  ambiguous_aliases=["Nshama Group"])  # ambiguous names may repeat; the kept one's copy stays
    item = _article(sync_db, "art-1")
    _link(seeded, item, dup["id"])
    seeded.post("/newsroom/suggest", json={"source_type": "article", "source_id": "art-2-none",
                                            "organisations": ["Nshama Developments"], "locations": []})

    preview = _merge(seeded, dup["id"], keep["id"], dry_run=True)
    assert preview.status_code == 200, preview.text
    counts = preview.json()
    assert (counts["aliases_moved"], counts["aliases_already_there"]) == (2, 1)
    assert (counts["links_moved"], counts["suggestions_moved"]) == (1, 1)
    assert seeded.get(f"/newsroom/entities/{dup['id']}").json()["status"] == "active"  # dry run wrote nothing

    resp = _merge(seeded, dup["id"], keep["id"])
    assert resp.status_code == 200, resp.text
    kept = resp.json()["entity"]
    assert sorted(a["alias"] for a in kept["aliases"]) == ["Nshama", "Nshama Developments", "Nshama Developments LLC", "Nshama Group"]
    assert _links(seeded, item) == {keep["id"]}
    suggestion = sync_db["entity_suggestions"].find_one({"source_id": "art-2-none"})
    assert suggestion["entity_id"] == keep["id"]

    merged = seeded.get(f"/newsroom/entities/{dup['id']}").json()
    assert (merged["status"], merged["merged_into"], merged["aliases"]) == ("merged", keep["id"], [])
    listed = [e["id"] for e in seeded.get("/newsroom/entities", params={"q": "nshama"}).json()["items"]]
    assert listed == [keep["id"]]
    only_merged = seeded.get("/newsroom/entities", params={"status": "merged"}).json()["items"]
    assert [e["id"] for e in only_merged] == [dup["id"]]

    actions = [a["action"] for a in seeded.get(f"/newsroom/entities/{keep['id']}/audit").json()]
    assert "merge_from" in actions
    assert seeded.get(f"/newsroom/entities/{dup['id']}/audit").json()[0]["action"] == "merge_into"

    # New text with the duplicate's name now suggests the kept entity.
    seeded.post("/newsroom/suggest", json={"source_type": "article", "source_id": "art-3-none",
                                            "organisations": ["Nshama Developments LLC"], "locations": []})
    assert sync_db["entity_suggestions"].find_one({"source_id": "art-3-none"})["entity_id"] == keep["id"]


def test_kept_entitys_decision_wins_on_the_same_item(seeded, sync_db):
    keep = _create(seeded, canonical_name="Nshama")
    dup = _create(seeded, canonical_name="Nshama Developments LLC")
    item = _article(sync_db, "art-1")
    _link(seeded, item, keep["id"])
    assert seeded.delete(f"{item}/links/{keep['id']}").status_code == 200  # reviewer removed it
    _link(seeded, item, dup["id"])

    counts = _merge(seeded, dup["id"], keep["id"]).json()
    assert (counts["links_moved"], counts["links_both_decided"]) == (0, 1)
    assert _links(seeded, item) == set()  # the removal is never undone
    assert sync_db["entity_mentions"].count_documents({"source_id": "art-1"}) == 1


def test_open_suggestion_is_closed_where_the_kept_entity_is_linked(seeded, sync_db):
    keep = _create(seeded, canonical_name="Nshama")
    dup = _create(seeded, canonical_name="Nshama Developments LLC")
    item = _article(sync_db, "art-1")
    _link(seeded, item, keep["id"])
    seeded.post("/newsroom/suggest", json={"source_type": "article", "source_id": "art-1",
                                            "organisations": ["Nshama Developments LLC"], "locations": []})
    assert _merge(seeded, dup["id"], keep["id"]).status_code == 200
    assert seeded.get(f"{item}/entities").json()["suggestions"] == []


def test_place_merge_moves_children_and_recomputes_places(seeded, sync_db):
    dubai, keep = _id(LOC, "Dubai"), _id(LOC, "Dubai Creek Harbour")
    dup = _create(seeded, kind=LOC, type="community", canonical_name="Creek Harbour", parent_id=dubai)
    child = _create(seeded, kind=LOC, type="project", canonical_name="Creek Tower Plot", parent_id=dup["id"])
    on_dup, on_child = _article(sync_db, "art-1"), _article(sync_db, "art-2")
    _link(seeded, on_dup, dup["id"])
    _link(seeded, on_child, child["id"])

    counts = _merge(seeded, dup["id"], keep).json()
    assert (counts["children_moved"], counts["links_moved"]) == (1, 1)

    keep_path = seeded.get(f"/newsroom/entities/{keep}").json()["path_ids"]
    moved = seeded.get(f"/newsroom/entities/{child['id']}").json()
    assert (moved["parent_id"], moved["path_ids"]) == (keep, keep_path + [child["id"]])
    for article_id in ("art-1", "art-2"):
        location_ids = sync_db["entity_mentions"].find_one({"source_id": article_id})["location_ids"]
        assert keep in location_ids and dubai in location_ids and dup["id"] not in location_ids


def test_country_merge_moves_companies_country(seeded):
    uae = _id(LOC, "United Arab Emirates")
    dup = _create(seeded, kind=LOC, type="country", canonical_name="U.A.E. (duplicate)")
    company = _create(seeded, canonical_name="Nshama", country_id=dup["id"])

    refused = _merge(seeded, dup["id"], _id(LOC, "Dubai"))
    assert refused.status_code == 422 and "not a country" in refused.json()["detail"]

    assert _merge(seeded, dup["id"], uae).json()["countries_moved"] == 1
    assert seeded.get(f"/newsroom/entities/{company['id']}").json()["country_id"] == uae


@pytest.mark.parametrize("pair, code", [
    (("Emaar Properties", "Emaar Properties"), 422),          # same entity
    (("Meraas", "Dubai Holding"), 422),                       # child into parent
    (("Dubai Holding", "Meraas"), 422),                       # parent into child
])
def test_merge_refusals(seeded, pair, code):
    assert _merge(seeded, _id(ORG, pair[0]), _id(ORG, pair[1])).status_code == code


def test_cannot_merge_company_and_place(seeded):
    resp = _merge(seeded, _id(ORG, "Nakheel"), _id(LOC, "Dubai"))
    assert resp.status_code == 422 and "place" in resp.json()["detail"]


def test_merged_entity_is_frozen(seeded, sync_db):
    keep, dup = _id(ORG, "Arada"), _id(ORG, "Deyaar")
    assert _merge(seeded, dup, keep).status_code == 200

    assert seeded.patch(f"/newsroom/entities/{dup}", json={"status": "active"}).status_code == 422
    assert seeded.post(f"/newsroom/entities/{dup}/aliases", json={"alias": "Deyaar PJSC"}).status_code == 422
    item = _article(sync_db, "art-1")
    assert seeded.post(f"{item}/links", json={"entity_id": dup}).status_code == 422
    assert _merge(seeded, keep, dup).status_code == 422                     # into a merged entity
    assert _merge(seeded, dup, _id(ORG, "Nakheel")).status_code == 409      # already merged elsewhere
    assert seeded.post("/newsroom/entities", json={
        "kind": ORG, "type": "developer", "canonical_name": "X", "status": "merged"}).status_code == 422


def test_seed_rerun_never_brings_a_merged_entity_back(seeded, sync_db):
    keep, dup = _id(ORG, "Arada"), _id(ORG, "Deyaar")
    assert _merge(seeded, dup, keep).status_code == 200

    result = apply_plan(build_plan(), sync_db)
    assert result["aliases_merged_entity"] == 2  # "Deyaar" and "Deyaar Development"
    assert result["aliases_inserted"] == 0
    assert sync_db["entity_aliases"].count_documents({"entity_id": dup}) == 0
    assert sync_db["entities"].find_one({"_id": dup})["status"] == "merged"


def test_a_merge_that_stopped_halfway_can_be_finished(seeded, sync_db):
    keep, dup = _id(ORG, "Arada"), _id(ORG, "Deyaar")
    # As if the first attempt stopped right after marking the duplicate.
    sync_db["entities"].update_one({"_id": dup}, {"$set": {
        "status": "merged", "merged_into": keep, "status_before_merge": "active"}})

    resp = _merge(seeded, dup, keep)
    assert resp.status_code == 200, resp.text
    assert resp.json()["aliases_moved"] == 2
    assert sync_db["entity_aliases"].count_documents({"entity_id": dup}) == 0
