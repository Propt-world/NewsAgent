"""LLM Observability summary from Phoenix (Phoenix answers are stand-ins; no real Phoenix or AI)."""
import asyncio
import json
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


# --- Detail on demand: a job's runs and one run's steps --------------------------------------------------

TRACE = "96338b63fb836cd3832a26e835114a1a"


def _run(trace_id, at, status="OK", outcome="completed", message=None, cost=0.001):
    return {"node": {"startTime": at, "latencyMs": 4000.0, "statusCode": status, "statusMessage": message,
                     "cumulativeTokenCountTotal": 1500.0, "context": {"traceId": trace_id},
                     "output": {"value": json.dumps({"outcome": outcome, "title": "Emaar tower"})},
                     "trace": {"numSpans": 7, "costSummary": {"total": {"cost": cost}}}}}


def _span(span_id, parent, name, start, kind="CHAIN", status="OK", message=None, attributes="{}", cost=0.0,
          prompt=0, completion=0):
    return {"node": {"spanId": span_id, "parentId": parent, "name": name, "spanKind": kind, "statusCode": status,
                     "statusMessage": message, "startTime": start, "latencyMs": 100.0, "tokenCountPrompt": prompt,
                     "tokenCountCompletion": completion, "costSummary": {"total": {"cost": cost}},
                     "attributes": attributes}}


@pytest.fixture
def phoenix_detail(monkeypatch):
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006/v1/traces")
    monkeypatch.setattr(settings, "PHOENIX_REPORT_PROJECTS", None)
    asked = []
    trace_project = {"name": "newsagent/dev"}
    secret = json.dumps({"llm": {"model_name": "gpt-4o-mini", "input_messages": [{"message": {"content": "SECRET PROMPT"}}],
                                 "output_messages": [{"message": {"content": "SECRET ANSWER"}}]},
                         "input": {"value": "SECRET PROMPT"}})

    async def fake_graphql(client, query, variables):
        asked.append((query, variables))
        if query.startswith("{ projects"):
            return {"projects": {"edges": [{"node": {"id": "p0", "name": "default"}},
                                           {"node": {"id": "p1", "name": "newsagent/dev"}},
                                           {"node": {"id": "p2", "name": "social-scraper/dev"}}]}}
        if "query Runs" in query:
            return {"node": {"spans": {"edges": [
                _run("a" * 32, "2026-10-28T10:00:00+00:00"),
                _run("b" * 32, "2026-10-28T11:00:00+00:00", status="ERROR", outcome="failed",
                     message="Took longer than 180 s (attempt 3 of 3); moved to the failed list"),
                _run("c" * 32, "2026-10-28T09:00:00+00:00", outcome="retried")]}}}
        if "query Trace" in query:
            if variables["traceId"] != TRACE:
                return {"getTraceByOtelId": None}
            return {"getTraceByOtelId": {
                "traceId": TRACE, "startTime": "2026-10-28T10:00:00+00:00", "latencyMs": 4000.0, "numSpans": 4,
                "project": trace_project,
                "costSummary": {"prompt": {"cost": 0.0009, "tokens": 1800}, "completion": {"cost": 0.0003, "tokens": 200},
                                "total": {"cost": 0.0012, "tokens": 2000}},
                "spans": {"edges": [
                    _span("s3", "s2", "ChatOpenAI", "2026-10-28T10:00:01.500+00:00", kind="LLM", attributes=secret,
                          cost=0.0012, prompt=1800, completion=200),
                    _span("s1", None, "article_pipeline", "2026-10-28T10:00:00+00:00"),
                    _span("s2", "s1", "summary", "2026-10-28T10:00:01+00:00"),
                    _span("s4", "s1", "generate_seo", "2026-10-28T10:00:03+00:00", status="ERROR",
                          message="SEO step failed")]}}}
        raise AssertionError(query)

    monkeypatch.setattr(obs, "_graphql", fake_graphql)
    obs._detail_cache.clear()
    return asked, trace_project


def test_job_runs_newest_first_from_the_jobs_own_service(phoenix_detail):
    asked, _ = phoenix_detail
    r = asyncio.run(obs.job_runs("article_pipeline", 7, now=NOW))
    assert r["available"] and r["label"] == "Articles processed"
    assert [run["trace_id"][0] for run in r["runs"]] == ["b", "a", "c"]
    failed, done, retried = r["runs"]
    assert failed["status"] == "failed" and "moved to the failed list" in failed["error"]
    assert (done["status"], done["outcome"], done["cost_usd"], done["tokens"], done["steps"]) == ("ok", "completed", 0.001, 1500, 7)
    assert retried["status"] == "ok" and retried["outcome"] == "retried"  # a retried attempt is not a failure
    runs_asked = [v for q, v in asked if "query Runs" in q]
    assert [v["project"] for v in runs_asked] == ["p1"]  # only NewsAgent's project, never "default" or the scraper
    assert runs_asked[0]["filter"] == 'name == "article_pipeline" and parent_id is None'


def test_job_runs_unknown_job(phoenix_detail):
    with pytest.raises(obs.NotFound):
        asyncio.run(obs.job_runs("anything_else", 1, now=NOW))


def test_trace_steps_in_order_with_depth_and_no_prompt_or_answer_text(phoenix_detail):
    r = asyncio.run(obs.trace_steps(TRACE.upper()))
    assert r["available"] and (r["job"], r["label"], r["service"]) == ("article_pipeline", "Articles processed", "NewsAgent (dev)")
    assert (r["cost_usd"], r["tokens_prompt"], r["tokens_completion"], r["status"]) == (0.0012, 1800, 200, "ok")
    assert [(s["name"], s["depth"], s["start_ms"]) for s in r["steps"]] == [
        ("article_pipeline", 0, 0), ("summary", 1, 1000), ("ChatOpenAI", 2, 1500), ("generate_seo", 1, 3000)]
    llm = r["steps"][2]
    assert (llm["kind"], llm["model"], llm["tokens_prompt"], llm["tokens_completion"], llm["cost_usd"]) == (
        "llm", "gpt-4o-mini", 1800, 200, 0.0012)
    assert r["steps"][3]["status"] == "failed" and r["steps"][3]["error"] == "SEO step failed"
    assert "SECRET" not in json.dumps(r)  # prompts and answers stay in Phoenix


def test_trace_steps_refuses_bad_unknown_and_unreported_traces(phoenix_detail):
    _, trace_project = phoenix_detail
    for bad in ("not-a-trace", "z" * 32, "b" * 32):  # not an id, not hex, not in Phoenix
        with pytest.raises(obs.NotFound):
            asyncio.run(obs.trace_steps(bad))
    trace_project["name"] = "default"  # a project the page does not report on
    with pytest.raises(obs.NotFound):
        asyncio.run(obs.trace_steps(TRACE))


def test_detail_is_cached_for_a_minute(phoenix_detail):
    asked, _ = phoenix_detail
    first = asyncio.run(obs.trace_steps(TRACE))
    count = len(asked)
    assert asyncio.run(obs.trace_steps(TRACE)) is first and len(asked) == count


def test_detail_endpoints_answer_404_for_unknown_items(phoenix_detail):
    from fastapi import HTTPException
    from src.scheduler.main import observability_job_runs, observability_trace

    for call in (observability_job_runs("nope", 1, 20), observability_trace("not-a-trace")):
        with pytest.raises(HTTPException) as e:
            asyncio.run(call)
        assert e.value.status_code == 404
