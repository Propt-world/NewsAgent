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
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_SIGNALS_DB_NAME": db_name,
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
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_SIGNALS_DB_NAME": db_name,
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


def test_article_list_loads_older_items_by_date(mongo_url, db_name, sync_db):
    """'Load more' asks for articles discovered at or before the last one shown, so new arrivals cannot shift it."""
    script = f"""
import json
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient
db = MongoClient({mongo_url!r})[{db_name!r}]
base = datetime(2026, 10, 1, tzinfo=timezone.utc)
for n in range(5):
    db.processed_articles.insert_one({{"_id": f"a{{n}}", "source_id": "feed", "url": f"https://example.com/{{n}}",
        "status": "processed", "discovered_at": base + timedelta(hours=n), "final_output": {{"title": f"T{{n}}"}}}})
from src.scheduler.main import app
with TestClient(app) as c:
    first = c.get("/articles", params={{"status": "processed", "limit": 2}}).json()
    # A new article arrives before "load more"
    db.processed_articles.insert_one({{"_id": "new", "source_id": "feed", "url": "https://example.com/new",
        "status": "processed", "discovered_at": base + timedelta(hours=9), "final_output": {{"title": "New"}}}})
    last = first["items"][-1]["discovered_at"]
    more = c.get("/articles", params={{"status": "processed", "limit": 2, "discovered_before": last}}).json()
print("RESULT=" + json.dumps({{"first": [i["_id"] for i in first["items"]], "more": [i["_id"] for i in more["items"]],
                              "total": more["total"]}}))
"""
    env = {k: v for k, v in os.environ.items()}
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_SIGNALS_DB_NAME": db_name,
                "NEWSROOM_ENABLED": "false", "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""})
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")).removeprefix("RESULT="))
    assert out["first"] == ["a4", "a3"]
    # The boundary item comes back once (the screen drops repeats); the new arrival never shifts the list.
    assert out["more"] == ["a3", "a2"]
    assert out["total"] == 6


def test_article_text_edit_endpoints(mongo_url, db_name, sync_db):
    """The real scheduler app: saving an edited why this matters (no AI step needed) and the refusals."""
    script = f"""
import json
from fastapi.testclient import TestClient
from pymongo import MongoClient
db = MongoClient({mongo_url!r})[{db_name!r}]
db.processed_articles.insert_one({{"_id": "art-1", "source_id": "src-1", "url": "https://example.com/1", "status": "processed",
    "final_output": {{"title": "Emaar launches", "title_ar": "إعمار تطلق", "content": "text", "summary": "s",
                      "summary_ar": "م", "why_this_matters": {{"heading": "Why this matters", "content": "old", "content_ar": "قديم"}}}}}})
from src.scheduler.main import app
out = {{}}
with TestClient(app) as c:
    ok = c.patch("/articles/art-1/text", json={{"why_this_matters": "new", "why_this_matters_ar": "جديد"}})
    out["ok"] = [ok.status_code, ok.json()]
    out["stored"] = db.processed_articles.find_one({{"_id": "art-1"}})["final_output"]["why_this_matters"]
    out["no_arabic"] = c.patch("/articles/art-1/text", json={{"summary": "changed"}}).status_code
    out["missing"] = c.post("/articles/nope/translate", json={{"title": "x"}}).status_code
    out["nothing"] = c.post("/articles/art-1/translate", json={{}}).status_code
    t = c.patch("/articles/art-1/title", json={{"title": "Emaar sells out!"}}, headers={{"X-Newsroom-User": "ann@propt.global"}})
    out["title"] = [t.status_code, t.json(), db.processed_articles.find_one({{"_id": "art-1"}})["final_output"]["seo"]]
    out["empty_title"] = c.patch("/articles/art-1/title", json={{"title": " "}}).status_code
    # Two reviewers: both opened version 2 (after the two edits above); the second save is refused
    v = db.processed_articles.find_one({{"_id": "art-1"}})["version"]
    first = c.patch("/articles/art-1/title", json={{"title": "First wins", "expected_version": v}}, headers={{"X-Newsroom-User": "ann@propt.global"}})
    second = c.patch("/articles/art-1/text", json={{"why_this_matters": "late", "why_this_matters_ar": "متأخر", "expected_version": v}},
                     headers={{"X-Newsroom-User": "bob@propt.global"}})
    out["conflict"] = [first.status_code, second.status_code, second.json()["detail"]]
    s = c.patch("/articles/art-1/status", json={{"status": "rejected"}}, headers={{"X-Newsroom-User": "bob@propt.global"}})
    stored = db.processed_articles.find_one({{"_id": "art-1"}})
    out["status"] = [s.status_code, stored["status_changed_by"], bool(stored.get("status_changed_at"))]
    out["history"] = [(h["action"], h["by"], h["before"], h["after"]) for h in c.get("/articles/art-1/history").json()["items"]]
    read = c.get("/articles/art-1").json()  # screens read the version from here to send it back
    out["read"] = [read["version"], read["edited_by"], read["status_changed_by"]]
print("RESULT=" + json.dumps(out, ensure_ascii=False))
"""
    env = {k: v for k, v in os.environ.items()}
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_SIGNALS_DB_NAME": db_name,
                "NEWSROOM_ENABLED": "true", "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""})
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")).removeprefix("RESULT="))
    assert out["ok"] == [200, {"status": "updated", "id": "art-1", "updated": ["final_output.why_this_matters"],
                               "regenerated": []}]
    assert (out["stored"]["content"], out["stored"]["content_ar"], out["stored"]["heading"]) == ("new", "جديد", "Why this matters")
    assert (out["no_arabic"], out["missing"], out["nothing"]) == (422, 404, 422)
    assert out["title"] == [200, {"status": "updated", "id": "art-1", "new_title": "Emaar sells out!",
                                  "new_slug": "emaar-sells-out"}, {"slug": "emaar-sells-out"}]
    assert out["empty_title"] == 400  # as before
    assert out["conflict"][:2] == [200, 409] and "changed by ann@propt.global" in out["conflict"][2]
    assert out["status"] == [200, "bob@propt.global", True]
    assert out["read"] == [3, "ann@propt.global", "bob@propt.global"]
    actions = [(a, by) for a, by, _, _ in out["history"]]
    assert actions == [("status", "bob@propt.global"), ("title", "ann@propt.global"), ("title", "ann@propt.global"),
                       ("text", "unknown")]
    assert out["history"][0][2:] == [{"status": "processed"}, {"status": "rejected"}]
    assert out["history"][3][2] == {"why_this_matters": "old", "why_this_matters_ar": "قديم"}



def test_dashboard_article_counts(mongo_url, db_name, sync_db):
    """The real scheduler app: pending and approved in all, approved / rejected in the window."""
    script = f"""
import json
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient
db = MongoClient({mongo_url!r})[{db_name!r}]
now = datetime.now(timezone.utc)
rows = [("p1", "processed", None), ("p2", "processed", None), ("a1", "approved", now - timedelta(hours=2)),
        ("a2", "approved", now - timedelta(hours=30)), ("a3", "approved", None), ("r1", "rejected", now - timedelta(hours=1))]
for i, status, changed in rows:
    db.processed_articles.insert_one({{"_id": i, "source_id": "s", "url": f"https://example.com/{{i}}", "status": status,
                                       **({{"status_changed_at": changed}} if changed else {{}})}})
from src.scheduler.main import app
with TestClient(app) as c:
    since = (now - timedelta(hours=24)).isoformat()
    ok = c.get("/stats/articles", params={{"since": since, "until": now.isoformat()}})
    naive = c.get("/stats/articles", params={{"since": "2026-10-22T10:00:00", "until": "2026-10-23T10:00:00"}})
print("RESULT=" + json.dumps({{"ok": [ok.status_code, {{k: v for k, v in ok.json().items() if k not in ("since", "until")}}],
                               "naive": naive.status_code}}))
"""
    env = {k: v for k, v in os.environ.items()}
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_SIGNALS_DB_NAME": db_name,
                "NEWSROOM_ENABLED": "true", "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""})
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")).removeprefix("RESULT="))
    assert out["ok"] == [200, {"pending": 2, "total_approved": 3, "approved": 1, "rejected": 1}]
    assert out["naive"] == 422  # a time without a time zone is refused (PKT vs UTC mix-ups)



def test_filtered_out_list_can_show_the_last_days_only(mongo_url, db_name, sync_db):
    """The real scheduler app: filtered_since hides older filtered-out articles; their records stay."""
    script = f"""
import json
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient
db = MongoClient({mongo_url!r})[{db_name!r}]
now = datetime.now(timezone.utc)
for i, days in (("new", 2), ("old", 40)):
    db.processed_articles.insert_one({{"_id": i, "source_id": "s", "url": f"https://example.com/{{i}}",
        "status": "not_real_estate", "filtered_at": now - timedelta(days=days), "discovered_at": now - timedelta(days=days)}})
from src.scheduler.main import app
with TestClient(app) as c:
    recent = c.get("/articles", params={{"status": "not_real_estate", "filtered_since": (now - timedelta(days=30)).isoformat()}}).json()
    every = c.get("/articles", params={{"status": "not_real_estate"}}).json()
print("RESULT=" + json.dumps({{"recent": [recent["total"], [a["_id"] for a in recent["items"]]], "every": every["total"],
                               "kept": db.processed_articles.count_documents({{"status": "not_real_estate"}})}}))
"""
    env = {k: v for k, v in os.environ.items()}
    env.update({"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_SIGNALS_DB_NAME": db_name,
                "NEWSROOM_ENABLED": "true", "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""})
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")).removeprefix("RESULT="))
    assert out["recent"] == [1, ["new"]]
    assert (out["every"], out["kept"]) == (2, 2)  # nothing deleted: the old one still blocks reprocessing
