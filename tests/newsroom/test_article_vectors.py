"""Pending articles' small vectors: made once while pending, packed, removed when they leave Pending (no real AI)."""
import asyncio
from datetime import datetime, timezone

import pytest
from bson.binary import Binary
from pymongo import AsyncMongoClient

from src.configs.settings import settings
from src.newsroom import article_vectors as av
from src.newsroom import db as ndb
from src.newsroom import pipeline

VECTOR = [0.5, 0.25, -0.125]  # exact in float32


def _article(article_id, status="processed", title="Emaar launches a tower", summary="Sales open in Dubai Hills.",
             day=1):
    return {"_id": article_id, "url": f"https://example.com/{article_id}", "status": status,
            "processed_at": datetime(2026, 10, day, tzinfo=timezone.utc),
            "final_output": {"title": title, "summary": summary}}


@pytest.fixture
def made(monkeypatch):
    calls = []

    async def fake_make(text):
        calls.append(text)
        return VECTOR

    monkeypatch.setattr(av, "make_vector", fake_make)
    monkeypatch.setattr(settings, "NEWSROOM_PENDING_VECTORS_ENABLED", True)
    return calls


def _run(mongo_url, db_name, scenario):
    async def go():
        client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=3000)
        try:
            db = client[db_name]
            return await scenario(db, db["processed_articles"])
        finally:
            await client.close()
    return asyncio.run(go())


def test_pending_article_gets_a_packed_vector_once(mongo_url, db_name, sync_db, made):
    sync_db["processed_articles"].insert_one(_article("a1"))

    async def scenario(db, articles):
        first = await av.refresh(db, articles, "a1")
        again = await av.refresh(db, articles, "a1")  # same title and summary: no new call
        return first, again, await av.get(db, "a1")
    first, again, vector = _run(mongo_url, db_name, scenario)
    assert (first, again, vector) == ("saved", "unchanged", VECTOR)
    assert made == ["Emaar launches a tower\nSales open in Dubai Hills."]
    stored = sync_db[ndb.ARTICLE_VECTORS].find_one({"_id": "a1"})
    assert isinstance(stored["vector"], Binary) and stored["model"] == settings.MONGO_EMBEDDING_MODEL  # packed


def test_an_edit_makes_it_again(mongo_url, db_name, sync_db, made):
    sync_db["processed_articles"].insert_one(_article("a1"))

    async def scenario(db, articles):
        await av.refresh(db, articles, "a1")
        await articles.update_one({"_id": "a1"}, {"$set": {"final_output.title": "Emaar sells out a tower"}})
        return await av.refresh(db, articles, "a1")
    assert _run(mongo_url, db_name, scenario) == "saved" and len(made) == 2


def test_removed_when_it_leaves_pending(mongo_url, db_name, sync_db, made):
    sync_db["processed_articles"].insert_many([_article("a1"), _article("a2")])

    async def scenario(db, articles):
        await av.refresh(db, articles, "a1")
        await av.refresh(db, articles, "a2")
        await articles.update_one({"_id": "a1"}, {"$set": {"status": "approved"}})
        approved = await av.refresh(db, articles, "a1")
        await articles.delete_one({"_id": "a2"})  # archived or deleted: moved out of processed_articles
        gone = await av.refresh(db, articles, "a2")
        return approved, gone, await db[ndb.ARTICLE_VECTORS].count_documents({})
    assert _run(mongo_url, db_name, scenario) == ("removed", "removed", 0)


def test_skipped_without_text_or_when_switched_off_and_never_raises(mongo_url, db_name, sync_db, made, monkeypatch):
    sync_db["processed_articles"].insert_many([_article("a1", title="", summary=""), _article("a2")])

    async def failing(text):
        raise RuntimeError("OpenAI down")

    async def scenario(db, articles):
        no_text = await av.refresh(db, articles, "a1")
        monkeypatch.setattr(settings, "NEWSROOM_PENDING_VECTORS_ENABLED", False)
        off = await av.refresh(db, articles, "a2")
        monkeypatch.setattr(settings, "NEWSROOM_PENDING_VECTORS_ENABLED", True)
        monkeypatch.setattr(av, "make_vector", failing)
        failed = await av.refresh(db, articles, "a2")
        return no_text, off, failed, await db[ndb.ARTICLE_VECTORS].count_documents({})
    assert _run(mongo_url, db_name, scenario) == ("skipped", "skipped", "failed", 0)
    assert made == []


def test_the_newsroom_hooks_keep_it_in_step(monkeypatch):
    """Stored, edited and status changes refresh it; archive and delete remove it."""
    calls = []

    async def fake_refresh(db, articles, article_id):
        calls.append(("refresh", article_id))
        return "saved"

    async def fake_drop(db, article_id):
        calls.append(("drop", article_id))

    async def nothing(*a, **k):
        return None

    class Articles:
        async def find_one(self, query, projection=None):
            return {"_id": "a1"}

    class Sources:
        articles = Articles()

    class Collections:
        processed_articles = Articles()

    monkeypatch.setattr(pipeline, "get_newsroom_db", lambda: None)
    monkeypatch.setattr(pipeline, "get_item_sources", lambda: Sources())
    monkeypatch.setattr(pipeline, "get_async_scheduler_collections", lambda: Collections())
    monkeypatch.setattr(pipeline.lnk, "item_changed", nothing)
    monkeypatch.setattr(pipeline.lnk, "on_item_status", nothing)
    monkeypatch.setattr(pipeline.article_vectors, "refresh", fake_refresh)
    monkeypatch.setattr(pipeline.article_vectors, "drop", fake_drop)

    async def scenario():
        await pipeline.article_stored("https://example.com/a1", None)
        await pipeline.article_edited("a1")
        await pipeline.article_status_changed("a1", "approved")
        await pipeline.article_moved("a1")
    asyncio.run(scenario())
    assert calls == [("refresh", "a1"), ("refresh", "a1"), ("refresh", "a1"), ("drop", "a1")]


def test_backfill_script_newest_first_skipping_those_with_one(mongo_url, db_name, sync_db, made):
    from src.scripts import newsroom_pending_vectors as script

    sync_db["processed_articles"].insert_many([
        _article("old", day=1), _article("new", day=9), _article("has", day=5),
        _article("approved", status="approved", day=10)])

    async def scenario(db, articles):
        await av.refresh(db, articles, "has")
        made.clear()
        dry = await script.run(db, articles, apply=False, limit=20)
        applied = await script.run(db, articles, apply=True, limit=1)
        return dry, applied
    dry, applied = _run(mongo_url, db_name, scenario)
    assert dry == {"without_vector": 2, "picked": 2, "saved": 0, "not_saved": 0}
    assert applied == {"without_vector": 2, "picked": 1, "saved": 1, "not_saved": 0}
    assert len(made) == 1 and sync_db[ndb.ARTICLE_VECTORS].find_one({"_id": "new"})  # newest first
