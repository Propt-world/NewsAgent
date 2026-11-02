"""
Repairs the copies each link keeps of its item (title, snippet, link, date, status, places, topics).

Links copy a few fields of their article or signal so lists are fast. The copies are
refreshed whenever NewsAgent hears about a change, but a missed message (Newsroom
switched off for a while, a network error, a call from social_scrapper that failed)
leaves them stale. This re-reads every linked item in batches, compares, and fixes only
what differs. Safe to run any time; a dry run only counts.

Runs every night from the scheduler (when the Newsroom is on) and from
src/scripts/newsroom_resync.py by hand.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.configs.settings import settings
from src.newsroom import db as ndb
from src.newsroom import links as lnk

logger = logging.getLogger("newsroom")

BATCH = 500
COPIED_FIELDS = ("title", "snippet", "url", "timestamp", "topics")


def _same_time(a, b) -> bool:
    """MongoDB returns times without a timezone (UTC) and keeps only milliseconds; compare them that way.
    (A published date written as text with microseconds, e.g. ...12.467721, is stored as ...12.467.)"""
    def utc(value: Optional[datetime]):
        if not isinstance(value, datetime):
            return value
        value = value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=value.microsecond // 1000 * 1000)
    return utc(a) == utc(b)


async def _place_paths(db) -> Dict[str, List[str]]:
    paths = {}
    async for place in db[ndb.ENTITIES].find({"kind": "location"}, {"path_ids": 1}):
        paths[place["_id"]] = place.get("path_ids") or [place["_id"]]
    return paths


async def resync(db, sources: ndb.ItemSources, apply: bool = False) -> Dict[str, int]:
    counts = {"items_checked": 0, "items_fixed": 0, "items_gone": 0,
              "status": 0, "title_or_text": 0, "link": 0, "date": 0, "places": 0, "topics": 0}
    paths = await _place_paths(db)

    for source_type in lnk.SOURCE_TYPES:
        ids = await db[ndb.ENTITY_MENTIONS].distinct("source_id", {"source_type": source_type})
        for start in range(0, len(ids), BATCH):
            batch = ids[start:start + BATCH]
            current = await lnk.items_info(sources, source_type, batch)
            mentions: Dict[str, List[dict]] = {}
            async for m in db[ndb.ENTITY_MENTIONS].find(
                    {"source_type": source_type, "source_id": {"$in": batch}},
                    {"source_id": 1, "entity_id": 1, "kind": 1, "status": 1, "item_status": 1,
                     "location_ids": 1, **{f: 1 for f in COPIED_FIELDS}}):
                mentions.setdefault(m["source_id"], []).append(m)

            for source_id in batch:
                counts["items_checked"] += 1
                stored = mentions.get(source_id, [])
                info = current.get(source_id)
                if info is None:
                    if any(m.get("item_status") != "missing" for m in stored):
                        counts["items_gone"] += 1
                        counts["items_fixed"] += 1
                        if apply:
                            await lnk.item_changed(db, sources, source_type, source_id)  # marks it missing
                    continue

                places = sorted({p for m in stored if m.get("kind") == "location" and m.get("status") == "linked"
                                 for p in paths.get(m["entity_id"], [m["entity_id"]])})
                fixes = set()
                for m in stored:
                    if m.get("item_status") != info["item_status"]:
                        fixes.add("status")
                    if m.get("title") != info["title"] or m.get("snippet") != info["snippet"]:
                        fixes.add("title_or_text")
                    if m.get("url") != info["url"]:
                        fixes.add("link")
                    if not _same_time(m.get("timestamp"), info["timestamp"]):
                        fixes.add("date")
                    if sorted(m.get("location_ids") or []) != places:
                        fixes.add("places")
                    if (m.get("topics") or []) != info["topics"]:
                        fixes.add("topics")
                if not fixes:
                    continue
                counts["items_fixed"] += 1
                for fix in fixes:
                    counts[fix] += 1
                if not apply:
                    continue
                if "status" in fixes:
                    # Also keeps the new-names queue and composed pieces' source flags right.
                    await lnk.on_item_status(db, source_type, source_id, info["item_status"])
                await db[ndb.ENTITY_MENTIONS].update_many(
                    {"source_type": source_type, "source_id": source_id},
                    {"$set": {**{f: info[f] for f in COPIED_FIELDS}, "location_ids": places}},
                )

        # Items with only open unknown names (no links): their place in the new-names queue follows approval.
        linked = set(ids)
        queued_ids = [i for i in await db[ndb.ENTITY_SUGGESTIONS].distinct(
            "source_id", {"source_type": source_type, "status": "open", "entity_id": None}) if i not in linked]
        for start in range(0, len(queued_ids), BATCH):
            batch = queued_ids[start:start + BATCH]
            current = await lnk.items_info(sources, source_type, batch)
            flags: Dict[str, set] = {}
            async for sug in db[ndb.ENTITY_SUGGESTIONS].find(
                    {"source_type": source_type, "source_id": {"$in": batch}, "status": "open", "entity_id": None},
                    {"source_id": 1, "in_new_names_queue": 1}):
                flags.setdefault(sug["source_id"], set()).add(bool(sug.get("in_new_names_queue")))
            for source_id in batch:
                counts["items_checked"] += 1
                info = current.get(source_id)
                should_queue = info is not None and info["item_status"] == "approved"
                if flags.get(source_id, set()) <= {should_queue}:
                    continue
                counts["items_fixed"] += 1
                counts["items_gone" if info is None else "status"] += 1
                if apply:
                    await lnk.item_changed(db, sources, source_type, source_id)
    return counts


async def nightly_repair() -> None:
    """Scheduler job: logs what it fixed; never raises."""
    try:
        counts = await resync(ndb.get_newsroom_db(), ndb.get_item_sources(), apply=True)
        logger.info(f"[NEWSROOM] Nightly link repair: {counts}")
    except Exception:
        logger.exception("[NEWSROOM] Nightly link repair failed")
    try:
        from src.newsroom.compose import prune_old_runs
        trimmed = await prune_old_runs(ndb.get_newsroom_db())
        logger.info(f"[NEWSROOM] Nightly compose clean-up: {trimmed} job(s) older than "
                    f"{settings.NEWSROOM_COMPOSE_KEEP_DAYS} days kept as cost records only")
    except Exception:
        logger.exception("[NEWSROOM] Nightly compose clean-up failed")
