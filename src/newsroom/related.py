"""
Related items and entity history, from confirmed (reviewer-linked) entities only.

Related items:
- The main reason two items are related is a shared switched-on organisation
  (company, developer, project...). Shared places only narrow and rank, because
  "both mention Dubai" would make almost everything related.
- An item with no organisation links falls back to its specific places
  (communities, landmarks), never whole cities or countries.
- Ranked by number of shared entities, then place overlap, then shared topics (the 30 real-estate topics:
  an article's categories, a signal's kg_topics), then recency. These strong matches are always shown.
- Rejected, duplicated and deleted items are never shown.

Similar text: when an item has fewer than FALLBACK_FILL strong matches, approved articles and approved signals
whose stored vectors are close to the opened item's own fill the list up to FALLBACK_FILL, each labelled with its
score. The opened item may be pending or approved (a pending article's small vector, article_vectors.py; an
approved article's first stored piece; a signal's own vector). No AI call is made, and nothing is saved:
the knowledge graph keeps only confirmed links.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId

from src.configs.settings import settings
from src.newsroom import article_vectors
from src.newsroom import db as ndb
from src.newsroom.entities import Invalid, NotFound
from src.newsroom.links import _article_info, _parse_date, _signal_info, _topics
from src.newsroom.signal_options import REAL_ESTATE_CATEGORIES

logger = logging.getLogger("newsroom")

# Archived articles and expired signals stay visible (they are history); deleted ones are hidden.
HIDDEN_ITEM_STATUSES = ["rejected", "duplicated", "missing", "deleted"]
SPECIFIC_PLACE_TYPES = ["community", "landmark"]
SIMILAR_CANDIDATE_CHUNKS = 50
SIMILAR_CANDIDATE_SIGNALS = 50
# Fewer strong matches than this: similar text fills the list up to it
FALLBACK_FILL = 5


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


async def _signal_vector_search(signals, vector: List[float], limit: int) -> List[dict]:
    """Atlas Vector Search over signals' own vectors (the index Grok Scout's duplicate check uses; no AI call)."""
    pipeline = [
        {"$vectorSearch": {"index": settings.NEWSROOM_SIGNAL_VECTOR_INDEX, "path": "embedding",
                           "queryVector": vector, "numCandidates": limit * 10, "limit": limit}},
        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
        {"$project": {"embedding": 0}},  # the 3072 numbers are not needed back
    ]
    return await (await signals.aggregate(pipeline)).to_list(length=limit)


async def _own_vector(db, sources: ndb.ItemSources, source_type: str, source_id: str) -> Optional[List[float]]:
    """The opened item's stored vector: a signal's own; a pending article's small one; an approved article's
    first stored piece (made at approval). None when it has none (it then gets no text matches)."""
    if source_type == "signal":
        if not ObjectId.is_valid(source_id):
            return None
        doc = await sources.signals.find_one({"_id": ObjectId(source_id)}, {"embedding": 1})
        return list(doc["embedding"]) if doc and doc.get("embedding") else None
    article = await sources.articles.find_one({"_id": source_id}, {"url": 1, "status": 1})
    if article is None:
        return None
    if article.get("status") != article_vectors.PENDING and sources.chunks is not None and article.get("url"):
        piece = await sources.chunks.find_one(
            {"metadata.url": article["url"]}, {"embedding": 1}, sort=[("metadata.chunk_index", 1)])
        if piece and piece.get("embedding"):
            return list(piece["embedding"])
    return await article_vectors.get(db, source_id)


async def text_matches(db, sources: Optional[ndb.ItemSources], source_type: str, source_id: str,
                       own_topics: List[str], want: int, skip_items: set, days: Optional[int] = None,
                       topic: Optional[str] = None) -> List[dict]:
    """Up to `want` approved articles and signals whose vectors are close to the opened item's (best first),
    leaving out `skip_items` ((source_type, source_id) pairs). A missing vector index only leaves its part out."""
    if sources is None or want <= 0:
        return []
    vector = await _own_vector(db, sources, source_type, source_id)
    if not vector:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    floor = settings.NEWSROOM_SIMILARITY_MIN_SCORE
    found = []  # (score, source_type, source_id, info)

    if sources.chunks is not None:
        try:
            hits = await _vector_search(sources.chunks, vector, SIMILAR_CANDIDATE_CHUNKS)
        except Exception as e:
            logger.warning(f"[NEWSROOM] Similar-articles search failed: {e}")
            hits = []
        best = {}
        for hit in hits:
            url, score = (hit.get("metadata") or {}).get("url"), hit.get("score") or 0.0
            if url and score >= floor:
                best[url] = max(score, best.get(url, 0.0))
        if best:
            docs = await sources.articles.find({"url": {"$in": list(best)}, "status": "approved"}, {
                "url": 1, "source_id": 1, "processed_at": 1, "discovered_at": 1, "final_output.title": 1,
                "final_output.summary": 1, "final_output.published_date": 1, "final_output.category": 1,
            }).to_list(length=None)
            for d in docs:
                found.append((best[d["url"]], "article", d["_id"], _article_info(d, d.get("status") or "approved")))

    try:
        hits = await _signal_vector_search(sources.signals, vector, SIMILAR_CANDIDATE_SIGNALS)
    except Exception as e:
        logger.warning(f"[NEWSROOM] Similar-signals search failed (no vector index on signals?): {e}")
        hits = []
    for hit in hits:
        score = hit.get("score") or 0.0
        if hit.get("status") == "approved" and score >= floor:
            found.append((score, "signal", str(hit["_id"]), _signal_info(hit)))

    items, seen = [], set(skip_items) | {(source_type, source_id)}
    for score, kind, item_id, info in sorted(found, key=lambda f: (-f[0], f[1], f[2])):
        if (kind, item_id) in seen:
            continue
        stamp = info.get("timestamp")
        if since and (stamp is None or (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)) < since):
            continue
        if topic and topic not in info["topics"]:
            continue
        seen.add((kind, item_id))
        shared = [t for t in own_topics if t in info["topics"]]
        items.append({
            "source_type": kind, "source_id": item_id, "title": info.get("title"), "snippet": info.get("snippet"),
            "url": info.get("url"), "timestamp": stamp, "item_status": info.get("item_status"),
            "shared_entities": [], "shared_places": [], "shared_topics": shared, "topics": info["topics"],
            "reason": f"Similar text (score {score:.2f})", "score": round(score, 4), "match": "text",
        })
        if len(items) >= want:
            break
    return items


async def _own_topics(sources: Optional[ndb.ItemSources], source_type: str, source_id: str) -> List[str]:
    if sources is None:
        return []
    from src.newsroom.links import items_info

    info = (await items_info(sources, source_type, [source_id])).get(source_id) or {}
    return info.get("topics") or []


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
        result = {"basis": "none", "items": [], "total": 0, "limit": limit, "skip": skip}
        return await _with_text_matches(db, sources, source_type, source_id, own_topics, result,
                                        location_id, days, topic)

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
            "match": "strong",
        })
    result = {"basis": basis, "items": items, "total": total, "limit": limit, "skip": skip}
    return await _with_text_matches(db, sources, source_type, source_id, own_topics, result, location_id, days, topic)


async def _with_text_matches(db, sources, source_type: str, source_id: str, own_topics: List[str], result: dict,
                             location_id: Optional[str], days: Optional[int], topic: Optional[str]) -> dict:
    """Fewer than FALLBACK_FILL strong matches (first page, no place filter): similar text fills up to it."""
    if result["skip"] or location_id or result["total"] >= FALLBACK_FILL:
        return result
    if not own_topics:
        own_topics = await _own_topics(sources, source_type, source_id)
    shown = {(i["source_type"], i["source_id"]) for i in result["items"]}
    extra = await text_matches(db, sources, source_type, source_id, own_topics, FALLBACK_FILL - result["total"],
                               shown, days, topic)
    if extra:
        result["items"] = result["items"] + extra
        result["total"] += len(extra)
        if result["basis"] == "none":
            result["basis"] = "similar"
    return result


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
