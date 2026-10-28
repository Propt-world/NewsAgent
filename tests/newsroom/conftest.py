"""
Database fixtures for Newsroom tests.

Tests that need MongoDB run only when TEST_MONGO_URL is set (for example a
local mongod or `docker run -p 27017:27017 mongo:8`); otherwise they are
skipped. Each test gets its own throwaway database named newsroom_test_<id>,
dropped afterwards. Never point TEST_MONGO_URL at a shared or production cluster.
"""
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import AsyncMongoClient, MongoClient, monitoring

from src.configs.settings import settings
from src.newsroom.db import ItemSources, ensure_indexes_sync, get_item_sources, get_newsroom_db
from src.newsroom.router import get_redis, mount_newsroom

API_KEY = "test-key"


@pytest.fixture
def mongo_url():
    url = os.getenv("TEST_MONGO_URL")
    if not url:
        pytest.skip("TEST_MONGO_URL not set; skipping MongoDB tests")
    return url


@pytest.fixture
def db_name():
    return f"newsroom_test_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def sync_db(mongo_url, db_name):
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=3000)
    db = client[db_name]
    yield db
    client.drop_database(db_name)
    client.close()


@pytest.fixture
def api(mongo_url, db_name, sync_db, monkeypatch):
    """A TestClient for the Newsroom router on the throwaway database, with indexes in place."""
    assert not ensure_indexes_sync(sync_db)
    monkeypatch.setattr(settings, "NEWSAGENT_API_KEY", API_KEY)

    async def test_db():
        client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=3000)
        try:
            yield client[db_name]
        finally:
            await client.close()

    async def test_sources():
        # Articles and signals live in the same throwaway database in tests.
        client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=3000)
        try:
            yield ItemSources(articles=client[db_name]["processed_articles"], signals=client[db_name]["signals"],
                              chunks=client[db_name]["vectorized_articles"])
        finally:
            await client.close()

    queue = FakeQueue()

    async def test_redis():
        yield queue

    app = FastAPI()
    mount_newsroom(app, True)
    app.dependency_overrides[get_newsroom_db] = test_db
    app.dependency_overrides[get_item_sources] = test_sources
    app.dependency_overrides[get_redis] = test_redis
    with TestClient(app, headers={"X-API-Key": API_KEY, "X-Newsroom-User": "tester@propt.global"}) as client:
        client.queue = queue
        yield client


class FakeQueue:
    """Stands in for Redis: records compose jobs instead of queueing them."""

    def __init__(self):
        self.pushed = []
        self.fail = False

    async def rpush(self, name, value):
        if self.fail:
            raise ConnectionError("redis down")
        self.pushed.append((name, value))


@pytest.fixture
def run_async(mongo_url, db_name):
    """Runs an async service function against the throwaway database: run_async(fn, *args)."""
    import asyncio

    def runner(fn, *args, **kwargs):
        async def go():
            client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=3000)
            try:
                return await fn(client[db_name], *args, **kwargs)
            finally:
                await client.close()
        return asyncio.run(go())

    return runner



class QueryCounter(monitoring.CommandListener):
    """Counts the database commands (finds, aggregates, writes...) a client sends."""

    def __init__(self):
        self.count = 0

    def started(self, event):
        if event.command_name not in ("endSessions", "hello", "isMaster", "ping"):
            self.count += 1

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


@pytest.fixture
def run_counted(mongo_url, db_name):
    """Runs fn(db, sources) on the throwaway database (articles and signals live there too)
    and returns (result, number of database commands it sent)."""
    import asyncio

    def runner(fn):
        counter = QueryCounter()

        async def go():
            client = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=3000, event_listeners=[counter])
            db = client[db_name]
            sources = ItemSources(articles=db["processed_articles"], signals=db["signals"],
                                  chunks=db["vectorized_articles"], archived=db["archived_articles"],
                                  deleted=db["deleted_articles"])
            try:
                await client.admin.command("ping")
                counter.count = 0
                return await fn(db, sources)
            finally:
                await client.close()

        result = asyncio.run(go())
        return result, counter.count

    return runner


@pytest.fixture
def trace_spans(monkeypatch):
    """Phoenix tracing switched on, with spans kept in memory instead of sent (also LangChain's own spans)."""
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from src.utils import tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
