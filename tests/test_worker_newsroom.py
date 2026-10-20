"""
The worker's Newsroom additions on top of production's worker (SIGTERM handling unchanged):
the compose listener and the real-estate "skipped" result. In-memory Redis, fake jobs, real signals.
"""
import asyncio
import json
import os
import signal

import fakeredis
import pytest

from src import worker
from src.configs.settings import settings
from src.newsroom import compose_worker

ARTICLES, COMPOSE = "test_articles", "test_compose"


@pytest.fixture
def redis_server(monkeypatch):
    """Every redis.from_url() in the worker connects to the same in-memory server."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(worker.redis, "from_url",
                        lambda *a, **k: fakeredis.FakeAsyncRedis(server=server, decode_responses=True))
    monkeypatch.setattr(worker, "COMPOSE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "REDIS_QUEUE_NAME", ARTICLES)
    monkeypatch.setattr(settings, "REDIS_DLQ_NAME", "test_dlq")
    monkeypatch.setattr(settings, "NEWSROOM_COMPOSE_QUEUE", COMPOSE)
    monkeypatch.setattr(settings, "NEWSROOM_COMPOSE_CONCURRENT", 2)
    monkeypatch.setattr(settings, "OPIK_API_KEY", "")
    return lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


def _compose_job(n):
    return json.dumps({"run_id": f"run-{n}"})


def _fake_compose(monkeypatch, started, finished, seconds=10):
    """run_compose_job stand-in: works for `seconds`; when cancelled (shutdown) it asks to be handed back."""
    async def fake_run(run_id):
        started.append(run_id)
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return compose_worker.REQUEUE
        finished.append(run_id)
    monkeypatch.setattr(compose_worker, "run_compose_job", fake_run)


# --- The compose listener -------------------------------------------------------------

def test_compose_takes_jobs_only_when_a_slot_is_free_and_hands_back_on_shutdown(redis_server, monkeypatch):
    started, finished = [], []
    _fake_compose(monkeypatch, started, finished)

    async def scenario():
        r = redis_server()
        for n in (1, 2, 3):
            await r.rpush(COMPOSE, _compose_job(n))
        shutdown = asyncio.Event()
        listener = asyncio.create_task(worker.compose_loop(shutdown))
        await asyncio.sleep(0.3)
        waiting = await r.lrange(COMPOSE, 0, -1)
        shutdown.set()
        await asyncio.wait_for(listener, timeout=3)
        return waiting, await r.lrange(COMPOSE, 0, -1)

    waiting, after = asyncio.run(scenario())
    assert started == ["run-1", "run-2"]
    assert waiting == [_compose_job(3)]  # the third stays in Redis, not in the worker's memory
    assert sorted(after[:2]) == [_compose_job(1), _compose_job(2)] and after[2] == _compose_job(3)  # nothing lost
    assert finished == []


def test_no_compose_job_is_taken_after_the_shutdown_signal(redis_server, monkeypatch):
    started, finished = [], []
    _fake_compose(monkeypatch, started, finished)

    async def scenario():
        r = redis_server()
        await r.rpush(COMPOSE, _compose_job(1))
        shutdown = asyncio.Event()
        shutdown.set()
        await asyncio.wait_for(worker.compose_loop(shutdown), timeout=3)
        return await r.lrange(COMPOSE, 0, -1)

    assert asyncio.run(scenario()) == [_compose_job(1)]
    assert started == []


def test_a_finished_compose_job_is_not_handed_back(redis_server, monkeypatch):
    started, finished = [], []
    _fake_compose(monkeypatch, started, finished, seconds=0.05)

    async def scenario():
        r = redis_server()
        await r.rpush(COMPOSE, _compose_job(1))
        shutdown = asyncio.Event()
        listener = asyncio.create_task(worker.compose_loop(shutdown))
        await asyncio.sleep(0.3)
        shutdown.set()
        await asyncio.wait_for(listener, timeout=3)
        return await r.lrange(COMPOSE, 0, -1)

    assert asyncio.run(scenario()) == []
    assert finished == ["run-1"]


# --- The whole worker: production's loop and SIGTERM handling, plus the Newsroom parts ------

class FakeGraph:
    """Stands in for the LangGraph workflow: each article takes `seconds`; results by URL."""

    def __init__(self, results, seconds=0.0):
        self.results, self.seconds, self.seen = results, seconds, []

    async def ainvoke(self, state, config=None):
        self.seen.append((state.source_url, state.check_real_estate))
        await asyncio.sleep(self.seconds)
        return self.results[state.source_url]


class FakeWorkflow:
    graph = None

    def create_workflow(self):
        return FakeWorkflow.graph


def _run_worker_then_sigterm(after_seconds):
    async def scenario():
        task = asyncio.create_task(worker.run_worker())
        await asyncio.sleep(after_seconds)
        os.kill(os.getpid(), signal.SIGTERM)  # what ECS sends on a Fargate Spot interruption
        await asyncio.wait_for(task, timeout=10)
    asyncio.run(scenario())


def test_worker_records_not_real_estate_and_still_stops_on_sigterm(redis_server, monkeypatch):
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", "false")
    FakeWorkflow.graph = FakeGraph({"https://example.com/villa-recipe": {
        "error_message": None, "real_estate_check": {"is_real_estate": False, "reason": "A recipe"}}})
    monkeypatch.setattr(worker, "MainWorkflow", FakeWorkflow)

    async def push():
        await redis_server().lpush(ARTICLES, json.dumps({
            "job_id": "job-1", "source_url": "https://example.com/villa-recipe", "check_real_estate": True}))
    asyncio.run(push())

    _run_worker_then_sigterm(0.5)

    async def read():
        r = redis_server()
        return await r.hget("job:job-1", "status"), await r.llen("test_dlq")
    assert asyncio.run(read()) == ("skipped", 0)  # not a failure: no dead-letter entry
    assert FakeWorkflow.graph.seen == [("https://example.com/villa-recipe", True)]


def test_compose_runs_beside_a_busy_article_queue_and_sigterm_hands_both_back(redis_server, monkeypatch):
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", "true")
    started, finished = [], []
    _fake_compose(monkeypatch, started, finished)
    urls = [f"https://example.com/{n}" for n in range(3)]
    FakeWorkflow.graph = FakeGraph({u: {"error_message": None} for u in urls}, seconds=30)
    monkeypatch.setattr(worker, "MainWorkflow", FakeWorkflow)

    async def push():
        r = redis_server()
        for n, u in enumerate(urls):
            await r.lpush(ARTICLES, json.dumps({"job_id": f"job-{n}", "source_url": u}))
        await r.rpush(COMPOSE, _compose_job(1))
    asyncio.run(push())

    _run_worker_then_sigterm(0.6)

    async def read():
        r = redis_server()
        return await r.lrange(ARTICLES, 0, -1), await r.lrange(COMPOSE, 0, -1)
    articles, compose = asyncio.run(read())
    assert started == ["run-1"]          # compose started while an article was still being processed
    assert compose == [_compose_job(1)]  # handed back on SIGTERM
    assert len(articles) == 3            # production's handling put the cut-off article back too
