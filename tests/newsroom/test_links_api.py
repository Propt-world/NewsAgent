"""Suggestions, links and the new-names queue (needs TEST_MONGO_URL)."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from src.newsroom import links as lnk
from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

ORG, LOC = "organisation", "location"
ARTICLE = "art-1"
ITEM = f"/newsroom/items/article/{ARTICLE}"


def _id(kind, name):
    return seed_entity_id(kind, name)


@pytest.fixture
def seeded(api, sync_db):
    apply_plan(build_plan(), sync_db)
    sync_db["processed_articles"].insert_one({
        "_id": ARTICLE,
        "url": "https://example.com/emaar-launch",
        "status": "processed",
        "processed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "final_output": {"title": "Emaar launches a tower", "summary": "Emaar announced...", "published_date": None},
    })
    return api


def _suggest(api, organisations=(), locations=(), source_type="article", source_id=ARTICLE):
    resp = api.post("/newsroom/suggest", json={
        "source_type": source_type, "source_id": source_id,
        "organisations": list(organisations), "locations": list(locations)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _open(api, item=ITEM):
    return {s["name"]: s for s in api.get(f"{item}/entities").json()["suggestions"]}


def _links(api, item=ITEM):
    return {l["canonical_name"]: l for l in api.get(f"{item}/entities").json()["links"]}


NAMES = dict(
    organisations=["Emaar", "Wasl", "PanicSelling.com", "waslgroup", "Brand New Developer", "Stake Saudi", "DAMAC", "emaar "],
    locations=["Dubai Creek Harbour", "Dubai", "JVC"],
)


def test_suggest_rules(seeded):
    counts = _suggest(seeded, **NAMES)
    assert counts["created"] == 7
    assert counts["skipped_ambiguous"] == 1      # "Wasl"
    assert counts["skipped_handle"] == 1         # PanicSelling.com
    assert counts["skipped_ignored"] == 1        # waslgroup
    suggestions = _open(seeded)
    assert suggestions["Emaar"]["entity_id"] == _id(ORG, "Emaar Properties")
    assert suggestions["Emaar"]["reason"] == "exact match on alias 'Emaar'"
    assert suggestions["Stake Saudi"]["entity_status"] == "staged"
    assert suggestions["Brand New Developer"]["entity_id"] is None
    assert suggestions["Brand New Developer"]["reason"] == "not in the list"
    assert suggestions["JVC"]["entity_id"] == _id(LOC, "Jumeirah Village Circle")
    assert _links(seeded) == {}  # suggestions are not graph facts

    again = _suggest(seeded, **NAMES)
    assert (again["created"], again["existing"]) == (0, 7)


def test_overlong_names_are_never_suggested(seeded, run_async):
    # Found automatically (not through the API's limits): text this long is not a name.
    counts = run_async(lnk.suggest, "article", ARTICLE, ["Emaar " + "x" * 200], [])
    assert counts["created"] == 0 and _open(seeded) == {}


def test_names_are_tried_in_the_other_list(seeded):
    # "Yas Island" given as a company/project is the place; "KEZAD" given as a place is the company.
    counts = _suggest(seeded, organisations=["Yas Island"], locations=["KEZAD", "Nowhere Town"])
    assert counts["created"] == 3
    suggestions = _open(seeded)
    yas, kezad = suggestions["Yas Island"], suggestions["KEZAD"]
    assert (yas["field"], yas["entity_id"]) == (LOC, _id(LOC, "Yas Island"))
    assert yas["reason"] == "exact match on alias 'Yas Island' (given as a company name)"
    assert (kezad["field"], kezad["entity_id"]) == (ORG, _id(ORG, "KEZAD"))
    assert suggestions["Nowhere Town"]["field"] == LOC and suggestions["Nowhere Town"]["entity_id"] is None
    # The suggestion is stored under the entity's own list, so it can be linked normally.
    resp = seeded.post(f"{ITEM}/links", json={"entity_id": yas["entity_id"], "suggestion_id": yas["id"]})
    assert resp.status_code == 200, resp.text
    assert "Yas Island" in _links(seeded)


def test_link_unlink_is_sticky(seeded, sync_db):
    _suggest(seeded, **NAMES)
    emaar = _open(seeded)["Emaar"]
    resp = seeded.post(f"{ITEM}/links", json={"entity_id": emaar["entity_id"], "suggestion_id": emaar["id"]})
    assert resp.status_code == 200, resp.text
    link = _links(seeded)["Emaar Properties"]
    assert link["linked_by"] == "tester@propt.global"
    assert "Emaar" not in _open(seeded)
    mention = sync_db["entity_mentions"].find_one({"entity_id": emaar["entity_id"]})
    assert mention["title"] == "Emaar launches a tower"
    assert mention["timestamp"].year == 2026  # falls back to processed_at when there is no published date

    assert seeded.delete(f"{ITEM}/links/{emaar['entity_id']}").status_code == 200
    assert _links(seeded) == {}
    assert seeded.delete(f"{ITEM}/links/{emaar['entity_id']}").status_code == 404
    # Never suggested again for this item...
    assert _suggest(seeded, organisations=["Emaar Properties"])["skipped_linked_or_removed"] == 1
    assert "Emaar Properties" not in _open(seeded)
    # ...but a reviewer can still link it by hand.
    assert seeded.post(f"{ITEM}/links", json={"entity_id": emaar["entity_id"]}).status_code == 200
    assert "Emaar Properties" in _links(seeded)
    actions = [a["action"] for a in seeded.get(f"/newsroom/entities/{emaar['entity_id']}/audit").json()]
    assert actions == ["link", "unlink", "link"]


def test_switched_off_entity_needs_activate(seeded):
    _suggest(seeded, **NAMES)
    stake_saudi = _open(seeded)["Stake Saudi"]
    body = {"entity_id": stake_saudi["entity_id"], "suggestion_id": stake_saudi["id"]}
    assert seeded.post(f"{ITEM}/links", json=body).status_code == 409
    assert seeded.post(f"{ITEM}/links", json={**body, "activate": True}).status_code == 200
    assert seeded.get(f"/newsroom/entities/{stake_saudi['entity_id']}").json()["status"] == "active"


def test_locations_include_ancestors_and_follow_moves(seeded):
    _suggest(seeded, **NAMES)
    emaar = _open(seeded)["Emaar"]
    creek = _open(seeded)["Dubai Creek Harbour"]
    seeded.post(f"{ITEM}/links", json={"entity_id": emaar["entity_id"]})
    seeded.post(f"{ITEM}/links", json={"entity_id": creek["entity_id"], "activate": True})
    uae, dubai, abu_dhabi = _id(LOC, "United Arab Emirates"), _id(LOC, "Dubai"), _id(LOC, "Abu Dhabi")
    expected = sorted([uae, dubai, creek["entity_id"]])
    assert sorted(_links(seeded)["Emaar Properties"]["location_ids"]) == expected

    # Moving the place in the hierarchy updates every item that has it linked.
    assert seeded.patch(f"/newsroom/entities/{creek['entity_id']}", json={"parent_id": abu_dhabi}).status_code == 200
    assert sorted(_links(seeded)["Emaar Properties"]["location_ids"]) == sorted([uae, abu_dhabi, creek["entity_id"]])

    seeded.delete(f"{ITEM}/links/{creek['entity_id']}")
    assert _links(seeded)["Emaar Properties"]["location_ids"] == []


def test_link_all_only_switched_on(seeded):
    _suggest(seeded, **NAMES)
    resp = seeded.post(f"{ITEM}/links/all")
    assert resp.status_code == 200
    assert set(_links(seeded)) == {"Emaar Properties", "DAMAC Properties", "Dubai"}
    assert {"Stake Saudi", "Brand New Developer", "Dubai Creek Harbour", "JVC"} <= set(_open(seeded))


def test_dismiss_is_remembered(seeded):
    _suggest(seeded, **NAMES)
    damac = _open(seeded)["DAMAC"]
    assert seeded.post(f"/newsroom/suggestions/{damac['id']}/dismiss").status_code == 200
    assert seeded.post(f"/newsroom/suggestions/{damac['id']}/dismiss").status_code == 409
    assert _suggest(seeded, organisations=["DAMAC"])["existing"] == 1
    assert "DAMAC" not in _open(seeded)


def test_link_rejections(seeded):
    _suggest(seeded, **NAMES)
    emaar_id = _id(ORG, "Emaar Properties")
    assert seeded.post("/newsroom/items/article/missing/links", json={"entity_id": emaar_id}).status_code == 404
    dubai_suggestion = _open(seeded)["Dubai"]
    wrong_kind = {"entity_id": emaar_id, "suggestion_id": dubai_suggestion["id"]}
    assert seeded.post(f"{ITEM}/links", json=wrong_kind).status_code == 422
    seeded.patch(f"/newsroom/entities/{emaar_id}", json={"status": "ignored"})
    assert seeded.post(f"{ITEM}/links", json={"entity_id": emaar_id}).status_code == 422
    assert seeded.get("/newsroom/items/podcast/x/entities").status_code == 422


def test_new_names_queue(seeded, run_async):
    _suggest(seeded, **NAMES)
    assert seeded.get("/newsroom/new-names").json() == []  # not approved yet

    run_async(lnk.on_item_status, "article", ARTICLE, "approved")
    groups = seeded.get("/newsroom/new-names").json()
    assert [(g["name"], g["count"]) for g in groups] == [("Brand New Developer", 1)]

    created = seeded.post("/newsroom/entities", json={
        "kind": ORG, "type": "developer", "status": "active", "canonical_name": "Brand New Developer"}).json()
    resp = seeded.post("/newsroom/new-names/link", json={
        "field": ORG, "name_norm": groups[0]["name_norm"], "entity_id": created["id"]})
    assert resp.json() == {"count": 1}
    assert "Brand New Developer" in _links(seeded)
    assert seeded.get("/newsroom/new-names").json() == []


def test_rejected_items_never_feed_the_queue(seeded, run_async, sync_db):
    _suggest(seeded, **NAMES)
    run_async(lnk.on_item_status, "article", ARTICLE, "approved")
    run_async(lnk.on_item_status, "article", ARTICLE, "rejected")
    assert seeded.get("/newsroom/new-names").json() == []
    seeded.post(f"{ITEM}/links", json={"entity_id": _id(ORG, "Emaar Properties")})
    run_async(lnk.on_item_status, "article", ARTICLE, "rejected")
    assert sync_db["entity_mentions"].find_one({"source_id": ARTICLE})["item_status"] == "rejected"


def test_new_name_dismiss(seeded, run_async):
    _suggest(seeded, **NAMES)
    run_async(lnk.on_item_status, "article", ARTICLE, "approved")
    name_norm = seeded.get("/newsroom/new-names").json()[0]["name_norm"]
    assert seeded.post("/newsroom/new-names/dismiss", json={"field": ORG, "name_norm": name_norm}).json() == {"count": 1}
    assert seeded.post("/newsroom/new-names/dismiss", json={"field": ORG, "name_norm": name_norm}).status_code == 404


def test_signal_items(seeded, sync_db):
    signal_id = ObjectId()
    sync_db["signals"].insert_one({
        "_id": signal_id, "summary_en": "Nakheel launches Palm Jebel Ali villas", "status": "draft",
        "generated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "evidence": [{"url": "https://x.example/post"}],
    })
    item = f"/newsroom/items/signal/{signal_id}"
    _suggest(seeded, organisations=["Nakheel"], source_type="signal", source_id=str(signal_id))
    nakheel = _open(seeded, item)["Nakheel"]
    assert seeded.post(f"{item}/links", json={"entity_id": nakheel["entity_id"]}).status_code == 200
    mention = sync_db["entity_mentions"].find_one({"source_type": "signal"})
    assert mention["url"] == "https://x.example/post"
    assert mention["title"].startswith("Nakheel launches")
    assert seeded.post("/newsroom/items/signal/not-an-id/links", json={"entity_id": nakheel["entity_id"]}).status_code == 404


def test_items_changed_refreshes_signal_links_and_queue(seeded, sync_db):
    signal_id = ObjectId()
    sync_db["signals"].insert_one({
        "_id": signal_id, "summary_en": "Aldar sells out a Yas Island project", "status": "draft",
        "generated_at": datetime(2026, 9, 3, tzinfo=timezone.utc), "evidence": [],
    })
    item = f"/newsroom/items/signal/{signal_id}"
    _suggest(seeded, organisations=["Aldar", "Unknown Signal Co"], source_type="signal", source_id=str(signal_id))
    aldar = _open(seeded, item)["Aldar"]
    seeded.post(f"{item}/links", json={"entity_id": aldar["entity_id"]})

    # social_scrapper approves and edits the signal, then sends only the id.
    sync_db["signals"].update_one({"_id": signal_id}, {"$set": {"status": "approved", "summary_en": "Aldar sells out Yas phase 2"}})
    resp = seeded.post("/newsroom/items/changed", json={"source_type": "signal", "source_ids": [str(signal_id), str(signal_id)]})
    assert resp.json() == {"updated": 1, "missing": 0}
    mention = sync_db["entity_mentions"].find_one({"source_id": str(signal_id)})
    assert (mention["item_status"], mention["title"]) == ("approved", "Aldar sells out Yas phase 2")
    assert [g["name"] for g in seeded.get("/newsroom/new-names").json()] == ["Unknown Signal Co"]

    # Rejected later: out of the queue.
    sync_db["signals"].update_one({"_id": signal_id}, {"$set": {"status": "rejected"}})
    seeded.post("/newsroom/items/changed", json={"source_type": "signal", "source_ids": [str(signal_id)]})
    assert seeded.get("/newsroom/new-names").json() == []

    # Deleted in social_scrapper: links are kept but marked missing.
    sync_db["signals"].delete_one({"_id": signal_id})
    resp = seeded.post("/newsroom/items/changed", json={"source_type": "signal", "source_ids": [str(signal_id)]})
    assert resp.json() == {"updated": 0, "missing": 1}
    assert sync_db["entity_mentions"].find_one({"source_id": str(signal_id)})["item_status"] == "missing"
    assert seeded.post("/newsroom/items/changed", json={"source_type": "signal", "source_ids": []}).status_code == 422


def test_items_summary_for_list_badges(seeded, sync_db):
    _suggest(seeded, **NAMES)
    emaar = _open(seeded)["Emaar"]
    seeded.post(f"{ITEM}/links", json={"entity_id": emaar["entity_id"], "suggestion_id": emaar["id"]})
    resp = seeded.post("/newsroom/items/summary", json={"source_type": "article", "source_ids": [ARTICLE, "no-such-item"]})
    assert resp.status_code == 200
    assert resp.json()["items"] == {
        ARTICLE: {"links": 1, "open_suggestions": 6},
        "no-such-item": {"links": 0, "open_suggestions": 0},
    }
    assert seeded.post("/newsroom/items/summary", json={"source_type": "article", "source_ids": []}).status_code == 422


def test_new_names_page_reads_few_items_in_few_queries(seeded, sync_db, run_counted):
    """A name on 40 items sends back its count and 5 examples with titles; titles are read in batches."""
    signal_ids = [ObjectId() for _ in range(10)]
    sync_db["signals"].insert_many([{"_id": oid, "summary_en": f"Signal {n}", "status": "approved"}
                                    for n, oid in enumerate(signal_ids)])
    sync_db["processed_articles"].insert_many([{"_id": f"big-{n}", "status": "approved",
                                                "final_output": {"title": f"Big {n}"}} for n in range(30)])
    refs = [("article", f"big-{n}") for n in range(30)] + [("signal", str(oid)) for oid in signal_ids]
    rows = []
    for n, (source_type, source_id) in enumerate(refs):
        for name in ["Popular Co"] + ([f"Rare Co {n}"] if n % 2 else []):
            rows.append({"_id": f"s-{n}-{name}", "source_type": source_type, "source_id": source_id,
                         "field": ORG, "name": name, "name_norm": name.lower(), "entity_id": None,
                         "status": "open", "in_new_names_queue": True,
                         "created_at": datetime(2026, 9, 1, n // 60, n % 60, tzinfo=timezone.utc)})
    sync_db["entity_suggestions"].insert_many(rows)

    groups, queries = run_counted(lambda db, sources: lnk.list_new_names(db, 100, sources))
    assert len(groups) == 21
    popular = groups[0]
    assert (popular["name"], popular["count"], len(popular["items"])) == ("Popular Co", 40, 5)
    assert [i["title"] for i in popular["items"]] == ["Big 0", "Big 1", "Big 2", "Big 3", "Big 4"]
    by_name = {g["name"]: g for g in groups}
    assert by_name["Rare Co 39"]["items"][0]["title"] == "Signal 9"  # signal titles too
    assert queries <= 6, queries  # was one lookup per example


def test_new_names_show_where_they_were_found(seeded, run_async):
    _suggest(seeded, **NAMES)
    run_async(lnk.on_item_status, "article", ARTICLE, "approved")
    group = seeded.get("/newsroom/new-names").json()[0]
    assert group["items"][0]["title"] == "Emaar launches a tower"


# --- Names added to the list later (dev, 2026-10-21: Zoya Developments) -----------------------

OTHER = "art-2"
OTHER_ITEM = f"/newsroom/items/article/{OTHER}"


@pytest.fixture
def two_items(seeded, sync_db):
    sync_db["processed_articles"].insert_one({
        "_id": OTHER, "url": "https://example.com/zoya-2", "status": "processed",
        "processed_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
        "final_output": {"title": "Zoya launches Elinor", "summary": "Zoya...", "published_date": None}})
    _suggest(seeded, organisations=["Zoya Developments", "Brand New Developer"])
    _suggest(seeded, organisations=["Zoya Developments"], source_id=OTHER)
    return seeded


def _create(api, name, status="active"):
    resp = api.post("/newsroom/entities", json={"kind": ORG, "type": "developer", "status": status, "canonical_name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_name_added_from_one_item_becomes_a_suggestion_on_the_others(two_items):
    first = _open(two_items)["Zoya Developments"]
    assert first["entity_id"] is None
    zoya = _create(two_items, "Zoya Developments")
    two_items.post(f"{ITEM}/links", json={"entity_id": zoya["id"], "suggestion_id": first["id"]})

    other = _open(two_items, OTHER_ITEM)["Zoya Developments"]
    assert (other["entity_id"], other["entity_status"]) == (zoya["id"], "active")
    assert other["reason"] == "exact match on alias 'Zoya Developments' (added to the list after this item was processed)"
    assert _links(two_items, OTHER_ITEM) == {}  # offered, never linked by itself
    assert _open(two_items)["Brand New Developer"]["entity_id"] is None  # other names untouched


def test_an_alias_or_a_switched_off_entity_matches_too(two_items):
    zoya = _create(two_items, "Zoya", status="staged")
    assert _open(two_items, OTHER_ITEM)["Zoya Developments"]["entity_id"] is None
    resp = two_items.post(f"/newsroom/entities/{zoya['id']}/aliases", json={"alias": "Zoya Developments"})
    assert resp.status_code == 201, resp.text
    other = _open(two_items, OTHER_ITEM)["Zoya Developments"]
    assert (other["entity_id"], other["entity_status"]) == (zoya["id"], "staged")
    assert "(entity is switched off)" in other["reason"]


def test_an_entity_added_outside_the_api_is_matched_when_the_item_is_opened(two_items, sync_db):
    # e.g. by the seed script
    sync_db["entities"].insert_one({"_id": "org-zoya", "kind": ORG, "type": "developer", "status": "active",
                                    "canonical_name": "Zoya Developments"})
    sync_db["entity_aliases"].insert_one({"_id": "alias-zoya", "kind": ORG, "entity_id": "org-zoya",
                                          "alias": "Zoya Developments", "alias_norm": "zoya developments",
                                          "ambiguous": False})
    assert _open(two_items, OTHER_ITEM)["Zoya Developments"]["entity_id"] == "org-zoya"


def test_an_ambiguous_name_still_suggests_nothing(two_items):
    resp = two_items.post("/newsroom/entities", json={
        "kind": ORG, "type": "developer", "status": "active", "canonical_name": "Zoya Developments",
        "ambiguous_aliases": ["Zoya Developments"]})
    assert resp.status_code == 201, resp.text
    assert _open(two_items, OTHER_ITEM)["Zoya Developments"]["entity_id"] is None


def test_a_removed_link_stays_removed(two_items, sync_db):
    zoya = _create(two_items, "Zoya Developments")
    sync_db["entity_suggestions"].update_many({"source_id": OTHER}, {"$set": {"entity_id": None, "status": "open"}})
    assert two_items.post(f"{OTHER_ITEM}/links", json={"entity_id": zoya["id"]}).status_code == 200
    assert two_items.delete(f"{OTHER_ITEM}/links/{zoya['id']}").status_code == 200
    sync_db["entity_suggestions"].update_many({"source_id": OTHER}, {"$set": {"entity_id": None, "status": "open"}})
    assert "Zoya Developments" not in _open(two_items, OTHER_ITEM)  # closed again, not offered
    assert _links(two_items, OTHER_ITEM) == {}
