"""
Newsroom health, for alerts.

- The backoffice shows a banner from GET /newsroom/status (problems of the last 24 hours).
- The scheduler runs the same checks every NEWSROOM_ALERT_CHECK_MINUTES and logs each problem as
  "NEWSROOM_ALERT <code>: <message>"; a CloudWatch metric filter on NEWSROOM_ALERT raises the alarm.
- The compose worker reports in every HEARTBEAT_SECONDS (a Redis key), so a worker that is not
  running, or runs without NEWSROOM_ENABLED, is noticed before jobs time out.

Every check only reads, and a check that cannot run is reported, never raised.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from src.configs.settings import settings
from src.newsroom import compose as cmp
from src.newsroom import db as ndb

logger = logging.getLogger("newsroom")

ALERT_PREFIX = "NEWSROOM_ALERT"
HEARTBEAT_KEY = "newsroom:compose_worker:heartbeat"
HEARTBEAT_SECONDS = 30
BANNER_WINDOW = timedelta(hours=24)
# Below this many articles in 24 h the filtered-out share means little
FILTERED_MIN_ARTICLES = 20
LEVELS = {"ok": 0, "warning": 1, "error": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- The worker reports in ------------------------------------------------------------

async def report_in(redis) -> None:
    # Kept a while after the worker stops, so "last seen" can be shown; the age is what is checked.
    await redis.set(HEARTBEAT_KEY, _now().isoformat(), ex=24 * 3600)


async def heartbeat_loop(redis, stop: asyncio.Event) -> None:
    """Runs next to the compose listener until the worker shuts down."""
    while not stop.is_set():
        try:
            await report_in(redis)
        except Exception as e:
            logger.warning(f"[NEWSROOM] Could not report the compose worker in: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            pass


# --- Checks ----------------------------------------------------------------------------

def _problem(code: str, level: str, message: str) -> dict:
    return {"code": code, "level": level, "message": message}


def _usd(value: float) -> str:
    # Small amounts keep 3 decimals so a few cents do not read as $0.00 (as in the backoffice)
    return f"${value:.{3 if value < 1 else 2}f}"


def _ago(minutes: float) -> str:
    if minutes < 90:
        return f"{int(minutes)} min"
    return f"{minutes / 60:.1f} h"


async def _missing_indexes(db) -> List[str]:
    wanted = {}
    for collection, _, options in ndb.INDEX_SPECS:
        wanted.setdefault(collection, set()).add(options["name"])
    missing = []
    for collection, names in wanted.items():
        found = {ix["name"] async for ix in await db[collection].list_indexes()}
        missing += [f"{collection}.{n}" for n in sorted(names - found)]
    return missing


async def check_status(db, articles=None, redis=None, failure_window: timedelta = BANNER_WINDOW) -> dict:
    """
    The Newsroom's problems right now. failure_window: how far back failed compose jobs count
    (24 h for the banner; one check interval for the log lines, so each failure is logged once).
    """
    now = _now()
    problems, facts = [], {}

    try:
        await db.command("ping")
    except Exception as e:
        problems.append(_problem("database_unreachable", "error", f"The Newsroom database cannot be reached: {e}"))
        return _result(now, problems, facts)

    try:
        missing = await _missing_indexes(db)
        if missing:
            problems.append(_problem("indexes_missing", "warning",
                                     f"{len(missing)} database indexes are missing ({', '.join(missing[:5])}). "
                                     "Lists may be slow; the scheduler creates them at startup, see its logs."))
    except Exception as e:
        problems.append(_problem("indexes_unknown", "warning", f"Could not check the database indexes: {e}"))

    if redis is not None:
        try:
            await redis.ping()
            beat = await redis.get(HEARTBEAT_KEY)
            seen = _as_utc(datetime.fromisoformat(beat)) if beat else None
            facts["worker_last_seen"] = seen
            if seen is None or now - seen > timedelta(minutes=settings.NEWSROOM_ALERT_WORKER_MINUTES):
                when = f"for {_ago((now - seen).total_seconds() / 60)}" if seen else "yet"
                problems.append(_problem(
                    "no_compose_worker", "error",
                    f"No compose worker has reported in {when}. New compose jobs will wait and then fail. "
                    "Check that the worker is running and has NEWSROOM_ENABLED=true."))
        except Exception as e:
            problems.append(_problem("queue_unreachable", "error",
                                     f"The compose queue (Redis) cannot be reached: {e}. Compose jobs cannot start."))

    runs = db[ndb.COMPOSE_RUNS]
    oldest = await runs.find_one({"status": "queued"}, {"created_at": 1}, sort=[("created_at", 1)])
    if oldest:
        waiting = (now - _as_utc(oldest["created_at"])).total_seconds() / 60
        facts["oldest_queued_minutes"] = round(waiting, 1)
        if waiting > settings.NEWSROOM_ALERT_QUEUE_MINUTES:
            queued = await runs.count_documents({"status": "queued"})
            problems.append(_problem("compose_jobs_waiting", "warning",
                                     f"{queued} compose job{'s' if queued != 1 else ''} waiting, the oldest for "
                                     f"{_ago(waiting)}. The worker may be busy, restarting or not running."))

    failed_query = {"status": "failed", "finished_at": {"$gte": now - failure_window}}
    failed = await runs.count_documents(failed_query)
    facts["failed_compose_jobs"] = failed
    if failed:
        latest = await runs.find_one(failed_query, {"error": 1}, sort=[("finished_at", -1)])
        window = "24 h" if failure_window == BANNER_WINDOW else _ago(failure_window.total_seconds() / 60)
        problems.append(_problem("compose_failed", "warning",
                                 f"{failed} compose job{'s' if failed != 1 else ''} failed in the last {window}. "
                                 f"Latest: {(latest or {}).get('error') or 'unknown error'}"))

    spent, budget = await cmp.spent_today(db, now), settings.NEWSROOM_DAILY_BUDGET_USD
    facts["spent_today_usd"], facts["daily_budget_usd"] = round(spent, 4), budget
    if budget > 0 and spent >= budget:
        problems.append(_problem("budget_reached", "error",
                                 f"Today's AI budget is used up ({_usd(spent)} of {_usd(budget)}). "
                                 "No new compose jobs until midnight UAE time."))
    elif budget > 0 and spent >= budget * settings.NEWSROOM_ALERT_BUDGET_SHARE:
        problems.append(_problem("budget_near", "warning",
                                 f"Today's AI spend is {_usd(spent)} of the {_usd(budget)} budget "
                                 f"({spent / budget:.0%}). New jobs stop when it is reached."))

    if settings.real_estate_check_enabled and articles is not None:
        problems += await _filtered_share(articles, now, facts)

    return _result(now, problems, facts)


async def _filtered_share(articles, now: datetime, facts: dict) -> List[dict]:
    since = now - timedelta(hours=24)
    filtered = await articles.count_documents({"status": "not_real_estate", "filtered_at": {"$gte": since}})
    processed = await articles.count_documents({"processed_at": {"$gte": since}})
    total = filtered + processed
    facts["filtered_out_24h"], facts["checked_24h"] = filtered, total
    if total >= FILTERED_MIN_ARTICLES and filtered / total > settings.NEWSROOM_ALERT_FILTERED_SHARE:
        return [_problem("many_filtered_out", "warning",
                         f"The real-estate check filtered out {filtered} of {total} articles in the last 24 h "
                         f"({filtered / total:.0%}). Check Filtered Out for real-estate articles marked wrongly.")]
    return []


def _result(now: datetime, problems: List[dict], facts: dict) -> dict:
    worst = max((LEVELS[p["level"]] for p in problems), default=0)
    return {"status": next(k for k, v in LEVELS.items() if v == worst), "checked_at": now,
            "problems": problems, "facts": facts}


# --- CloudWatch ------------------------------------------------------------------------

async def log_alerts(db=None, articles=None, redis=None) -> List[dict]:
    """The scheduler's periodic check: one "NEWSROOM_ALERT <code>: <message>" line per problem. Never raises."""
    own_redis = None
    try:
        if db is None:
            db = ndb.get_newsroom_db()
        if articles is None:
            articles = ndb.get_item_sources().articles
        if redis is None:
            import redis.asyncio as aioredis
            redis = own_redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        result = await check_status(db, articles, redis,
                                    failure_window=timedelta(minutes=settings.NEWSROOM_ALERT_CHECK_MINUTES))
    except Exception as e:
        result = {"problems": [_problem("status_check_failed", "error", f"The Newsroom status check failed: {e}")]}
    finally:
        if own_redis is not None:
            try:
                await own_redis.aclose()
            except Exception:
                pass
    for p in result["problems"]:
        log = logger.error if p["level"] == "error" else logger.warning
        log(f"{ALERT_PREFIX} {p['code']}: {p['message']}")
    return result["problems"]

