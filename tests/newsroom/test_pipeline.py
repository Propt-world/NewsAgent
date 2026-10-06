"""The article pipeline hooks: country step names, webhook payload, scheduler wiring."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.configs.settings import settings
from src.graph.nodes import extract_country as extract_country_node
from src.graph.nodes import notify_webhook as notify_webhook_node
from src.models.AgentPromptsModel import AgentPromptsModel
from src.models.ArticleModel import ArticleModel
from src.models.CountryExtractionModel import CountryAndNamesExtractionModel, CountryExtractionModel
from src.models.MainWorkflowState import MainWorkflowState
from src.newsroom.pipeline import split_names

REPO_ROOT = Path(__file__).resolve().parents[2]


def _state(**extra):
    prompts = AgentPromptsModel.model_construct(
        country_extraction_system="system", country_extraction_user="{title} {summary} {content}")
    article = ArticleModel(title="Emaar launches in Dubai", content="text", summary="Emaar launched a tower in Dubai.")
    return MainWorkflowState(source_url="https://example.com/a", cleaned_article_text="text",
                             news_article=article, active_prompts=prompts, **extra)


class _FakeLLM:
    def __init__(self, calls):
        self.calls = calls

    def with_structured_output(self, output_model):
        self.calls.append(output_model)
        llm = self

        class _Structured:
            async def ainvoke(self, messages):
                if output_model is CountryAndNamesExtractionModel:
                    return output_model(countries=["United Arab Emirates"], companies=["Emaar"],
                                        projects=[], places=["Dubai"])
                return output_model(countries=["United Arab Emirates"])

        return _Structured()


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_country_step_names_only_when_enabled(monkeypatch, enabled):
    calls = []
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", enabled)
    monkeypatch.setattr(type(settings), "get_model", lambda self: _FakeLLM(calls))
    state = asyncio.run(extract_country_node.extract_country(_state()))
    assert state.news_article.countries == ["United Arab Emirates"]
    if enabled == "true":
        assert calls == [CountryAndNamesExtractionModel]
        assert state.newsroom_names == {"companies": ["Emaar"], "projects": [], "places": ["Dubai"]}
    else:
        assert calls == [CountryExtractionModel]
        assert state.newsroom_names is None


@pytest.mark.parametrize("names", [None, {"companies": ["Emaar"], "projects": [], "places": ["Dubai"]}])
def test_webhook_payload_carries_names_beside_the_article(monkeypatch, names):
    sent = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update(json)

            class _Resp:
                status_code = 200
                text = "ok"
            return _Resp()

    monkeypatch.setattr(settings, "WEBHOOK_URL", "http://scheduler.test/webhook/store-result")
    monkeypatch.setattr(notify_webhook_node.httpx, "AsyncClient", _FakeClient)
    asyncio.run(notify_webhook_node.notify_webhook(_state(newsroom_names=names)))
    assert "newsroom_names" not in sent["data"]  # the stored article stays unchanged
    if names is None:
        assert "newsroom_names" not in sent
    else:
        assert sent["newsroom_names"] == names


def test_split_names():
    orgs, places = split_names({"companies": ["Emaar", " ", 5], "projects": ["Burj Khalifa"], "places": ["Dubai"]})
    assert orgs == ["Emaar", "Burj Khalifa"]
    assert places == ["Dubai"]
    assert split_names("not a dict") == ([], [])
    assert len(split_names({"companies": ["x"] * 80})[0]) == 50


def test_scheduler_webhook_and_status_hooks_end_to_end(mongo_url, db_name, sync_db):
    """Runs the real scheduler app in a fresh process against the throwaway database."""
    script = f"""
import json
from fastapi.testclient import TestClient
from pymongo import MongoClient
from src.newsroom.seed import apply_plan, build_plan
db = MongoClient({mongo_url!r})[{db_name!r}]
apply_plan(build_plan(), db)
from src.scheduler.main import app
url = "https://example.com/emaar-dubai"
out = {{}}
with TestClient(app) as c:
    r = c.post("/webhook/store-result", json={{
        "source_url": url, "status": "success",
        "data": {{"title": "Emaar launches in Dubai", "summary": "Emaar launched a tower."}},
        "newsroom_names": {{"companies": ["Emaar", "Wasl"], "projects": [], "places": ["Dubai", "Brand New Place"]}},
    }})
    out["webhook"] = r.status_code
    article = db.processed_articles.find_one({{"url": url}})
    out["stored_keys"] = sorted(article["final_output"].keys())
    out["suggestions"] = sorted(s["name"] for s in db.entity_suggestions.find({{"source_id": article["_id"]}}))
    emaar = db.entity_suggestions.find_one({{"source_id": article["_id"], "name": "Emaar"}})
    c.post(f"/newsroom/items/article/{{article['_id']}}/links", json={{"entity_id": emaar["entity_id"]}})
    r = c.patch(f"/articles/{{article['_id']}}/status", json={{"status": "rejected"}})
    out["status"] = r.status_code
    out["item_status"] = db.entity_mentions.find_one({{"source_id": article["_id"]}})["item_status"]
print("RESULT=" + json.dumps(out))
"""
    env = {k: v for k, v in os.environ.items()}
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_DB_NAME": db_name,
                "NEWSROOM_ENABLED": "true", "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""})
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    line = next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))
    out = json.loads(line.removeprefix("RESULT="))
    assert out["webhook"] == 200
    assert out["stored_keys"] == ["summary", "title"]  # names are not stored on the article
    assert out["suggestions"] == ["Brand New Place", "Dubai", "Emaar"]  # "Wasl" is ambiguous: no suggestion
    assert out["status"] == 200
    assert out["item_status"] == "rejected"
