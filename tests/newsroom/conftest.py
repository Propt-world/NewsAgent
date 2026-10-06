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
from pymongo import AsyncMongoClient, MongoClient

from src.configs.settings import settings
from src.newsroom.db import ensure_indexes_sync, get_newsroom_db
from src.newsroom.router import mount_newsroom

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

    app = FastAPI()
    mount_newsroom(app, True)
    app.dependency_overrides[get_newsroom_db] = test_db
    with TestClient(app, headers={"X-API-Key": API_KEY, "X-Newsroom-User": "tester@propt.global"}) as client:
        yield client
