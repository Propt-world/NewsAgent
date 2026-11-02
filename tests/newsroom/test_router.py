import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.configs.settings import Settings, settings
from src.newsroom.router import mount_newsroom

REPO_ROOT = Path(__file__).resolve().parents[2]


def _client(enabled: bool) -> TestClient:
    app = FastAPI()
    mount_newsroom(app, enabled)
    return TestClient(app)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(settings, "NEWSAGENT_API_KEY", "test-key")
    return "test-key"


def test_disabled_mounts_nothing():
    app = FastAPI()
    assert mount_newsroom(app, False) is False
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/newsroom")]
    assert TestClient(app).get("/newsroom/health").status_code == 404


def test_health_with_valid_key(api_key):
    resp = _client(True).get("/newsroom/health", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["service"] == "newsroom"
    assert "timestamp" in body


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_health_rejects_missing_or_wrong_key(api_key, headers):
    assert _client(True).get("/newsroom/health", headers=headers).status_code == 401


def test_health_follows_existing_open_mode_when_no_key_set(monkeypatch):
    # Same behaviour as every other endpoint: verify_api_key allows all when NEWSAGENT_API_KEY is unset.
    monkeypatch.setattr(settings, "NEWSAGENT_API_KEY", None)
    assert _client(True).get("/newsroom/health").status_code == 200


@pytest.mark.parametrize(
    "value, expected",
    [
        ("true", True),
        ("TRUE", True),
        (" 1 ", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_newsroom_enabled_parsing(value, expected):
    assert Settings(NEWSROOM_ENABLED=value).newsroom_enabled is expected


def _scheduler_newsroom_paths(env_value):
    """Imports the real scheduler app in a fresh process and lists its /newsroom routes."""
    env = {k: v for k, v in os.environ.items() if k != "NEWSROOM_ENABLED"}
    if env_value is not None:
        env["NEWSROOM_ENABLED"] = env_value
    # The app logs to stdout, so the result line is tagged to find it among the logs.
    code = (
        "from src.scheduler.main import app\n"
        "print('NEWSROOM_ROUTES=' + str(sorted(r.path for r in app.routes if getattr(r, 'path', '').startswith('/newsroom'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = [l for l in result.stdout.splitlines() if l.startswith("NEWSROOM_ROUTES=")]
    assert len(lines) == 1, result.stdout
    return lines[0].removeprefix("NEWSROOM_ROUTES=")


def test_scheduler_has_no_newsroom_routes_by_default():
    assert _scheduler_newsroom_paths(None) == "[]"


def test_scheduler_mounts_newsroom_when_enabled():
    paths = _scheduler_newsroom_paths("true")
    assert "'/newsroom/health'" in paths
    assert "'/newsroom/entities'" in paths


LONG_ID, LONG_NAME = "x" * 101, "n" * 201


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", f"/newsroom/entities/{LONG_ID}", {}),
    ("get", f"/newsroom/items/article/{LONG_ID}/entities", {}),
    ("get", "/newsroom/items", {"params": {"location": LONG_ID}}),
    ("get", "/newsroom/items", {"params": {"entity": "e", "status": ["processed"] * 21}}),
    ("get", "/newsroom/items", {"params": {"entity": "e", "status": ["s" * 41]}}),
    ("post", "/newsroom/suggest", {"json": {"source_type": "article", "source_id": "a", "organisations": [LONG_NAME]}}),
    ("post", "/newsroom/items/changed", {"json": {"source_type": "article", "source_ids": [LONG_ID]}}),
    ("post", "/newsroom/items/summary", {"json": {"source_type": "article", "source_ids": [LONG_ID]}}),
    ("post", "/newsroom/new-names/link", {"json": {"field": "organisation", "name_norm": LONG_NAME, "entity_id": "e"}}),
    ("post", "/newsroom/entities", {"json": {"kind": "organisation", "type": "developer", "canonical_name": "A",
                                             "aliases": [LONG_NAME]}}),
])
def test_oversized_input_is_refused_before_any_work(api_key, method, path, kwargs):
    """Each id, name and list has a size limit (no database is needed to refuse these)."""
    resp = getattr(_client(True), method)(path, headers={"X-API-Key": api_key}, **kwargs)
    assert resp.status_code == 422, resp.text


def test_newsroom_data_lives_with_the_articles_and_signals_are_read_from_social_scrapper(monkeypatch):
    """User decision 2026-10-19: the Newsroom's own collections are in NewsAgent's database."""
    from src.newsroom import db as ndb

    monkeypatch.setattr(settings, "MONGO_DB_NAME", "newsagent_x")
    monkeypatch.setattr(settings, "NEWSROOM_SIGNALS_DB_NAME", "newsroom_x")
    assert ndb.get_newsroom_db().name == "newsagent_x"
    sources = ndb.get_item_sources()
    assert sources.articles.database.name == "newsagent_x"
    assert (sources.signals.database.name, sources.signals.name) == ("newsroom_x", settings.NEWSROOM_SIGNALS_COLLECTION)
