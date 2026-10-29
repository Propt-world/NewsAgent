"""Related items and entity history (needs TEST_MONGO_URL)."""
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from src.newsroom import links as lnk
from src.newsroom import related
from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

ORG, LOC = "organisation", "location"
NOW = datetime.now(timezone.utc)

EMAAR = seed_entity_id(ORG, "Emaar Properties")
DAMAC = seed_entity_id(ORG, "DAMAC Properties")
UAE = seed_entity_id(LOC, "United Arab Emirates")
DUBAI = seed_entity_id(LOC, "Dubai")
ABU_DHABI = seed_entity_id(LOC, "Abu Dhabi")
CREEK = seed_entity_id(LOC, "Dubai Creek Harbour")


def _article(sync_db, article_id, days_ago):
    sync_db["processed_articles"].insert_one({
        "_id": article_id, "url": f"https://example.com/{article_id}", "status": "processed",
        "processed_at": NOW - timedelta(days=days_ago),
        "final_output": {"title": f"Article {article_id}", "summary": "..."},
    })


def _link(api, source_type, source_id, *entity_ids):
    for entity_id in entity_ids:
        resp = api.post(f"/newsroom/items/{source_type}/{source_id}/links", json={"entity_id": entity_id, "activate": True})
        assert resp.status_code == 200, resp.text


@pytest.fixture
def scene(api, sync_db, run_async):
    """
    A1  Emaar, Creek Harbour (Dubai)        3 days ago
    A2  Emaar, DAMAC, Creek Harbour         2 days ago   <- the item we look from
    A3  Emaar, Abu Dhabi                    5 days ago
    S1  signal: Emaar, DAMAC                1 day ago
    A4  DAMAC                               4 days ago
    A5  Emaar, but rejected                 1 day ago
    A6  only Creek Harbour (a community)    6 days ago
    A7  only Dubai (a city)                 6 days ago
    A8  Emaar                               400 days ago
    """
    apply_plan(build_plan(), sync_db)
    for article_id, days_ago in [("A1", 3), ("A2", 2), ("A3", 5), ("A4", 4), ("A5", 1), ("A6", 6), ("A7", 6), ("A8", 400)]:
        _article(sync_db, article_id, days_ago)
    s1 = ObjectId()
    sync_db["signals"].insert_one({"_id": s1, "summary_en": "Emaar and DAMAC signal", "status": "draft",
                                   "generated_at": NOW - timedelta(days=1), "evidence": []})
    _link(api, "article", "A1", EMAAR, CREEK)
    _link(api, "article", "A2", EMAAR, DAMAC, CREEK)
    _link(api, "article", "A3", EMAAR, ABU_DHABI)
    _link(api, "signal", str(s1), EMAAR, DAMAC)
    _link(api, "article", "A4", DAMAC)
    _link(api, "article", "A5", EMAAR)
    run_async(lnk.on_item_status, "article", "A5", "rejected")
    _link(api, "article", "A6", CREEK)
    _link(api, "article", "A7", DUBAI)
    _link(api, "article", "A8", EMAAR)
    api.s1 = str(s1)
    return api


def _related(api, item="article/A2", **params):
    resp = api.get(f"/newsroom/items/{item}/related", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(body):
    return [i["source_id"] for i in body["items"]]


def test_ranking_and_reasons(scene):
    body = _related(scene)
    assert body["basis"] == "organisations"
    assert _ids(body) == [scene.s1, "A1", "A3", "A4", "A8"]  # 2 shared; then place overlap; then recency
    assert body["total"] == 5
    first, a1 = body["items"][0], body["items"][1]
    assert first["reason"] == "Shares DAMAC Properties, Emaar Properties"
    assert first["source_type"] == "signal"
    assert a1["reason"] == "Shares Emaar Properties · same area: Dubai Creek Harbour"
    assert [p["name"] for p in a1["shared_places"]] == ["Dubai Creek Harbour"]  # not UAE / Dubai as well
    a3 = body["items"][2]
    assert a3["reason"] == "Shares Emaar Properties · same country: United Arab Emirates"


def test_rejected_items_never_shown(scene):
    assert "A5" not in _ids(_related(scene, limit=100))


def test_only_in_one_place(scene):
    assert _ids(_related(scene, location=DUBAI)) == ["A1"]
    assert _ids(_related(scene, location=ABU_DHABI)) == ["A3"]
    bad = scene.get("/newsroom/items/article/A2/related", params={"location": EMAAR})
    assert bad.status_code == 422  # not a place


def test_date_window_and_paging(scene):
    assert "A8" not in _ids(_related(scene, days=30))
    page2 = _related(scene, limit=2, skip=2)
    assert _ids(page2) == ["A3", "A4"]
    assert page2["total"] == 5


def test_switched_off_entities_do_not_count(scene):
    assert scene.patch(f"/newsroom/entities/{DAMAC}", json={"status": "staged"}).status_code == 200
    body = _related(scene)
    assert "A4" not in _ids(body)
    assert body["items"][0]["reason"] == "Shares Emaar Properties · same area: Dubai Creek Harbour"


def test_specific_place_fallback(scene):
    body = _related(scene, item="article/A6")
    assert body["basis"] == "places"
    assert set(_ids(body)) == {"A1", "A2"}
    assert body["items"][0]["reason"] == "Shares Dubai Creek Harbour"
    # A whole city is too broad to relate items on its own.
    assert _related(scene, item="article/A7") == {"basis": "none", "items": [], "total": 0, "limit": 20, "skip": 0}


def test_unlinked_item_has_nothing(scene):
    assert _related(scene, item="article/NOT-LINKED")["basis"] == "none"


def test_entity_history(scene):
    resp = scene.get(f"/newsroom/entities/{EMAAR}/history")
    body = resp.json()
    assert body["entity"]["name"] == "Emaar Properties"
    assert [i["source_id"] for i in body["items"]] == [scene.s1, "A2", "A1", "A3", "A8"]  # newest first, A5 hidden
    assert body["total"] == 5

    in_dubai = scene.get(f"/newsroom/entities/{EMAAR}/history", params={"location": DUBAI}).json()
    assert [i["source_id"] for i in in_dubai["items"]] == ["A2", "A1"]

    recent = scene.get(f"/newsroom/entities/{EMAAR}/history",
                       params={"after": (NOW - timedelta(days=30)).isoformat()}).json()
    assert "A8" not in [i["source_id"] for i in recent["items"]]

    page = scene.get(f"/newsroom/entities/{EMAAR}/history", params={"limit": 2, "skip": 2}).json()
    assert [i["source_id"] for i in page["items"]] == ["A1", "A3"]
    assert scene.get("/newsroom/entities/missing/history").status_code == 404


def _stages_below_group(explain: dict) -> list:
    """The winning plan's stages under its GROUP stage, top to bottom (e.g. FETCH, IXSCAN)."""
    stages = explain.get("stages") or [{"$cursor": explain}]
    node = stages[0]["$cursor"]["queryPlanner"]["winningPlan"]
    node = node.get("queryPlan", node)
    chain, below_group = [], False
    while node:
        if below_group:
            chain.append(node["stage"])
        below_group = below_group or node["stage"] == "GROUP"
        node = node.get("inputStage")
    return chain


def test_queries_use_indexes_and_stay_fast(scene, sync_db):
    """About 6,000 links, similar to a backfilled year; both lookups must use an index."""
    docs = []
    for n in range(6000):
        entity = [EMAAR, DAMAC, seed_entity_id(ORG, "Aldar Properties")][n % 3]
        docs.append({
            "_id": str(uuid.uuid4()), "entity_id": entity, "kind": ORG, "source_type": "article",
            "source_id": f"bulk-{n}", "status": "linked", "item_status": "approved",
            "title": f"Bulk {n}", "timestamp": NOW - timedelta(minutes=n), "topics": [],
            "location_ids": [UAE, DUBAI] if n % 2 else [UAE, ABU_DHABI],
        })
    sync_db["entity_mentions"].insert_many(docs)

    # The list filter's real queries read links in date order from an index: no sort of every link in memory.
    for entity_id, location_id in [(EMAAR, None), (None, UAE), (EMAAR, DUBAI)]:
        match = related.filter_match(entity_id, location_id, None, None, None)
        plan = sync_db.command("explain", {"aggregate": "entity_mentions", "cursor": {},
                                           "pipeline": related.filter_pipeline(match, 20, 0)})
        chain = _stages_below_group(plan)
        assert chain and chain[-1] == "IXSCAN" and "SORT" not in chain, (entity_id, location_id, chain)

    started = time.perf_counter()
    body = _related(scene, limit=20)
    history = scene.get(f"/newsroom/entities/{EMAAR}/history", params={"location": DUBAI}).json()
    elapsed = time.perf_counter() - started
    assert body["total"] > 4000
    assert history["total"] > 900
    assert elapsed < 3.0, f"related + history took {elapsed:.2f}s"


# --- Similar text: approved articles and signals fill the list when strong matches are few ------------------

G1, G2, G3 = ObjectId(), ObjectId(), ObjectId()  # approved, draft and approved signals


@pytest.fixture
def similar_scene(api, sync_db, monkeypatch):
    """Approved and pending articles with stored vectors, and signals; Atlas's vector search itself is replaced
    (not available locally)."""
    from src.newsroom import article_vectors

    apply_plan(build_plan(), sync_db)
    for article_id, status, days_ago in [("S0", "approved", 1), ("S1", "approved", 2), ("S2", "approved", 3),
                                         ("S3", "rejected", 1), ("S4", "approved", 4), ("P1", "processed", 1),
                                         ("S5", "approved", 40)]:
        sync_db["processed_articles"].insert_one({
            "_id": article_id, "url": f"https://example.com/{article_id}", "status": status,
            "processed_at": NOW - timedelta(days=days_ago),
            "final_output": {"title": f"Article {article_id}", "summary": "...",
                             "category": ["Future Forecast"] if article_id == "S1" else ["Developer News"]}})
    for article_id in ["S0", "S1", "S2", "S3", "S4", "S5"]:
        sync_db["vectorized_articles"].insert_one({
            "_id": f"chunk-{article_id}", "embedding": [0.1, 0.2, 0.3],
            "metadata": {"url": f"https://example.com/{article_id}", "chunk_index": 0}})
    sync_db["article_vectors"].insert_one({"_id": "P1", "vector": article_vectors.pack([0.5, 0.25, 0.125])})
    for signal_id, status, days_ago in [(G1, "approved", 1), (G2, "draft", 1), (G3, "approved", 2)]:
        sync_db["signals"].insert_one({"_id": signal_id, "summary_en": f"Signal {signal_id}", "status": status,
                                       "generated_at": NOW - timedelta(days=days_ago), "evidence": [],
                                       "kg_topics": ["Future Forecast"], "embedding": [0.9, 0.8, 0.7]})

    calls = []

    async def fake_articles(chunks, vector, limit):
        calls.append(("articles", list(vector)))
        return [
            {"metadata": {"url": "https://example.com/S0"}, "score": 1.0},    # the opened article itself
            {"metadata": {"url": "https://example.com/S2"}, "score": 0.93},
            {"metadata": {"url": "https://example.com/S1"}, "score": 0.88},
            {"metadata": {"url": "https://example.com/S1"}, "score": 0.81},   # second piece of S1
            {"metadata": {"url": "https://example.com/S3"}, "score": 0.95},   # rejected later
            {"metadata": {"url": "https://example.com/P1"}, "score": 0.94},   # pending: never a match
            {"metadata": {"url": "https://example.com/S5"}, "score": 0.85},   # 40 days old
            {"metadata": {"url": "https://example.com/S4"}, "score": 0.62},   # too weak
        ]

    async def fake_signals(signals, vector, limit):
        calls.append(("signals", list(vector)))
        docs = {d["_id"]: d for d in sync_db["signals"].find({}, {"embedding": 0})}
        return [{**docs[G2], "score": 0.97},   # draft: never a match
                {**docs[G1], "score": 0.91},
                {**docs[G3], "score": 0.83}]

    monkeypatch.setattr(related, "_vector_search", fake_articles)
    monkeypatch.setattr(related, "_signal_vector_search", fake_signals)
    api.search_calls = calls
    return api


def test_approved_article_without_links_gets_similar_articles_and_signals(similar_scene):
    body = _related(similar_scene, item="article/S0")
    assert body["basis"] == "similar"
    # Best score first, both types; itself, rejected, pending, draft and weak matches left out; at most 5
    assert _ids(body) == ["S2", str(G1), "S1", "S5", str(G3)]
    first = body["items"][0]
    assert (first["reason"], first["match"], first["score"]) == ("Similar text (score 0.93)", "text", 0.93)
    assert body["items"][1]["source_type"] == "signal" and body["items"][1]["topics"] == ["Future Forecast"]
    assert similar_scene.search_calls == [("articles", [0.1, 0.2, 0.3]), ("signals", [0.1, 0.2, 0.3])]  # stored


def test_pending_article_uses_its_small_vector(similar_scene):
    body = _related(similar_scene, item="article/P1")
    assert body["basis"] == "similar" and "P1" not in _ids(body) and str(G1) in _ids(body)
    assert similar_scene.search_calls[0] == ("articles", [0.5, 0.25, 0.125])


def test_signal_uses_its_own_vector_and_finds_articles_too(similar_scene):
    body = _related(similar_scene, item=f"signal/{G1}")
    assert _ids(body) == ["S0", "S2", "S1", "S5", str(G3)]  # not itself (G1); at most 5
    assert similar_scene.search_calls[0] == ("articles", [0.9, 0.8, 0.7])
    assert body["items"][2]["shared_topics"] == ["Future Forecast"]  # S1


def test_strong_matches_first_then_similar_text_up_to_five(similar_scene):
    _link(similar_scene, "article", "S0", EMAAR)
    _link(similar_scene, "article", "S2", EMAAR)
    body = _related(similar_scene, item="article/S0")
    assert body["basis"] == "organisations"
    assert [(i["source_id"], i["match"]) for i in body["items"]] == [
        ("S2", "strong"), (str(G1), "text"), ("S1", "text"), ("S5", "text"), (str(G3), "text")]  # S2 once
    assert body["total"] == 5


def test_five_strong_matches_need_no_search(scene):
    calls = []

    async def watch(*a):
        calls.append(a)
        return []

    from unittest.mock import patch
    with patch.object(related, "_vector_search", watch), patch.object(related, "_signal_vector_search", watch):
        assert _related(scene)["total"] == 5
    assert calls == []


def test_filters_apply_to_similar_text(similar_scene):
    assert _ids(_related(similar_scene, item="article/S0", days=30)) == ["S2", str(G1), "S1", str(G3)]  # not S5
    assert _ids(_related(similar_scene, item="article/S0", topic="Future Forecast")) == [str(G1), "S1", str(G3)]
    assert _related(similar_scene, item="article/S0", location=DUBAI)["basis"] == "none"  # place: strong only


def test_a_missing_vector_index_leaves_only_its_part_out(similar_scene, monkeypatch):
    async def broken(*_):
        raise RuntimeError("index not found")

    monkeypatch.setattr(related, "_signal_vector_search", broken)  # e.g. dev without the signals' index
    assert _ids(_related(similar_scene, item="article/S0")) == ["S2", "S1", "S5"]
    monkeypatch.setattr(related, "_vector_search", broken)
    assert _related(similar_scene, item="article/S0")["basis"] == "none"


def test_no_stored_vector_no_search(similar_scene, sync_db):
    sync_db["article_vectors"].delete_many({})
    assert _related(similar_scene, item="article/P1")["basis"] == "none"
    assert similar_scene.search_calls == []


def _filtered(api, **params):
    resp = api.get("/newsroom/items", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_newsroom_list_filter(scene):
    # A company: one row per item, newest first, rejected A5 hidden
    emaar = _filtered(scene, entity=EMAAR)
    assert _ids(emaar) == [scene.s1, "A2", "A1", "A3", "A8"] and emaar["total"] == 5
    # A place includes its communities (A1, A2, A6 are in Dubai Creek Harbour), one row per item
    assert _ids(_filtered(scene, location=DUBAI)) == ["A2", "A1", "A6", "A7"]
    # Both together
    assert _ids(_filtered(scene, entity=DAMAC, location=DUBAI)) == ["A2"]
    # Type and status, as the Newsroom toggles send them (pending signals are "draft")
    assert _ids(_filtered(scene, entity=EMAAR, source_type="signal", status=["draft"])) == [scene.s1]
    assert _ids(_filtered(scene, entity=EMAAR, source_type="article", status=["approved"])) == []
    assert "A5" not in _ids(_filtered(scene, entity=EMAAR))  # rejected: hidden by default
    assert _ids(_filtered(scene, entity=EMAAR, status=["rejected"])) == ["A5"]  # the Rejected view asks for it
    # Title search is plain text
    assert _ids(_filtered(scene, entity=EMAAR, q="article a3")) == ["A3"]
    assert _filtered(scene, entity=EMAAR, q="(")["total"] == 0
    # Paging
    page = _filtered(scene, entity=EMAAR, limit=2, skip=2)
    assert _ids(page) == ["A1", "A3"] and page["total"] == 5
    # Errors
    assert scene.get("/newsroom/items").status_code == 422
    assert scene.get("/newsroom/items", params={"location": EMAAR}).status_code == 422  # not a place
    assert scene.get("/newsroom/items", params={"entity": "missing"}).status_code == 404


def test_shared_topics_order_related_items_and_narrow_the_list(scene, sync_db):
    """Topics (articles' categories, signals' kg_topics) only order related items and narrow the list filter."""
    topics = {"A2": ["Off-Plan & New Launches", "Developer News"], "A8": ["Developer News"], "A4": ["Luxury Residences"]}
    for article_id, names in topics.items():
        sync_db["processed_articles"].update_one({"_id": article_id}, {"$set": {"final_output.category": names}})
    sync_db["signals"].update_one({"_id": ObjectId(scene.s1)}, {"$set": {"kg_topics": ["Off-Plan & New Launches"]}})
    # What social_scrapper and the pipeline send after a change; NewsAgent re-reads the items
    for source_type, ids in (("article", list(topics)), ("signal", [scene.s1])):
        resp = scene.post("/newsroom/items/changed", json={"source_type": source_type, "source_ids": ids})
        assert resp.status_code == 200, resp.text

    body = _related(scene)
    # A8 (400 days old, shares a topic) now comes before A4 (4 days old, no shared topic); companies and places
    # still come first, so the order of the others is unchanged
    assert _ids(body) == [scene.s1, "A1", "A3", "A8", "A4"]
    first, a8, a4 = body["items"][0], body["items"][3], body["items"][4]
    assert first["shared_topics"] == ["Off-Plan & New Launches"]
    assert first["reason"] == "Shares DAMAC Properties, Emaar Properties · same topic: Off-Plan & New Launches"
    assert (a8["shared_topics"], a4["shared_topics"]) == (["Developer News"], [])
    # Each item's own topics too, so an item opened from the related list shows them
    assert (a8["topics"], a4["topics"]) == (["Developer News"], ["Luxury Residences"])

    # The related list's Topic filter: only related items with that topic
    luxury = _related(scene, topic="Luxury Residences")
    assert (_ids(luxury), luxury["total"]) == (["A4"], 1)
    assert _ids(_related(scene, topic="Off-Plan & New Launches")) == [scene.s1]
    assert scene.get("/newsroom/items/article/A2/related", params={"topic": "Financing"}).status_code == 422

    # The list filter: company + topic
    by_topic = _filtered(scene, entity=EMAAR, topic="Developer News")
    assert _ids(by_topic) == ["A2", "A8"]
    assert by_topic["items"][0]["topics"] == ["Off-Plan & New Launches", "Developer News"]  # shown in the panel
    assert _ids(_filtered(scene, entity=DAMAC, topic="Off-Plan & New Launches")) == [scene.s1, "A2"]
    assert scene.get("/newsroom/items", params={"entity": EMAAR, "topic": "Financing"}).status_code == 422

    # A new link takes the item's topics straight away
    _article(sync_db, "A9", 1)
    sync_db["processed_articles"].update_one({"_id": "A9"}, {"$set": {"final_output.category": ["Developer News"]}})
    _link(scene, "article", "A9", EMAAR)
    assert _ids(_filtered(scene, entity=EMAAR, topic="Developer News")) == ["A9", "A2", "A8"]
