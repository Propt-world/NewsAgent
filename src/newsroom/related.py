"""
Related items and entity history, from confirmed (reviewer-linked) entities only.

Related items:
- The main reason two items are related is a shared switched-on organisation
  (company, developer, project...). Shared places only narrow and rank, because
  "both mention Dubai" would make almost everything related.
- An item with no organisation links falls back to its specific places
  (communities, landmarks), never whole cities or countries.
- Ranked by number of shared entities, then place overlap, then shared topics (the 30 real-estate topics:
  an article's categories, a signal's kg_topics), then recency. Topics only order items that are already
  related; they never make two items related on their own ("both are off-plan" would match almost everything).
- Rejected, duplicated and deleted items are never shown.

Similar articles (option A): an *approved* article with no links falls back to
articles whose stored fingerprint (embedding, made at approval) is close to its
own. No new AI call is ever made, and pending articles never use this.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.configs.settings import settings
from src.newsroom import db as ndb
from src.newsroom.entities import Invalid, NotFound
from src.newsroom.links import _parse_date, _topics
from src.newsroom.signal_options import REAL_ESTATE_CATEGORIES

logger = logging.getLogger("newsroom")

# Archived articles and expired signals stay visible (they are history); deleted ones are hidden.
HIDDEN_ITEM_STATUSES = ["rejected", "duplicated", "missing", "deleted"]
SPECIFIC_PLACE_TYPES = ["community", "landmark"]
SIMILAR_CANDIDATE_CHUNKS = 50


def _check_topic(topic: Optional[str]) -> None:
    if topic and topic not in REAL_ESTATE_CATEGORIES:
        raise Invalid(f"Unknown topic: {topic}")


def _visible(extra: dict) -> dict:
    return {"status": "linked", "item_status": {"$nin": HIDDEN_ITEM_STATUSES}, **extra}


async def _check_location(db, location_id: Optional[str]) -> None:
    if not location_id:
        return
    place = await db[ndb.ENTITIES].find_one({"_id": location_id}, {"kind": 1})
    if place is None or place["kind"] != "location":
        raise Invalid(f"{location_id} is not a location")


async def _names(db, ids) -> dict:
    ids = list(ids)
    if not ids:
        return {}
    return {e["_id"]: e async for e in db[ndb.ENTITIES].find(
        {"_id": {"$in": ids}}, {"canonical_name": 1, "kind": 1, "type": 1, "path_ids": 1})}


def _most_specific(place_ids: List[str], places: dict) -> List[str]:
    """Drops places that are an ancestor of another place in the list (UAE, Dubai, Creek Harbour -> Creek Harbour)."""
    ancestors = set()
    for pid in place_ids:
        path = places.get(pid, {}).get("path_ids") or []
        ancestors.update(path[:-1])
    return [pid for pid in place_ids if pid not in ancestors]


async def _vector_search(chunks, vector: List[float], limit: int) -> List[dict]:
    """Atlas Vector Search over approved articles' stored chunks (no AI call)."""
    pipeline = [
        {"$vectorSearch": {"index": settings.MONGO_VECTOR_INDEX_NAME, "path": "embedding",
                           "queryVector": vector, "numCandidates": limit * 10, "limit": limit}},
        {"$project": {"_id": 0, "metadata.url": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return await (await chunks.aggregate(pipeline)).to_list(length=limit)


async def similar_articles(sources: ndb.ItemSources, article_id: str, days: Optional[int],
                           limit: int, skip: int, topic: Optional[str] = None) -> Optional[dict]:
    """Similar approved articles for an approved article, or None when this cannot apply."""
    if sources is None or sources.chunks is None:
        return None
    article = await sources.articles.find_one({"_id": article_id}, {"url": 1, "status": 1})
    if article is None or article.get("status") != "approved" or not article.get("url"):
        return None
    own = await sources.chunks.find_one(
        {"metadata.url": article["url"]}, {"embedding": 1}, sort=[("metadata.chunk_index", 1)])
    if not own or not own.get("embedding"):
        return None
    try:
        hits = await _vector_search(sources.chunks, own["embedding"], SIMILAR_CANDIDATE_CHUNKS)
    except Exception as e:
        # For example the vector index is missing in this environment: show nothing rather than fail.
        logger.warning(f"[NEWSROOM] Similar-articles search failed: {e}")
        return None

    best = {}
    for hit in hits:
        url = (hit.get("metadata") or {}).get("url")
        score = hit.get("score") or 0.0
        if url and url != article["url"] and score >= settings.NEWSROOM_SIMILARITY_MIN_SCORE:
            best[url] = max(score, best.get(url, 0.0))
    query = {"url": {"$in": list(best)}, "status": {"$nin": HIDDEN_ITEM_STATUSES}}
    if days:
        query["processed_at"] = {"$gte": datetime.now(timezone.utc) - timedelta(days=days)}
    if topic:
        query["final_output.category"] = topic
    docs = await sources.articles.find(query, {
        "url": 1, "status": 1, "processed_at": 1, "discovered_at": 1,
        "final_output.title": 1, "final_output.summary": 1, "final_output.published_date": 1,
        "final_output.category": 1,
    }).to_list(length=None) if best else []

    rows = sorted(docs, key=lambda d: best[d["url"]], reverse=True)
    items = []
    for d in rows[skip:skip + limit]:
        final = d.get("final_output") or {}
        score = best[d["url"]]
        items.append({
            "source_type": "article",
            "source_id": d["_id"],
            "title": final.get("title"),
            "snippet": (final.get("summary") or "")[:300],
            "url": d["url"],
            "timestamp": _parse_date(final.get("published_date")) or d.get("processed_at") or d.get("discovered_at"),
            "item_status": d.get("status"),
            "shared_entities": [],
            "shared_places": [],
            "topics": _topics(final.get("category")),
            "reason": f"Similar text (score {score:.2f})",
            "score": round(score, 4),
        })
    return {"basis": "similar", "items": items, "total": len(rows), "limit": limit, "skip": skip}


async def related_items(db, source_type: str, source_id: str, location_id: Optional[str] = None,
                        days: Optional[int] = None, limit: int = 20, skip: int = 0,
                        sources: Optional[ndb.ItemSources] = None, topic: Optional[str] = None) -> dict:
    """topic: only related items with this real-estate topic (one of the 30)."""
    await _check_location(db, location_id)
    _check_topic(topic)
    own = await db[ndb.ENTITY_MENTIONS].find(
        {"source_type": source_type, "source_id": source_id, "status": "linked"},
        {"entity_id": 1, "kind": 1, "location_ids": 1, "topics": 1}).to_list(length=None)
    own_location_ids = own[0].get("location_ids", []) if own else []
    own_topics = own[0].get("topics") or [] if own else []

    linked = await _names(db, [m["entity_id"] for m in own])
    active = {e["_id"]: e async for e in db[ndb.ENTITIES].find(
        {"_id": {"$in": list(linked)}, "status": "active"}, {"kind": 1, "type": 1})}
    basis_ids = [eid for eid, e in active.items() if e["kind"] == "organisation"]
    basis = "organisations"
    if not basis_ids:
        basis_ids = [eid for eid, e in active.items() if e["kind"] == "location" and e["type"] in SPECIFIC_PLACE_TYPES]
        basis = "places"
    if not basis_ids:
        # No confirmed links: approved articles fall back to similar text (a place filter cannot apply).
        if source_type == "article" and not location_id:
            similar = await similar_articles(sources, source_id, days, limit, skip, topic)
            if similar is not None:
                return similar
        return {"basis": "none", "items": [], "total": 0, "limit": limit, "skip": skip}

    match = _visible({"entity_id": {"$in": basis_ids}})
    if location_id:
        match["location_ids"] = location_id
    if topic:
        match["topics"] = topic
    if days:
        match["timestamp"] = {"$gte": datetime.now(timezone.utc) - timedelta(days=days)}

    pipeline = [
        {"$match": match},
        {"$match": {"$nor": [{"source_type": source_type, "source_id": source_id}]}},
        {"$group": {
            "_id": {"source_type": "$source_type", "source_id": "$source_id"},
            "shared_ids": {"$addToSet": "$entity_id"},
            "title": {"$first": "$title"},
            "snippet": {"$first": "$snippet"},
            "url": {"$first": "$url"},
            "timestamp": {"$max": "$timestamp"},
            "item_status": {"$first": "$item_status"},
            "location_ids": {"$first": "$location_ids"},
            "topics": {"$first": "$topics"},
        }},
        {"$addFields": {
            "shared_count": {"$size": "$shared_ids"},
            "place_ids": {"$setIntersection": [{"$ifNull": ["$location_ids", []]}, own_location_ids]},
            "topic_names": {"$setIntersection": [{"$ifNull": ["$topics", []]}, own_topics]},
        }},
        {"$addFields": {"place_overlap": {"$size": "$place_ids"}, "topic_overlap": {"$size": "$topic_names"}}},
        {"$sort": {"shared_count": -1, "place_overlap": -1, "topic_overlap": -1, "timestamp": -1,
                   "_id.source_type": 1, "_id.source_id": 1}},
        {"$facet": {
            "items": [{"$skip": skip}, {"$limit": limit}],
            "total": [{"$count": "n"}],
        }},
    ]
    result = await (await db[ndb.ENTITY_MENTIONS].aggregate(pipeline)).to_list(length=1)
    page = result[0]["items"] if result else []
    total = result[0]["total"][0]["n"] if result and result[0]["total"] else 0

    names = await _names(db, {i for row in page for i in row["shared_ids"] + row["place_ids"]})
    items = []
    for row in page:
        shared = sorted(row["shared_ids"], key=lambda i: names.get(i, {}).get("canonical_name", ""))
        places = _most_specific(row["place_ids"], names)
        shared_names = [names[i]["canonical_name"] for i in shared if i in names]
        place_names = [names[i]["canonical_name"] for i in places if i in names]
        reason = "Shares " + ", ".join(shared_names)
        if place_names and basis == "organisations":
            only_countries = all(names.get(i, {}).get("type") == "country" for i in places)
            reason += (" · same country: " if only_countries else " · same area: ") + ", ".join(place_names)
        # In the item's own order (a $setIntersection has none)
        topics = [t for t in own_topics if t in (row.get("topic_names") or [])]
        if topics:
            reason += (" · same topic: " if len(topics) == 1 else " · same topics: ") + ", ".join(topics)
        items.append({
            "source_type": row["_id"]["source_type"],
            "source_id": row["_id"]["source_id"],
            "title": row.get("title"),
            "snippet": row.get("snippet"),
            "url": row.get("url"),
            "timestamp": row.get("timestamp"),
            "item_status": row.get("item_status"),
            "shared_entities": [{"id": i, "name": names[i]["canonical_name"]} for i in shared if i in names],
            "shared_places": [{"id": i, "name": names[i]["canonical_name"]} for i in places if i in names],
            "shared_topics": topics,
            "topics": row.get("topics") or [],
            "reason": reason,
        })
    return {"basis": basis, "items": items, "total": total, "limit": limit, "skip": skip}


async def entity_history(db, entity_id: str, location_id: Optional[str] = None, before: Optional[datetime] = None,
                         after: Optional[datetime] = None, limit: int = 50, skip: int = 0) -> dict:
    """An entity's confirmed items, newest first, optionally only in one place and/or a date window."""
    entity = await db[ndb.ENTITIES].find_one({"_id": entity_id}, {"canonical_name": 1, "kind": 1, "status": 1})
    if entity is None:
        raise NotFound(f"Entity {entity_id} not found")
    await _check_location(db, location_id)

    match = _visible({"entity_id": entity_id})
    if location_id:
        match["location_ids"] = location_id
    window = {}
    if before:
        window["$lt"] = before
    if after:
        window["$gt"] = after
    if window:
        match["timestamp"] = window

    total = await db[ndb.ENTITY_MENTIONS].count_documents(match)
    docs = await db[ndb.ENTITY_MENTIONS].find(match).sort([("timestamp", -1), ("_id", -1)]).skip(skip).limit(limit).to_list(length=limit)
    return {
        "entity": {"id": entity["_id"], "name": entity["canonical_name"], "kind": entity["kind"], "status": entity["status"]},
        "items": [{
            "source_type": d["source_type"],
            "source_id": d["source_id"],
            "title": d.get("title"),
            "snippet": d.get("snippet"),
            "url": d.get("url"),
            "timestamp": d.get("timestamp"),
            "item_status": d.get("item_status"),
            "location_ids": d.get("location_ids", []),
            "topics": d.get("topics") or [],
        } for d in docs],
        "total": total,
        "limit": limit,
        "skip": skip,
    }


def _history_item(d: dict) -> dict:
    return {
        "source_type": d["source_type"],
        "source_id": d["source_id"],
        "title": d.get("title"),
        "snippet": d.get("snippet"),
        "url": d.get("url"),
        "timestamp": d.get("timestamp"),
        "item_status": d.get("item_status"),
        "location_ids": d.get("location_ids", []),
        "topics": d.get("topics") or [],
    }


async def filtered_items(db, entity_id: Optional[str] = None, location_id: Optional[str] = None,
                         source_type: Optional[str] = None, item_statuses: Optional[List[str]] = None,
                         q: Optional[str] = None, limit: int = 20, skip: int = 0, topic: Optional[str] = None) -> dict:
    """Distinct items linked to an entity and/or in a place, newest first (the Newsroom list filter).
    A place matches through the item's places and their parents, so Dubai includes Dubai communities.
    topic narrows them to one of the 30 real-estate topics (on its own the Newsroom asks the article and signal
    lists, which also have unlinked items).
    Rejected and duplicated items are hidden unless asked for by status (the Newsroom's Rejected view)."""
    if not entity_id and not location_id:
        raise Invalid("Choose a company or a place")
    _check_topic(topic)
    if entity_id and await db[ndb.ENTITIES].count_documents({"_id": entity_id}) == 0:
        raise NotFound(f"Entity {entity_id} not found")
    await _check_location(db, location_id)

    match = filter_match(entity_id, location_id, source_type, item_statuses, q, topic)
    result = (await (await db[ndb.ENTITY_MENTIONS].aggregate(filter_pipeline(match, limit, skip))).to_list(length=1))[0]
    total = result["total"][0]["n"] if result["total"] else 0
    # Only the page's links are read in full.
    page_ids = [r["mention_id"] for r in result["items"]]
    docs = {d["_id"]: d async for d in db[ndb.ENTITY_MENTIONS].find({"_id": {"$in": page_ids}})}
    items = [_history_item(docs[i]) for i in page_ids if i in docs]
    return {"items": items, "total": total, "limit": limit, "skip": skip}


def filter_match(entity_id: Optional[str], location_id: Optional[str], source_type: Optional[str],
                 item_statuses: Optional[List[str]], q: Optional[str], topic: Optional[str] = None) -> dict:
    match = {"status": "linked", "item_status": {"$nin": HIDDEN_ITEM_STATUSES}}
    if topic:
        match["topics"] = topic
    if entity_id:
        match["entity_id"] = entity_id
    if location_id:
        match["location_ids"] = location_id
    if source_type:
        match["source_type"] = source_type
    if item_statuses:
        # Explicit statuses are shown as asked; items whose source no longer exists never are.
        match["item_status"] = {"$in": [s for s in item_statuses if s != "missing"]}
    if q and q.strip():
        match["title"] = {"$regex": re.escape(q.strip()), "$options": "i"}
    return match


def filter_pipeline(match: dict, limit: int, skip: int) -> list:
    """
    One row per item, newest first. A country matches nearly every link, so links are read in
    date order straight from the index (no sort of all of them in memory) and only the fields
    needed to pick each item's newest link are kept.
    """
    return [
        {"$match": match},
        {"$sort": {"timestamp": -1}},
        {"$group": {"_id": {"t": "$source_type", "i": "$source_id"},
                    "timestamp": {"$first": "$timestamp"}, "mention_id": {"$first": "$_id"}}},
        {"$sort": {"timestamp": -1, "_id.t": 1, "_id.i": 1}},
        {"$facet": {"total": [{"$count": "n"}], "items": [{"$skip": skip}, {"$limit": limit}]}},
    ]
