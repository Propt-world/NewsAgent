"""Real-estate check: the step, the pipeline wiring, the scheduler result, "process anyway", the trial."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pymongo import MongoClient

from src.configs.settings import settings
from src.graph.nodes import check_real_estate as rec
from src.graph.nodes import notify_webhook as notify_webhook_node
from src.models.AgentPromptsModel import AgentPromptsModel
from src.models.ArticleModel import ArticleModel
from src.models.InvokeRequest import InvokeRequest
from src.models.MainWorkflowState import MainWorkflowState

REPO_ROOT = Path(__file__).resolve().parents[1]
ICC = "WASHINGTON: The United States imposed sanctions on the International Criminal Court (ICC)..."


class FakeCheckModel:
    """Says "not real estate" for the ICC story, "real estate" otherwise; records prompts and temperature use."""

    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def with_structured_output(self, schema):
        model = self

        class Structured:
            async def ainvoke(self, messages, config=None):
                model.messages.append(messages)
                if model.fail:
                    raise RuntimeError("openai down")
                about_icc = "Criminal Court" in messages[1][1]
                return schema(is_real_estate=not about_icc,
                              reason="International politics, no property angle." if about_icc else "Property market news.")
        return Structured()


def _state(title="US imposes sanctions on International Criminal Court", text=ICC, check=True, prompts=None):
    return MainWorkflowState(source_url="https://www.qatar-tribune.com/article/258402/latest-news/x",
                             cleaned_article_text=text, check_real_estate=check,
                             news_article=ArticleModel(title=title, content=text),
                             active_prompts=prompts)


@pytest.fixture
def model(monkeypatch):
    fake = FakeCheckModel()
    monkeypatch.setattr(rec, "get_check_model", lambda: fake)
    monkeypatch.setattr(settings, "REAL_ESTATE_CHECK_ENABLED", "true")
    return fake


def test_off_topic_article_is_skipped(model):
    state = asyncio.run(rec.check_real_estate(_state()))
    assert state.real_estate_check["is_real_estate"] is False
    assert state.real_estate_check["reason"] == "International politics, no property angle."
    assert rec.route_after_check(state) == "skip" and rec.is_skipped(state.model_dump())
    # Only the title and the start of the text are sent
    assert "Criminal Court" in model.messages[0][1][1] and "keep" not in model.messages[0][1][1]


def test_real_estate_article_continues(model):
    state = asyncio.run(rec.check_real_estate(_state(title="Qatar's real estate trading tops QR508mn",
                                                     text="Real estate trading volume in Qatar...")))
    assert state.real_estate_check["is_real_estate"] is True
    assert rec.route_after_check(state) == "continue"


def test_check_does_not_run_when_off_or_not_asked(model, monkeypatch):
    assert asyncio.run(rec.check_real_estate(_state(check=False))).real_estate_check is None  # manual submission
    monkeypatch.setattr(settings, "REAL_ESTATE_CHECK_ENABLED", "false")
    assert asyncio.run(rec.check_real_estate(_state())).real_estate_check is None  # switched off
    assert model.messages == []


def test_a_failed_check_keeps_the_article(monkeypatch):
    monkeypatch.setattr(rec, "get_check_model", lambda: FakeCheckModel(fail=True))
    monkeypatch.setattr(settings, "REAL_ESTATE_CHECK_ENABLED", "true")
    state = asyncio.run(rec.check_real_estate(_state()))
    assert state.real_estate_check is None and state.error_message is None
    assert rec.route_after_check(state) == "continue"


def test_registry_prompts_are_used_when_present(model):
    prompts = AgentPromptsModel.model_construct(real_estate_check_system="REGISTRY SYSTEM",
                                                real_estate_check_user="T={title} X={text}")
    asyncio.run(rec.check_real_estate(_state(prompts=prompts)))
    system, user = model.messages[0]
    assert system == ("system", "REGISTRY SYSTEM") and user[1].startswith("T=US imposes")


def test_text_is_cut_to_the_setting(model, monkeypatch):
    monkeypatch.setattr(settings, "REAL_ESTATE_CHECK_CHARS", 20)
    asyncio.run(rec.check_real_estate(_state(text="x" * 5000)))
    assert "x" * 21 not in model.messages[0][1][1]


def test_an_earlier_error_is_never_reported_as_skipped():
    state = _state().model_copy(update={"error_message": "fetch failed",
                                        "real_estate_check": {"is_real_estate": False}})
    assert rec.route_after_check(state) == "continue" and not rec.is_skipped(state)


def test_pipeline_checks_right_after_fetching():
    from src.graph.graph import MainWorkflow
    graph = MainWorkflow().create_workflow().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("fetch_content", "check_real_estate") in edges
    assert ("check_real_estate", "extract_links") in edges
    assert ("check_real_estate", "report_not_real_estate") in edges
    assert ("fetch_content", "extract_links") not in edges


def test_report_node_tells_the_scheduler(monkeypatch):
    sent = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update(json)

            class Resp:
                status_code, text = 200, "ok"
            return Resp()

    monkeypatch.setattr(settings, "WEBHOOK_URL", "http://scheduler.test/webhook/store-result")
    monkeypatch.setattr(notify_webhook_node.httpx, "AsyncClient", FakeClient)
    state = _state().model_copy(update={"real_estate_check": {"is_real_estate": False, "reason": "Politics."}})
    asyncio.run(notify_webhook_node.report_not_real_estate(state))
    assert sent == {"source_url": state.source_url, "status": "not_real_estate",
                    "title": "US imposes sanctions on International Criminal Court",
                    "real_estate_check": {"is_real_estate": False, "reason": "Politics."}}


def test_manual_submissions_are_not_checked_by_default():
    assert InvokeRequest(source_url="https://x.test/a").check_real_estate is False


def test_trial_script_reports_what_would_be_lost_and_saved(model, tmp_path, monkeypatch):
    from src.scripts import real_estate_check_trial as trial
    articles = [
        {"_id": "a1", "status": "approved", "final_output": {"title": "Aldar sells out", "content": "Aldar sold homes."}},
        {"_id": "r1", "status": "rejected", "final_output": {"title": "ICC sanctions", "content": ICC}},
        {"_id": "r2", "status": "rejected", "final_output": {"title": "Duplicate tower story", "content": "Tower."}},
    ]
    export = tmp_path / "export.json"
    export.write_text(json.dumps(articles))
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["trial", "--articles", str(export), "--out", str(out)])
    assert trial.main() == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["approved"] == {"checked": 1, "kept": 1, "skipped": 0, "skipped_share": 0.0}
    assert summary["rejected"]["skipped"] == 1 and summary["rejected"]["kept"] == 1
    assert summary["run"]["checked"] == 3
    assert trial.main() == 0  # a second run reuses the saved checks
    assert json.loads((out / "summary.json").read_text())["run"]["already_checked"] == 3
    assert len(model.messages) == 3


# --- Database: prompts seeding and the scheduler (need TEST_MONGO_URL) -----------------------

@pytest.fixture
def mongo():
    url = os.getenv("TEST_MONGO_URL")
    if not url:
        pytest.skip("TEST_MONGO_URL not set; skipping MongoDB tests")
    name = f"recheck_test_{uuid.uuid4().hex[:12]}"
    client = MongoClient(url)
    yield url, name, client[name]
    client.drop_database(name)
    client.close()


def test_init_db_adds_the_prompts_only_when_missing(mongo):
    from src.scripts.init_db import seed_real_estate_check_prompts
    _, _, db = mongo
    seed_real_estate_check_prompts(db)
    db.prompts.update_one({"name": "real_estate_check_system"}, {"$set": {"content": "EDITED IN REGISTRY"}})
    seed_real_estate_check_prompts(db)
    assert db.prompts.find_one({"name": "real_estate_check_system"})["content"] == "EDITED IN REGISTRY"
    assert db.prompts.count_documents({}) == 2


def test_scheduler_marks_and_releases_filtered_articles(mongo):
    """The real scheduler app in a fresh process (settings come from the environment)."""
    url, name, db = mongo
    db.processed_articles.insert_many([
        {"_id": "q1", "url": "https://x.test/icc", "status": "queued", "source_id": "s1"},
        {"_id": "p1", "url": "https://x.test/done", "status": "processed", "source_id": "s1", "final_output": {"title": "Done"}},
    ])
    script = f"""
import json
from fastapi.testclient import TestClient
import src.scheduler.main as m
from src.scheduler.main import app
submitted = []
async def fake_submit(client, link, check_real_estate):
    submitted.append([link, check_real_estate])
    return len(submitted) != 2  # the second submission fails
m.submit_article_job = fake_submit
out = {{}}
with TestClient(app) as c:
    check = {{"is_real_estate": False, "reason": "Politics.", "model": "gpt-4o-mini", "checked_at": "2026-10-10T00:00:00Z"}}
    out["mark"] = c.post("/webhook/store-result", json={{"source_url": "https://x.test/icc", "status": "not_real_estate",
                                                       "title": "ICC sanctions", "real_estate_check": check}}).json()
    out["mark_processed"] = c.post("/webhook/store-result", json={{"source_url": "https://x.test/done",
                                   "status": "not_real_estate", "real_estate_check": check}}).json()
    listed = c.get("/articles", params={{"status": "not_real_estate"}}).json()["items"]
    out["filtered_list"] = [[a["_id"], (a.get("real_estate_check") or {{}}).get("reason"), bool(a.get("filtered_at"))] for a in listed]
    out["anyway_wrong_status"] = c.post("/articles/p1/process-anyway").status_code
    r = c.post("/articles/q1/process-anyway")
    out["anyway"] = [r.status_code, r.json().get("status")]
    out["anyway_again"] = c.post("/articles/q1/process-anyway").status_code
    out["submitted"] = submitted
print("RESULT=" + json.dumps(out))
"""
    env = {**os.environ, "DATABASE_URL": url, "MONGO_DB_NAME": name, "NEWSROOM_ENABLED": "false",
           "WEBHOOK_SECRET": "", "NEWSAGENT_API_KEY": ""}
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")).removeprefix("RESULT="))

    assert out["mark"]["status"] == "ok"
    assert out["mark_processed"]["status"] == "ignored"  # a processed article is never changed
    assert db.processed_articles.find_one({"_id": "p1"})["status"] == "processed"
    assert out["filtered_list"] == [["q1", "Politics.", True]]  # the reason reaches the backoffice
    assert out["anyway_wrong_status"] == 409
    assert out["anyway"] == [200, "queued"]
    assert out["submitted"][0] == ["https://x.test/icc", False]  # processed without the check
    # The second call found it queued (not filtered) and was refused, so nothing was sent twice
    assert out["anyway_again"] == 409 and len(out["submitted"]) == 1
    doc = db.processed_articles.find_one({"_id": "q1"})
    assert doc["status"] == "queued"
    assert doc["real_estate_check"]["reason"] == "Politics." and doc["real_estate_check"]["title"] == "ICC sanctions"
    assert doc["real_estate_check"]["overridden_at"]


def test_process_anyway_puts_it_back_when_queueing_fails(mongo):
    url, name, db = mongo
    db.processed_articles.insert_one({"_id": "f1", "url": "https://x.test/f", "status": "not_real_estate", "source_id": "s1",
                                      "real_estate_check": {"is_real_estate": False, "reason": "Sport."}})
    script = """
import json
from fastapi.testclient import TestClient
import src.scheduler.main as m
async def failing_submit(client, link, check_real_estate):
    return False
m.submit_article_job = failing_submit
with TestClient(m.app) as c:
    print("RESULT=" + json.dumps(c.post("/articles/f1/process-anyway").status_code))
"""
    env = {**os.environ, "DATABASE_URL": url, "MONGO_DB_NAME": name, "NEWSROOM_ENABLED": "false", "NEWSAGENT_API_KEY": ""}
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    assert next(l for l in result.stdout.splitlines() if l.startswith("RESULT=")) == "RESULT=502"
    doc = db.processed_articles.find_one({"_id": "f1"})
    assert doc["status"] == "not_real_estate" and "overridden_at" not in doc["real_estate_check"]
