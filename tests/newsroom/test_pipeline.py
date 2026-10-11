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
    c.patch(f"/articles/{{article['_id']}}/title", json={{"title": "Edited title"}})
    out["title_after_edit"] = db.entity_mentions.find_one({{"source_id": article["_id"]}})["title"]
    r = c.patch(f"/articles/{{article['_id']}}/status", json={{"status": "rejected"}})
    out["status"] = r.status_code
    out["item_status"] = db.entity_mentions.find_one({{"source_id": article["_id"]}})["item_status"]
    # Processed again (same url): links follow the new text and status.
    c.post("/webhook/store-result", json={{"source_url": url, "status": "success",
        "data": {{"title": "Reprocessed title", "summary": "New summary."}}}})
    mention = db.entity_mentions.find_one({{"source_id": article["_id"]}})
    out["after_reprocess"] = [mention["title"], mention["item_status"]]
    # A Propt Newsroom article without an image cannot be approved
    db.processed_articles.insert_one({{"_id": "composed-1", "url": "newsroom://compose/run-1", "status": "processed",
        "source_id": "newsroom_compose", "final_output": {{"title": "Composed", "top_image": None}}}})
    r = c.patch("/articles/composed-1/status", json={{"status": "approved"}})
    out["composed_approve"] = [r.status_code, r.json().get("detail"), db.processed_articles.find_one({{"_id": "composed-1"}})["status"]]
    db.processed_articles.update_one({{"_id": "composed-1"}}, {{"$set": {{"generated_from": "run-1"}}}})
    out["api_generated_from"] = [c.get("/articles/composed-1").json().get("generated_from"),
                                 c.get(f"/articles/{{article['_id']}}").json().get("generated_from")]
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
    assert out["title_after_edit"] == "Edited title"
    assert out["after_reprocess"] == ["Reprocessed title", "processed"]
    assert out["composed_approve"] == [422, "Add an image before approving this Propt Newsroom article.", "processed"]
    assert out["api_generated_from"] == ["run-1", None]  # the compose job id reaches the backoffice


def test_approval_needs_an_image_only_for_composed_articles():
    from src.newsroom.compose import NEEDS_IMAGE, approval_blocked_reason
    composed = {"source_id": "newsroom_compose", "final_output": {"title": "x"}}
    assert approval_blocked_reason(composed) == NEEDS_IMAGE
    assert approval_blocked_reason({**composed, "final_output": {"top_image": ""}}) == NEEDS_IMAGE
    assert approval_blocked_reason({**composed, "final_output": {"top_image": "https://img.test/a.jpg"}}) is None
    assert approval_blocked_reason({"source_id": "zawya", "final_output": {}}) is None  # scraped: unchanged
    assert approval_blocked_reason(None) is None  # missing: the endpoint's own 404 handles it


# --- Public links for composed articles; archived and deleted articles ---------------------

def test_public_link_is_built_like_the_app():
    from src.newsroom.public_links import public_article_url, shown_url, slugify
    assert slugify("Mashreq Expo City off-plan financing") == "mashreq-expo-city-off-plan-financing"
    assert slugify("  Emaar's Q3: AED 1.2bn sales — up 20%!  ") == "emaars-q3-aed-12bn-sales-up-20"
    assert public_article_url("abc", "إعمار") == "https://propt.global/news/article/abc"  # no Latin letters
    composed = {"_id": "a1", "url": "newsroom://compose/run-1", "source_id": "newsroom_compose",
                "final_output": {"title": "Emaar and DAMAC in Dubai"}}
    assert shown_url(composed) == "https://propt.global/news/emaar-and-damac-in-dubai/a1"
    assert shown_url({"_id": "a2", "url": "https://gulfnews.com/x", "source_id": "gulf"}) == "https://gulfnews.com/x"


def test_approval_chunks_carry_the_public_link_for_composed_articles_only():
    from src.utils.loader import normalize_document
    composed = {"_id": "a1", "url": "newsroom://compose/run-1", "source_id": "newsroom_compose",
                "final_output": {"title": "Emaar and DAMAC in Dubai", "content": "x"}}
    scraped = {"_id": "a2", "url": "https://gulfnews.com/x", "source_id": "gulf", "final_output": {"title": "T"}}
    assert normalize_document(composed)["url"] == "https://propt.global/news/emaar-and-damac-in-dubai/a1"
    assert normalize_document(scraped)["url"] == "https://gulfnews.com/x"  # unchanged


def test_archive_keeps_links_and_delete_hides_them(mongo_url, db_name, sync_db):
    """The real scheduler app: Archive and Delete tell the Newsroom (fresh process, throwaway database)."""
    script = f"""
import json
from fastapi.testclient import TestClient
from pymongo import MongoClient
from src.newsroom.seed import apply_plan, build_plan, seed_entity_id
db = MongoClient({mongo_url!r})[{db_name!r}]
apply_plan(build_plan(), db)
from src.scheduler.main import app
emaar = seed_entity_id("organisation", "Emaar Properties")
out = {{}}
with TestClient(app) as c:
    for n in (1, 2, 3):
        db.processed_articles.insert_one({{"_id": f"art-{{n}}", "url": f"https://example.com/{{n}}", "status": "approved",
            "processed_at": None, "final_output": {{"title": f"Emaar story {{n}}", "summary": "s"}}}})
        c.post(f"/newsroom/items/article/art-{{n}}/links", json={{"entity_id": emaar}})
    out["archive"] = c.post("/articles/art-1/archive").status_code
    out["delete"] = c.delete("/articles/art-2").status_code
    out["statuses"] = {{m["source_id"]: m["item_status"] for m in db.entity_mentions.find({{"entity_id": emaar}})}}
    history = c.get(f"/newsroom/entities/{{emaar}}/history").json()
    out["history"] = sorted((i["source_id"], i.get("item_status")) for i in history["items"])
    from src.scheduler.main import scheduler
    job = scheduler.get_job("newsroom_nightly_repair")
    out["nightly"] = str(job.trigger) if job else None
print("RESULT=" + json.dumps(out))
"""
    env = {k: v for k, v in os.environ.items()}
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_DB_NAME": db_name,
                "NEWSROOM_ENABLED": "true", "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""})
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")).removeprefix("RESULT="))
    assert (out["archive"], out["delete"]) == (200, 200)
    assert out["statuses"] == {"art-1": "archived", "art-2": "deleted", "art-3": "approved"}
    # Archived stays in the company's history (labelled); deleted is gone.
    assert out["history"] == [["art-1", "archived"], ["art-3", "approved"]]
    assert out["nightly"] == "cron[hour='3', minute='0']"  # the link repair, every night (UAE time)
