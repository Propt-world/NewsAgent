"""Related items and entity history (needs TEST_MONGO_URL)."""
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from src.newsroom import links as lnk
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

    plan = sync_db["entity_mentions"].find(
        {"entity_id": EMAAR, "status": "linked", "item_status": {"$nin": ["rejected"]}, "location_ids": DUBAI}
    ).sort([("timestamp", -1)]).limit(50).explain()
    assert "IXSCAN" in str(plan["queryPlanner"]["winningPlan"])

    started = time.perf_counter()
    body = _related(scene, limit=20)
    history = scene.get(f"/newsroom/entities/{EMAAR}/history", params={"location": DUBAI}).json()
    elapsed = time.perf_counter() - started
    assert body["total"] > 4000
    assert history["total"] > 900
    assert elapsed < 3.0, f"related + history took {elapsed:.2f}s"


# --- Similar articles (option A: approved articles only, stored fingerprints, no AI call) ---

@pytest.fixture
def similar_scene(api, sync_db, monkeypatch):
    """Approved articles with stored chunks; the Atlas vector search itself is replaced (not available locally)."""
    from src.newsroom import related

    apply_plan(build_plan(), sync_db)
    for article_id, status, days_ago in [("S0", "approved", 1), ("S1", "approved", 2), ("S2", "approved", 3),
                                         ("S3", "rejected", 1), ("S4", "approved", 4), ("P1", "processed", 1)]:
        sync_db["processed_articles"].insert_one({
            "_id": article_id, "url": f"https://example.com/{article_id}", "status": status,
            "processed_at": NOW - timedelta(days=days_ago),
            "final_output": {"title": f"Article {article_id}", "summary": "..."}})
    for article_id in ["S0", "S1", "S2", "S3", "S4"]:
        sync_db["vectorized_articles"].insert_one({
            "_id": f"chunk-{article_id}", "embedding": [0.1, 0.2, 0.3],
            "metadata": {"url": f"https://example.com/{article_id}", "chunk_index": 0}})

    calls = []

    async def fake_search(chunks, vector, limit):
        calls.append(vector)
        return [
            {"metadata": {"url": "https://example.com/S0"}, "score": 1.0},    # itself
            {"metadata": {"url": "https://example.com/S2"}, "score": 0.93},
            {"metadata": {"url": "https://example.com/S1"}, "score": 0.88},
            {"metadata": {"url": "https://example.com/S1"}, "score": 0.81},   # second chunk of S1
            {"metadata": {"url": "https://example.com/S3"}, "score": 0.95},   # rejected later
            {"metadata": {"url": "https://example.com/S4"}, "score": 0.62},   # too weak
        ]

    monkeypatch.setattr(related, "_vector_search", fake_search)
    api.search_calls = calls
    return api


def test_similar_for_approved_article_without_links(similar_scene):
    body = _related(similar_scene, item="article/S0")
    assert body["basis"] == "similar"
    assert _ids(body) == ["S2", "S1"]  # itself, rejected and weak matches left out; best score first
    assert body["items"][0]["reason"] == "Similar text (score 0.93)"
    assert body["items"][1]["score"] == 0.88
    assert similar_scene.search_calls == [[0.1, 0.2, 0.3]]  # the stored fingerprint, no new AI call


def test_pending_articles_never_use_similarity(similar_scene):
    assert _related(similar_scene, item="article/P1")["basis"] == "none"
    assert similar_scene.search_calls == []


def test_linked_articles_use_companies_not_similarity(similar_scene):
    _link(similar_scene, "article", "S0", EMAAR)
    assert _related(similar_scene, item="article/S0")["basis"] == "organisations"
    assert similar_scene.search_calls == []


def test_place_filter_does_not_apply_to_similarity(similar_scene):
    assert _related(similar_scene, item="article/S0", location=DUBAI)["basis"] == "none"


def test_signals_never_use_similarity(similar_scene, sync_db):
    signal_id = ObjectId()
    sync_db["signals"].insert_one({"_id": signal_id, "summary_en": "x", "status": "approved", "evidence": []})
    assert _related(similar_scene, item=f"signal/{signal_id}")["basis"] == "none"
    assert similar_scene.search_calls == []


def test_missing_vector_index_shows_nothing(similar_scene, monkeypatch):
    from src.newsroom import related

    async def broken(*_):
        raise RuntimeError("index not found")

    monkeypatch.setattr(related, "_vector_search", broken)
    assert _related(similar_scene, item="article/S0")["basis"] == "none"


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
    assert _ids(_filtered(scene, entity=EMAAR, status=["rejected"])) == []  # never shown, even if asked
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
