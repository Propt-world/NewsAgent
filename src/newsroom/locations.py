"""
Keeps entity_mentions.location_ids correct.

Every link on an item carries the item's confirmed places plus all their
ancestors (Dubai Creek Harbour -> Dubai -> United Arab Emirates), so "Emaar in
Dubai" is one indexed lookup. The value is recomputed whenever a place is
linked or unlinked on an item, or a place moves in the hierarchy.
"""
from typing import Iterable

from src.newsroom import db as ndb


async def refresh_item_locations(db, source_type: str, source_id: str) -> list:
    place_ids = await db[ndb.ENTITY_MENTIONS].distinct(
        "entity_id", {"source_type": source_type, "source_id": source_id, "status": "linked", "kind": "location"}
    )
    location_ids = set()
    if place_ids:
        async for place in db[ndb.ENTITIES].find({"_id": {"$in": place_ids}}, {"path_ids": 1}):
            location_ids.update(place.get("path_ids") or [place["_id"]])
    location_ids = sorted(location_ids)
    await db[ndb.ENTITY_MENTIONS].update_many(
        {"source_type": source_type, "source_id": source_id},
        {"$set": {"location_ids": location_ids}},
    )
    return location_ids


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
    for source_type, source_id in items:
        await refresh_item_locations(db, source_type, source_id)
    return len(items)
