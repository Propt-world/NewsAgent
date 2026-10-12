"""
Compose requests (scheduler side): checks, spending limits and the run record.

Nothing here calls an AI model. A request is checked, recorded in compose_runs
and queued; the worker (compose_worker.py) does the paid work. Every run keeps
its provenance: sources, entities, model, prompts, tokens and cost.

Limits (B1), all checked before anything is queued:
- at most NEWSROOM_COMPOSE_PER_HOUR new runs per hour (all users together)
- at most NEWSROOM_COMPOSE_CONCURRENT runs queued or running at once
- no new runs once today's spend (NEWSROOM_BUDGET_TIMEZONE day) reaches NEWSROOM_DAILY_BUDGET_USD
- the same sources and output cannot be composed twice at the same time
These checks and recording the run happen under one short lock, so a double click
or two reviewers at once cannot both pass them. Today's spend includes what running
jobs have spent so far, plus NEWSROOM_COMPOSE_RESERVE_USD held back per active job.

Deadlines: a queued run may wait up to NEWSROOM_COMPOSE_MAX_WAIT_HOURS for a
worker (busy or restarting); NEWSROOM_COMPOSE_TIMEOUT_MINUTES counts only from
when a worker starts it. deadline_at always holds the current one.
"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from src.configs.settings import settings
from src.newsroom import db as ndb
from src.newsroom import links as lnk
from src.newsroom.entities import Conflict, Invalid, NotFound
from src.newsroom.public_links import COMPOSED_SOURCE_ID  # re-exported: used by pipeline and the worker

AUTHOR = "Propt Newsroom"
ALLOWED_SOURCE_STATUSES = {
    "article": {"processed", "approved"},           # Pending review or approved
    "signal": {"draft", "pending_review", "approved"},
}
ACTIVE_STATUSES = ["queued", "running"]
MIN_SOURCES, MAX_SOURCES = 2, 5


NEEDS_IMAGE = "Add an image before approving this Propt Newsroom article."


class TooManyRequests(Exception):
    pass


def approval_blocked_reason(article: Optional[dict]) -> Optional[str]:
    """Composed articles have no source image, so one must be added before they go live (user decision,
    2026-10-10). Other articles are never blocked here."""
    if not article or article.get("source_id") != COMPOSED_SOURCE_ID:
        return None
    return None if (article.get("final_output") or {}).get("top_image") else NEEDS_IMAGE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def composed_url(run_id: str) -> str:
    """Unique url for a composed article (processed_articles.url is unique)."""
    return f"newsroom://compose/{run_id}"


def budget_day_start(now: Optional[datetime] = None) -> datetime:
    """Midnight of the current budget day (UAE time by default), in UTC."""
    tz = ZoneInfo(settings.NEWSROOM_BUDGET_TIMEZONE)
    local = (now or _now()).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def spent_today(db, now: Optional[datetime] = None) -> float:
    """Today's spend: finished starts (cost_usd) plus what running jobs have spent so far."""
    rows = await (await db[ndb.COMPOSE_RUNS].aggregate([
        {"$match": {"created_at": {"$gte": budget_day_start(now)}}},
        {"$group": {"_id": None, "cost": {"$sum": {"$add": [
            {"$ifNull": ["$cost_usd", 0]}, {"$ifNull": ["$running_cost_usd", 0]}]}}}},
    ])).to_list(length=1)
    return float(rows[0]["cost"]) if rows else 0.0


CREATE_LOCK = "compose_create"
LOCK_SECONDS = 10


@asynccontextmanager
async def _create_lock(db):
    """One request at a time checks the limits and records its run (a few milliseconds)."""
    locks, token = db[ndb.NEWSROOM_LOCKS], str(uuid.uuid4())
    for _ in range(30):
        now = _now()
        await locks.delete_one({"_id": CREATE_LOCK, "expires_at": {"$lt": now}})  # left by a crashed request
        try:
            await locks.insert_one({"_id": CREATE_LOCK, "token": token, "expires_at": now + timedelta(seconds=LOCK_SECONDS)})
            break
        except DuplicateKeyError:
            await asyncio.sleep(0.1)
    else:
        raise TooManyRequests("Another compose job is being started; try again in a moment")
    try:
        yield
    finally:
        await locks.delete_one({"_id": CREATE_LOCK, "token": token})


NEVER_STARTED = "No worker started this job in time (busy or restarting). Nothing more was charged; try again."
TIMED_OUT = "Timed out while writing"


def wait_deadline(now: datetime) -> datetime:
    return now + timedelta(hours=settings.NEWSROOM_COMPOSE_MAX_WAIT_HOURS)


def run_deadline(now: datetime) -> datetime:
    return now + timedelta(minutes=settings.NEWSROOM_COMPOSE_TIMEOUT_MINUTES)


async def reap_stale_runs(db) -> int:
    """Runs past their deadline are marked failed: queued ones waited too long, running ones took too long."""
    now = _now()
    count = 0
    for status, error in (("queued", NEVER_STARTED), ("running", TIMED_OUT)):
        res = await db[ndb.COMPOSE_RUNS].update_many(
            {"status": status, "deadline_at": {"$lt": now}},
            {"$set": {"status": "failed", "error": error, "finished_at": now}},
        )
        count += res.modified_count
    return count


def _out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


async def _source_entities(db, keys: List[tuple], exclude: List[str], include: Optional[List[str]] = None) -> List[str]:
    """Confirmed links of the sources, switched-on entities only, minus the ones the reviewer removed,
    plus the ones the reviewer added (which must be switched on)."""
    entity_ids = set()
    for source_type, source_id in keys:
        entity_ids.update(await db[ndb.ENTITY_MENTIONS].distinct(
            "entity_id", {"source_type": source_type, "source_id": source_id, "status": "linked"}))
    entity_ids -= set(exclude or [])
    added = set(include or [])
    if added:
        found = set(await db[ndb.ENTITIES].distinct("_id", {"_id": {"$in": list(added)}, "status": "active"}))
        if found != added:
            raise Invalid("Only switched-on companies and places can be added: "
                          + ", ".join(sorted(added - found)))
    entity_ids |= added
    if not entity_ids:
        return []
    active = await db[ndb.ENTITIES].distinct("_id", {"_id": {"$in": list(entity_ids)}, "status": "active"})
    return sorted(active)


async def create_run(db, sources: ndb.ItemSources, redis, *, kind: str, output_type: str,
                     items: List[dict], actor: str, exclude_entity_ids: Optional[List[str]] = None,
                     include_entity_ids: Optional[List[str]] = None) -> dict:
    """Checks and records a compose request, then queues it for the worker."""
    keys = list(dict.fromkeys((i["source_type"], i["source_id"]) for i in items))
    if kind == "compose" and not (MIN_SOURCES <= len(keys) <= MAX_SOURCES):
        raise Invalid(f"Choose {MIN_SOURCES} to {MAX_SOURCES} different sources")
    if kind == "extract_signal" and (len(keys) != 1 or keys[0][0] != "article"):
        raise Invalid("Extract a signal from exactly one article")

    source_docs = []
    for source_type, source_id in keys:
        info = await lnk.item_info(sources, source_type, source_id)
        if info is None:
            raise NotFound(f"{source_type} {source_id} not found")
        if info["item_status"] not in ALLOWED_SOURCE_STATUSES[source_type]:
            raise Invalid(f'"{info["title"] or source_id}" is {info["item_status"]} and cannot be used as a source')
        source_docs.append({"source_type": source_type, "source_id": source_id, "title": info["title"],
                            "url": info["url"], "status_at_request": info["item_status"]})

    await reap_stale_runs(db)
    source_keys = sorted(f"{t}:{i}" for t, i in keys)
    entity_ids = await _source_entities(db, keys, exclude_entity_ids or [], include_entity_ids or [])

    async with _create_lock(db):
        run = await _check_and_record(db, kind, output_type, source_keys, source_docs, entity_ids, actor,
                                      exclude_entity_ids, include_entity_ids)
    try:
        await redis.rpush(settings.NEWSROOM_COMPOSE_QUEUE, json.dumps({"run_id": run["_id"]}))
    except Exception as e:
        await db[ndb.COMPOSE_RUNS].update_one(
            {"_id": run["_id"]}, {"$set": {"status": "failed", "error": f"Could not queue: {e}", "finished_at": _now()}})
        raise
    return _out(run)


async def _check_and_record(db, kind, output_type, source_keys, source_docs, entity_ids, actor,
                            exclude_entity_ids, include_entity_ids) -> dict:
    """The limits, then the new run. Called under _create_lock."""
    running = await db[ndb.COMPOSE_RUNS].find_one(
        {"source_keys": source_keys, "kind": kind, "output_type": output_type, "status": {"$in": ACTIVE_STATUSES}})
    if running:
        raise Conflict(f"These sources are already being composed (run {running['_id']})")

    now = _now()
    active = await db[ndb.COMPOSE_RUNS].count_documents({"status": {"$in": ACTIVE_STATUSES}})
    if await db[ndb.COMPOSE_RUNS].count_documents({"created_at": {"$gte": now - timedelta(hours=1)}}) >= settings.NEWSROOM_COMPOSE_PER_HOUR:
        raise TooManyRequests(f"The limit of {settings.NEWSROOM_COMPOSE_PER_HOUR} compose jobs per hour is reached; try again later")
    if active >= settings.NEWSROOM_COMPOSE_CONCURRENT:
        raise TooManyRequests(f"{settings.NEWSROOM_COMPOSE_CONCURRENT} compose jobs are already running; try again in a minute")
    # Jobs in progress hold back a small amount each, so they cannot push the day past the budget.
    if await spent_today(db, now) + active * settings.NEWSROOM_COMPOSE_RESERVE_USD >= settings.NEWSROOM_DAILY_BUDGET_USD:
        raise TooManyRequests("Daily AI budget reached, try again tomorrow")

    run = {
        "_id": str(uuid.uuid4()),
        "kind": kind,
        "output_type": output_type,
        "status": "queued",
        "sources": source_docs,
        "source_keys": source_keys,
        "entity_ids": entity_ids,
        "excluded_entity_ids": sorted(set(exclude_entity_ids or [])),
        "included_entity_ids": sorted(set(include_entity_ids or [])),
        "requested_by": actor,
        "created_at": now,
        "deadline_at": wait_deadline(now),
        "started_at": None,
        "attempts": 0,
        "finished_at": None,
        "model": settings.NEWSROOM_COMPOSE_MODEL,
        "temperature": {"writer": settings.NEWSROOM_COMPOSE_TEMPERATURE,
                        "checks": settings.NEWSROOM_COMPOSE_CHECK_TEMPERATURE},
        "prompts": {},
        "tokens": {"prompt": 0, "completion": 0, "total": 0},
        "cost_usd": 0.0,
        "result": None,
        "error": None,
        "saved": None,
        "source_rejected": False,
    }
    await db[ndb.COMPOSE_RUNS].insert_one(run)
    return run


async def get_run(db, run_id: str) -> dict:
    await reap_stale_runs(db)
    doc = await db[ndb.COMPOSE_RUNS].find_one({"_id": run_id})
    if doc is None:
        raise NotFound(f"Compose run {run_id} not found")
    return _out(doc)


async def list_runs(db, limit: int = 20) -> dict:
    await reap_stale_runs(db)
    docs = await db[ndb.COMPOSE_RUNS].find({}).sort("created_at", -1).limit(limit).to_list(length=limit)
    return {
        "items": [_out(d) for d in docs],
        "spent_today_usd": round(await spent_today(db), 4),
        "daily_budget_usd": settings.NEWSROOM_DAILY_BUDGET_USD,
    }


async def mark_signal_saved(db, sources: ndb.ItemSources, run_id: str, signal_id: str, actor: str) -> dict:
    """The reviewer saved the drafted signal in social_scrapper: record it and link the run's entities to it."""
    run = await db[ndb.COMPOSE_RUNS].find_one({"_id": run_id})
    if run is None:
        raise NotFound(f"Compose run {run_id} not found")
    if run["output_type"] != "signal" or run["status"] != "succeeded":
        raise Invalid("Only a finished signal draft can be saved")
    if run.get("saved"):
        raise Conflict(f"This draft was already saved as signal {run['saved']['signal_id']}")
    if await lnk.item_info(sources, "signal", signal_id) is None:
        raise NotFound(f"Signal {signal_id} not found")
    res = await db[ndb.COMPOSE_RUNS].update_one(
        {"_id": run_id, "saved": None},
        {"$set": {"saved": {"signal_id": signal_id, "saved_by": actor, "saved_at": _now()}}})
    if res.modified_count == 0:
        raise Conflict("This draft was saved at the same moment by someone else")
    for entity_id in run.get("entity_ids", []):
        try:
            await lnk.link(db, sources, "signal", signal_id, entity_id, actor, link_source="compose")
        except (Invalid, Conflict, NotFound):
            pass  # The entity was switched off since; the reviewer can still link it by hand.
    return await get_run(db, run_id)
