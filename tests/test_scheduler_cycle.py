from datetime import datetime, timedelta

import pytest

from src.scheduler import main as scheduler_main


class FakeSourcesCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, query):
        return self._docs


@pytest.mark.asyncio
async def test_run_scheduler_cycle_only_schedules_due_sources(monkeypatch):
    now = datetime.utcnow()
    docs = [
        {
            "_id": "due-no-last-run",
            "is_active": True,
            "last_run_at": None,
            "fetch_interval_minutes": 60,
        },
        {
            "_id": "due-interval-elapsed",
            "is_active": True,
            "last_run_at": now - timedelta(minutes=90),
            "fetch_interval_minutes": 60,
        },
        {
            "_id": "not-due",
            "is_active": True,
            "last_run_at": now - timedelta(minutes=10),
            "fetch_interval_minutes": 60,
        },
    ]

    monkeypatch.setattr(scheduler_main, "sources_col", FakeSourcesCollection(docs))

    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(scheduler_main.asyncio, "create_task", fake_create_task)

    await scheduler_main.run_scheduler_cycle()

    assert len(scheduled) == 2


def test_ensure_utc_adds_timezone_to_naive_datetime():
    naive = datetime(2026, 1, 1, 10, 0, 0)
    utc_value = scheduler_main.ensure_utc(naive)

    assert utc_value.tzinfo is not None