"""
Related items and entity history, from confirmed (reviewer-linked) entities only.

Related items:
- The main reason two items are related is a shared switched-on organisation
  (company, developer, project...). Shared places only narrow and rank, because
  "both mention Dubai" would make almost everything related.
- An item with no organisation links falls back to its specific places
  (communities, landmarks), never whole cities or countries.
- Ranked by number of shared entities, then place overlap, then recency.
- Rejected, duplicated and deleted items are never shown.

Similarity search for items with no links is a separate, later addition.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.newsroom import db as ndb
from src.newsroom.entities import Invalid, NotFound

HIDDEN_ITEM_STATUSES = ["rejected", "duplicated", "missing"]
SPECIFIC_PLACE_TYPES = ["community", "landmark"]


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


async def related_items(db, source_type: str, source_id: str, location_id: Optional[str] = None,
                        days: Optional[int] = None, limit: int = 20, skip: int = 0) -> dict:
    await _check_location(db, location_id)
    own = await db[ndb.ENTITY_MENTIONS].find(
        {"source_type": source_type, "source_id": source_id, "status": "linked"},
        {"entity_id": 1, "kind": 1, "location_ids": 1}).to_list(length=None)
    own_location_ids = own[0].get("location_ids", []) if own else []

    linked = await _names(db, [m["entity_id"] for m in own])
    active = {e["_id"]: e async for e in db[ndb.ENTITIES].find(
        {"_id": {"$in": list(linked)}, "status": "active"}, {"kind": 1, "type": 1})}
    basis_ids = [eid for eid, e in active.items() if e["kind"] == "organisation"]
    basis = "organisations"
    if not basis_ids:
        basis_ids = [eid for eid, e in active.items() if e["kind"] == "location" and e["type"] in SPECIFIC_PLACE_TYPES]
        basis = "places"
    if not basis_ids:
        return {"basis": "none", "items": [], "total": 0, "limit": limit, "skip": skip}

    match = _visible({"entity_id": {"$in": basis_ids}})
    if location_id:
        match["location_ids"] = location_id
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
        }},
        {"$addFields": {
            "shared_count": {"$size": "$shared_ids"},
            "place_ids": {"$setIntersection": [{"$ifNull": ["$location_ids", []]}, own_location_ids]},
        }},
        {"$addFields": {"place_overlap": {"$size": "$place_ids"}}},
        {"$sort": {"shared_count": -1, "place_overlap": -1, "timestamp": -1}},
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
            reason += " · same area: " + ", ".join(place_names)
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
        } for d in docs],
        "total": total,
        "limit": limit,
        "skip": skip,
    }
