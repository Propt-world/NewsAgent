"""LLM Observability summary from Phoenix (Phoenix answers are stand-ins; no real Phoenix or AI)."""
import asyncio
from datetime import datetime, timezone

import pytest

from src.configs.settings import settings
from src.utils import observability as obs

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def test_where_phoenix_is_read():
    for collector, url, expected in [("http://phoenix:6006/v1/traces", None, "http://phoenix:6006"),
                                     ("http://phoenix:6006/v1/traces", "http://reader:6006/", "http://reader:6006"),
                                     ("", None, None)]:
        settings.PHOENIX_COLLECTOR_ENDPOINT, settings.PHOENIX_URL = collector, url
        assert obs.phoenix_url() == expected
    settings.PHOENIX_COLLECTOR_ENDPOINT = settings.PHOENIX_URL = None


def test_off_says_so(monkeypatch):
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", "")
    monkeypatch.setattr(settings, "PHOENIX_URL", None)
    r = asyncio.run(obs.summary(1, now=NOW))
    assert r["available"] is False and "not switched on" in r["message"]


def _project(jobs, failed, calls, cost, prompt, completion, models=(), failures=()):
    return {"jobs": jobs, "failed": failed, "calls": calls, "p50": 900.0, "p90": 4000.0,
            "cost": {"prompt": {"cost": 0, "tokens": prompt}, "completion": {"cost": 0, "tokens": completion},
                     "total": {"cost": cost, "tokens": prompt + completion}},
            "models": list(models), "failures": {"edges": [{"node": f} for f in failures]}}


@pytest.fixture
def phoenix(monkeypatch):
    """A stand-in Phoenix with two services."""
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006/v1/traces")
    monkeypatch.setattr(settings, "PHOENIX_REPORT_PROJECTS", None)
    asked = []
    projects = {
        "p1": _project(3, 1, 9, 0.003, 9000, 1500,
                       models=[{"name": "gpt-4o-mini", "costSummary": {"prompt": {"tokens": 9000}, "completion": {"tokens": 1500},
                                                                       "total": {"cost": 0.003}}}],
                       failures=[{"name": "article_pipeline", "startTime": "2026-10-28T10:00:00+00:00",
                                  "statusMessage": "Summary failed the checks", "context": {"traceId": "t1"}}]),
        "p2": _project(1, 0, 2, 0.002, 1200, 300,
                       models=[{"name": "x-ai/grok-4.3", "costSummary": {"prompt": {"tokens": 1200}, "completion": {"tokens": 300},
                                                                         "total": {"cost": 0.00225}}}]),
    }
    jobs = {"article_pipeline": {"count": 2, "failed": 1, "p50": 800.0, "calls": 6, "cost": {"total": {"cost": 0.002, "tokens": 7000}}},
            "compose_compose": {"count": 1, "failed": 0, "p50": 9000.0, "calls": 3, "cost": {"total": {"cost": 0.001, "tokens": 3500}}},
            "grok_scout": {"count": 1, "failed": 0, "p50": 30000.0, "calls": 2, "cost": {"total": {"cost": 0.002, "tokens": 1500}}}}

    async def fake_graphql(client, query, variables):
        asked.append(query)
        if query.startswith("{ projects"):
            return {"projects": {"edges": [{"node": {"id": "p0", "name": "default"}}, {"node": {"id": "p1", "name": "newsagent/dev"}},
                                           {"node": {"id": "p2", "name": "social-scraper/dev"}}]}}
        if "query Project" in query:
            return {"node": dict(projects[variables["project"]])}
        node = {}
        for i, name in enumerate([n for n in obs.JOBS if f'== \\"{n}\\" and parent_id' in query]):
            for key, value in jobs.get(name, {"count": 0}).items():
                node[f"j{i}_{key}"] = value
        return {"node": node}

    monkeypatch.setattr(obs, "_graphql", fake_graphql)
    obs._cache.clear()
    return asked


def test_summary_per_service_model_and_job(phoenix):
    r = asyncio.run(obs.summary(7, now=NOW))
    assert r["available"] and r["days"] == 7
    assert r["totals"] == {"jobs": 4, "failed_jobs": 1, "ai_calls": 11, "tokens_prompt": 10200,
                           "tokens_completion": 1800, "cost_usd": 0.005}
    assert [s["label"] for s in r["services"]] == ["NewsAgent (dev)", "Social scraper (dev)"]  # "default" left out
    assert [m["model"] for m in r["models"]] == ["gpt-4o-mini", "x-ai/grok-4.3"]  # most expensive first
    jobs = {j["job"]: j for j in r["jobs"]}
    assert jobs["article_pipeline"]["label"] == "Articles processed" and jobs["article_pipeline"]["avg_cost_usd"] == 0.001
    assert jobs["grok_scout"]["service"] == "Social scraper (dev)"
    assert r["failures"] == [{"job": "article_pipeline", "label": "Articles processed", "service": "NewsAgent (dev)",
                              "at": "2026-10-28T10:00:00+00:00", "error": "Summary failed the checks", "trace_id": "t1"}]
    # Each service is asked only about its own jobs, at most 9 per request (Phoenix allows 50 named fields)
    jobs_asked = [q for q in phoenix if q.startswith("query Jobs")]
    assert all(q.count("_count:") <= obs.JOBS_PER_REQUEST for q in jobs_asked)
    assert not any("grok_scout" in q for q in jobs_asked if "article_pipeline" in q)


def test_phoenix_problems_are_shown_not_raised(monkeypatch):
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006/v1/traces")

    async def down(client, query, variables):
        raise obs.PhoenixUnavailable("Phoenix could not be reached: connection refused")
    monkeypatch.setattr(obs, "_graphql", down)
    r = asyncio.run(obs.summary(1, now=NOW))
    assert r["available"] is False and "could not be reached" in r["message"]


def test_cached_for_a_minute(phoenix):
    first = asyncio.run(obs.summary(1))
    asked = len(phoenix)
    assert asyncio.run(obs.summary(1)) is first and len(phoenix) == asked  # no new questions to Phoenix
