"""Newsroom status for the backoffice banner and CloudWatch alerts (needs TEST_MONGO_URL)."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.configs.settings import settings
from src.newsroom import status


def _now():
    return datetime.now(timezone.utc)


class FakeRedis:
    def __init__(self, beat=None, down=False):
        self.values = {status.HEARTBEAT_KEY: beat.isoformat()} if beat else {}
        self.down = down

    async def ping(self):
        if self.down:
            raise ConnectionError("connection refused")
        return True

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value


def _check(run_counted, redis, **kwargs):
    result, _ = run_counted(lambda db, sources: status.check_status(db, sources.articles, redis, **kwargs))
    return result, {p["code"]: p for p in result["problems"]}


def _run(sync_db, run_id, status_, minutes_ago, **fields):
    sync_db["compose_runs"].insert_one({"_id": run_id, "status": status_, "created_at": _now() - timedelta(minutes=minutes_ago),
                                        "cost_usd": 0.0, **fields})


def test_all_well(api, run_counted):
    result, problems = _check(run_counted, FakeRedis(beat=_now() - timedelta(seconds=20)))
    assert (result["status"], problems) == ("ok", {})
    assert result["facts"]["spent_today_usd"] == 0


def test_a_missing_or_silent_worker_is_an_error(api, run_counted):
    result, problems = _check(run_counted, FakeRedis())
    assert result["status"] == "error"
    assert "has reported in yet" in problems["no_compose_worker"]["message"]
    assert "NEWSROOM_ENABLED=true" in problems["no_compose_worker"]["message"]
    _, problems = _check(run_counted, FakeRedis(beat=_now() - timedelta(minutes=12)))
    assert "for 12 min" in problems["no_compose_worker"]["message"]


def test_an_unreachable_queue_is_an_error(api, run_counted):
    _, problems = _check(run_counted, FakeRedis(down=True))
    assert problems["queue_unreachable"]["level"] == "error"
    assert "no_compose_worker" not in problems


def test_waiting_and_failed_jobs(api, sync_db, run_counted):
    _run(sync_db, "waiting", "queued", 25)
    _run(sync_db, "failed-old", "failed", 300, finished_at=_now() - timedelta(hours=3), error="Old failure")
    _run(sync_db, "failed-new", "failed", 70, finished_at=_now() - timedelta(minutes=60), error="model unavailable")
    _, problems = _check(run_counted, FakeRedis(beat=_now()))
    assert "1 compose job waiting, the oldest for 25 min" in problems["compose_jobs_waiting"]["message"]
    failed = problems["compose_failed"]["message"]
    assert "2 compose jobs failed in the last 24 h" in failed and "Latest: model unavailable" in failed
    # The periodic log check counts only its own interval, so each failure is logged once
    _, problems = _check(run_counted, FakeRedis(beat=_now()), failure_window=timedelta(minutes=10))
    assert "compose_failed" not in problems


@pytest.mark.parametrize("spent, code", [(7.0, None), (8.5, "budget_near"), (10.2, "budget_reached")])
def test_daily_budget(api, sync_db, run_counted, monkeypatch, spent, code):
    monkeypatch.setattr(settings, "NEWSROOM_DAILY_BUDGET_USD", 10.0)
    _run(sync_db, "spend", "succeeded", 1, cost_usd=spent)
    _, problems = _check(run_counted, FakeRedis(beat=_now()))
    assert ({"budget_near", "budget_reached"} & set(problems)) == ({code} if code else set())


def test_filtered_out_share_only_when_the_check_is_on(api, sync_db, run_counted, monkeypatch):
    articles = [{"_id": f"f{n}", "status": "not_real_estate", "filtered_at": _now() - timedelta(hours=1)} for n in range(15)]
    articles += [{"_id": f"p{n}", "status": "processed", "processed_at": _now() - timedelta(hours=2)} for n in range(10)]
    sync_db["processed_articles"].insert_many(articles)
    monkeypatch.setattr(settings, "REAL_ESTATE_CHECK_ENABLED", "false")
    assert "many_filtered_out" not in _check(run_counted, FakeRedis(beat=_now()))[1]
    monkeypatch.setattr(settings, "REAL_ESTATE_CHECK_ENABLED", "true")
    result, problems = _check(run_counted, FakeRedis(beat=_now()))
    assert "15 of 25 articles" in problems["many_filtered_out"]["message"]
    assert (result["facts"]["filtered_out_24h"], result["facts"]["checked_24h"]) == (15, 25)


def test_missing_indexes_are_reported(mongo_url, db_name, sync_db, run_counted):
    sync_db["entities"].insert_one({"_id": "x"})  # a database without the Newsroom indexes
    _, problems = _check(run_counted, FakeRedis(beat=_now()))
    assert "database indexes are missing" in problems["indexes_missing"]["message"]


def test_alert_lines_for_cloudwatch(api, sync_db, run_counted, caplog):
    _run(sync_db, "failed-new", "failed", 3, finished_at=_now() - timedelta(minutes=2), error="model unavailable")
    with caplog.at_level(logging.WARNING, logger="newsroom"):
        problems, _ = run_counted(lambda db, sources: status.log_alerts(db, sources.articles, FakeRedis()))
    lines = [r.getMessage() for r in caplog.records]
    assert {p["code"] for p in problems} == {"no_compose_worker", "compose_failed"}
    assert any(line.startswith("NEWSROOM_ALERT no_compose_worker: ") for line in lines)
    assert any(line.startswith("NEWSROOM_ALERT compose_failed: 1 compose job failed in the last 10 min") for line in lines)


def test_alert_check_never_raises(caplog):
    class BrokenDb:
        async def command(self, name):
            raise RuntimeError("no route to host")

    with caplog.at_level(logging.WARNING, logger="newsroom"):
        problems = asyncio.run(status.log_alerts(BrokenDb(), None, FakeRedis()))
    assert [p["code"] for p in problems] == ["database_unreachable"]
    assert "NEWSROOM_ALERT database_unreachable: The Newsroom database cannot be reached: no route to host" in caplog.text


def test_the_worker_reports_in_until_it_stops():
    async def go():
        redis, stop = FakeRedis(), asyncio.Event()
        task = asyncio.create_task(status.heartbeat_loop(redis, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        return redis.values[status.HEARTBEAT_KEY]
    seen = datetime.fromisoformat(asyncio.run(go()))
    assert abs((datetime.now(timezone.utc) - seen).total_seconds()) < 5


def test_status_route(api, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens there
    body = api.get("/newsroom/status").json()
    assert body["status"] == "error"
    assert "queue_unreachable" in {p["code"] for p in body["problems"]}
