"""
Keeps entity_mentions.location_ids correct.

Every link on an item carries the item's confirmed places plus all their
ancestors (Dubai Creek Harbour -> Dubai -> United Arab Emirates), so "Emaar in
Dubai" is one indexed lookup. The value is recomputed whenever a place is
linked or unlinked on an item, or a place moves in the hierarchy.
"""
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

from pymongo import UpdateMany

from src.newsroom import db as ndb

# Items recomputed per round: one read of their place links, one bulk write.
BATCH_SIZE = 500

ItemKey = Tuple[str, str]


async def _refresh_batch(db, items: List[ItemKey], paths: Dict[str, list]) -> Dict[ItemKey, list]:
    """Recomputes location_ids for up to BATCH_SIZE items. `paths` caches places' path_ids between batches."""
    by_type = defaultdict(list)
    for source_type, source_id in items:
        by_type[source_type].append(source_id)
    places = defaultdict(set)
    async for mention in db[ndb.ENTITY_MENTIONS].find(
        {"$or": [{"source_type": t, "source_id": {"$in": ids}} for t, ids in by_type.items()],
         "status": "linked", "kind": "location"},
        {"source_type": 1, "source_id": 1, "entity_id": 1},
    ):
        places[(mention["source_type"], mention["source_id"])].add(mention["entity_id"])
    unknown = list({p for found in places.values() for p in found} - paths.keys())
    if unknown:
        async for place in db[ndb.ENTITIES].find({"_id": {"$in": unknown}}, {"path_ids": 1}):
            paths[place["_id"]] = place.get("path_ids") or [place["_id"]]

    result = {}
    for key in items:
        result[key] = sorted({a for p in places.get(key, ()) for a in paths.get(p, [])})
    await db[ndb.ENTITY_MENTIONS].bulk_write(
        [UpdateMany({"source_type": t, "source_id": i}, {"$set": {"location_ids": ids}}) for (t, i), ids in result.items()],
        ordered=False,
    )
    return result


async def refresh_items_locations(db, items: Iterable[ItemKey]) -> int:
    """Recomputes location_ids on every link of these items, in batches. Returns the number of items."""
    items = list(dict.fromkeys(items))
    paths: Dict[str, list] = {}
    for start in range(0, len(items), BATCH_SIZE):
        await _refresh_batch(db, items[start:start + BATCH_SIZE], paths)
    return len(items)


async def refresh_item_locations(db, source_type: str, source_id: str) -> list:
    key = (source_type, source_id)
    return (await _refresh_batch(db, [key], {}))[key]


async def refresh_items_for_places(db, place_ids: Iterable[str]) -> int:
    """Recomputes every item that has one of these places linked. Returns the number of items."""
    place_ids = list(place_ids)
    if not place_ids:
        return 0
    items = set()
    async for mention in db[ndb.ENTITY_MENTIONS].find(
        {"entity_id": {"$in": place_ids}, "status": "linked"}, {"source_type": 1, "source_id": 1}
    ):
        items.add((mention["source_type"], mention["source_id"]))
    return await refresh_items_locations(db, items)
