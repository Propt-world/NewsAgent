"""
Suggestion-first entity linking.

- suggest(): names found on an article or signal become suggestions. Exact
  alias matches only; ambiguous aliases, ignored strings, websites/handles and
  switched-off ("ignored") entities produce nothing. Unknown names become
  "not in the list" suggestions. Nothing is written to the graph.
- link(): only an explicit reviewer action writes an entity_mentions record.
- unlink(): marks the link removed and dismisses its suggestions, so it is
  never suggested again for that item (a reviewer may still re-link by hand).
- Unknown names from approved items go to the "new names" queue.
"""
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import UpdateMany, UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError

from src.newsroom import db as ndb
from src.newsroom import entities as ent
from src.newsroom.entities import Conflict, Invalid, NotFound
from src.newsroom.locations import items_query, refresh_item_locations, refresh_items_locations
from src.newsroom.models import EntityUpdate
from src.newsroom import resolve as rs
from src.newsroom.public_links import shown_url
from src.newsroom.normalize import normalize_alias

SOURCE_TYPES = ("article", "signal")
FIELDS = ("organisation", "location")
SNIPPET_CHARS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _article_info(doc: dict, status: Optional[str]) -> dict:
    final = doc.get("final_output") or {}
    return {
        "title": final.get("title") or final.get("article_title") or "",
        "snippet": (final.get("summary") or "")[:SNIPPET_CHARS],
        # Composed articles get their public link, never the internal newsroom:// key.
        "url": shown_url(doc),
        # 30% of articles have no published date: fall back to when we processed/found them.
        "timestamp": _parse_date(final.get("published_date")) or doc.get("processed_at") or doc.get("discovered_at"),
        "item_status": status,
    }


def _signal_info(doc: dict) -> dict:
    text = doc.get("summary_en") or doc.get("summary") or ""
    evidence = doc.get("evidence") or []
    return {
        "title": text[:120],
        "snippet": text[:SNIPPET_CHARS],
        "url": evidence[0].get("url") if evidence and isinstance(evidence[0], dict) else None,
        "timestamp": doc.get("generated_at") or doc.get("updated_at"),
        "item_status": doc.get("status"),
    }


async def items_info(sources: ndb.ItemSources, source_type: str, source_ids: List[str]) -> Dict[str, dict]:
    """item_info for many items at once (a few queries per batch). Items that do not exist are left out."""
    ids = list(dict.fromkeys(i for i in source_ids if i))
    found: Dict[str, dict] = {}
    if source_type == "article":
        # Archive and Delete move an article out of processed_articles: archived ones stay linked
        # (shown as "archived"), deleted ones are hidden everywhere.
        for collection, label in ((sources.articles, None), (sources.archived, "archived"), (sources.deleted, "deleted")):
            todo = [i for i in ids if i not in found]
            if not todo or collection is None:
                continue
            async for doc in collection.find({"_id": {"$in": todo}}):
                found[doc["_id"]] = _article_info(doc, label or doc.get("status"))
        return found
    if source_type == "signal":
        object_ids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
        if object_ids:
            async for doc in sources.signals.find({"_id": {"$in": object_ids}}):
                found[str(doc["_id"])] = _signal_info(doc)
        return found
    raise Invalid(f"Unknown source type '{source_type}'")


async def item_info(sources: ndb.ItemSources, source_type: str, source_id: str) -> Optional[dict]:
    """Title, snippet, url, date and status of an article or signal, or None if it does not exist."""
    return (await items_info(sources, source_type, [source_id])).get(source_id)


def _check_source_type(source_type: str) -> None:
    if source_type not in SOURCE_TYPES:
        raise Invalid(f"source_type must be one of {SOURCE_TYPES}")


def _out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


# --- Suggestions ---------------------------------------------------------------

async def suggest(db, source_type: str, source_id: str, organisations: List[str], locations: List[str],
                  actor: str = "system") -> Dict[str, int]:
    """Creates suggestions for the names found on an item. Safe to call again: existing ones are kept as they are."""
    _check_source_type(source_type)
    counts = {"created": 0, "existing": 0, "skipped_ignored": 0, "skipped_handle": 0,
              "skipped_ambiguous": 0, "skipped_switched_off": 0, "skipped_linked_or_removed": 0}

    names, seen = [], set()
    for field, values in (("organisation", organisations), ("location", locations)):
        for name in values or []:
            name = (name or "").strip()
            norm = normalize_alias(name)
            if not norm or (field, norm) in seen:
                continue
            seen.add((field, norm))
            names.append((field, name, norm))
    if not names:
        return counts

    ignored = {d["string_key"] for d in await db[ndb.ENTITY_IGNORE].find({}, {"string_key": 1}).to_list(length=None)}
    mentions = {m["entity_id"]: m["status"] async for m in db[ndb.ENTITY_MENTIONS].find(
        {"source_type": source_type, "source_id": source_id}, {"entity_id": 1, "status": 1})}

    skip_counts = {rs.HANDLE: "skipped_handle", rs.IGNORED: "skipped_ignored",
                   rs.AMBIGUOUS: "skipped_ambiguous", rs.SWITCHED_OFF: "skipped_switched_off"}
    for field, name, norm in names:
        skipped = rs.precheck(name, ignored)
        if skipped:
            counts[skip_counts[skipped]] += 1
            continue
        given_as = field
        resolution, exact = await _resolve_in(db, field, name, norm, ignored)
        if resolution.outcome == rs.UNKNOWN:
            other, other_exact = await _resolve_in(db, rs.other_kind(field), name, norm, ignored)
            if other.outcome in rs.OTHER_LIST_OUTCOMES:
                field, resolution, exact = rs.other_kind(field), other, other_exact
        if resolution.outcome in skip_counts:
            counts[skip_counts[resolution.outcome]] += 1
            continue
        entity = resolution.entity
        if entity is not None and entity["_id"] in mentions:
            counts["skipped_linked_or_removed"] += 1
            continue

        if entity is None:
            reason = "not in the list"
        elif entity["status"] == "staged":
            reason = f"exact match on alias '{exact['alias']}' (entity is switched off)"
        else:
            reason = f"exact match on alias '{exact['alias']}'"
        if field != given_as:
            reason += f" (given as a {'company' if given_as == 'organisation' else 'place'} name)"
        doc = {
            "_id": str(uuid.uuid4()),
            "source_type": source_type,
            "source_id": source_id,
            "field": field,
            "name": name,
            "name_norm": norm,
            "entity_id": entity["_id"] if entity else None,
            "entity_status": entity["status"] if entity else None,
            "matched_alias": exact["alias"] if exact else None,
            "reason": reason,
            "status": "open",
            "in_new_names_queue": False,
            "created_at": _now(),
            "created_by": actor,
            "decided_at": None,
            "decided_by": None,
        }
        try:
            await db[ndb.ENTITY_SUGGESTIONS].insert_one(doc)
            counts["created"] += 1
        except DuplicateKeyError:
            counts["existing"] += 1
    return counts


async def _resolve_in(db, kind: str, name: str, norm: str, ignored) -> tuple:
    """The shared matching rules for one name in one list; returns (resolution, exact alias or None)."""
    aliases = await db[ndb.ENTITY_ALIASES].find({"kind": kind, "alias_norm": norm}).to_list(length=None)
    exact = rs.exact_alias(aliases)
    found = await db[ndb.ENTITIES].find_one({"_id": exact["entity_id"]}) if exact else None
    return rs.resolve(name, ignored, aliases, found), exact


async def item_entities(db, source_type: str, source_id: str) -> dict:
    """Linked entities and open suggestions for one item."""
    _check_source_type(source_type)
    mentions = await db[ndb.ENTITY_MENTIONS].find(
        {"source_type": source_type, "source_id": source_id, "status": "linked"}).to_list(length=None)
    entity_ids = [m["entity_id"] for m in mentions]
    entities = {e["_id"]: e async for e in db[ndb.ENTITIES].find({"_id": {"$in": entity_ids}})}
    links = []
    for m in mentions:
        e = entities.get(m["entity_id"], {})
        links.append({**_out(m), "canonical_name": e.get("canonical_name"), "entity_type": e.get("type"),
                      "entity_status": e.get("status")})
    suggestions = await db[ndb.ENTITY_SUGGESTIONS].find(
        {"source_type": source_type, "source_id": source_id, "status": "open"}).sort("name_norm", 1).to_list(length=None)
    return {"links": links, "suggestions": [_out(s) for s in suggestions]}


# --- Link / unlink / dismiss ----------------------------------------------------

async def link(db, sources: ndb.ItemSources, source_type: str, source_id: str, entity_id: str,
               actor: str, suggestion_id: Optional[str] = None, activate: bool = False,
               link_source: str = "reviewer") -> dict:
    _check_source_type(source_type)
    entity = await db[ndb.ENTITIES].find_one({"_id": entity_id})
    if entity is None:
        raise NotFound(f"Entity {entity_id} not found")
    if entity["status"] == "ignored":
        raise Invalid("This entity is set to ignored and cannot be linked")
    if entity["status"] == "merged":
        raise Invalid(f'"{entity["canonical_name"]}" was merged into another entity; link that one instead')

    suggestion = None
    if suggestion_id:
        suggestion = await db[ndb.ENTITY_SUGGESTIONS].find_one(
            {"_id": suggestion_id, "source_type": source_type, "source_id": source_id})
        if suggestion is None:
            raise NotFound(f"Suggestion {suggestion_id} not found on this item")
        if suggestion["field"] != entity["kind"]:
            raise Invalid(f"A {suggestion['field']} name cannot be linked to a {entity['kind']}")

    info = await item_info(sources, source_type, source_id)
    if info is None:
        raise NotFound(f"{source_type} {source_id} not found")

    if entity["status"] == "staged":
        if not activate:
            raise Conflict("This entity is switched off; link with activate=true to switch it on")
        await ent.update_entity(db, entity_id, EntityUpdate(status="active"), actor)

    now = _now()
    existing = await db[ndb.ENTITY_MENTIONS].find_one(
        {"entity_id": entity_id, "source_type": source_type, "source_id": source_id})
    if existing is None or existing["status"] != "linked":
        fields = {
            "kind": entity["kind"],
            "status": "linked",
            "link_source": link_source,
            "matched_alias": suggestion.get("matched_alias") if suggestion else None,
            "suggestion_id": suggestion_id,
            "title": info["title"],
            "snippet": info["snippet"],
            "url": info["url"],
            "timestamp": info["timestamp"],
            "item_status": info["item_status"],
            "linked_by": actor,
            "linked_at": now,
            "removed_by": None,
            "removed_at": None,
        }
        try:
            await db[ndb.ENTITY_MENTIONS].update_one(
                {"entity_id": entity_id, "source_type": source_type, "source_id": source_id},
                {"$set": fields, "$setOnInsert": {"_id": str(uuid.uuid4()), "topics": [], "location_ids": []}},
                upsert=True,
            )
        except DuplicateKeyError:
            pass  # Linked at the same moment by another request; the result is the same.
        await ent.audit(db, actor, "link", entity_id, None, {"source_type": source_type, "source_id": source_id})

    # The chosen suggestion, and any other open one for this entity on the item, are now done.
    decided = {"status": "linked", "decided_at": now, "decided_by": actor}
    if suggestion is not None:
        await db[ndb.ENTITY_SUGGESTIONS].update_one(
            {"_id": suggestion_id}, {"$set": {**decided, "entity_id": entity_id, "in_new_names_queue": False}})
    await db[ndb.ENTITY_SUGGESTIONS].update_many(
        {"source_type": source_type, "source_id": source_id, "entity_id": entity_id, "status": "open"}, {"$set": decided})

    await refresh_item_locations(db, source_type, source_id)
    return await item_entities(db, source_type, source_id)


async def link_many(db, sources: ndb.ItemSources, requests: List[Tuple[str, str, str, str]], actor: str,
                    activate: bool = False) -> Dict[str, list]:
    """
    Links many (source_type, source_id, entity_id, suggestion_id) at once with link()'s rules, in a
    few database calls instead of about a dozen per link:
    - an ignored, merged or missing entity, or a switched-off one without activate, refuses everything
      before anything is written; a suggestion must be on its item and of the entity's kind
    - a link a reviewer removed on an item stays removed (that item's chosen suggestion is dismissed)
    - every new link is audited; chosen and other open suggestions for the entity on the item are closed
    - items that no longer exist are left alone
    Returns the (source_type, source_id, entity_id) keys per outcome: linked, already_linked,
    kept_removed, missing_items.
    """
    for source_type, *_ in requests:
        _check_source_type(source_type)
    requests = list(dict.fromkeys(requests))
    outcome = {"linked": [], "already_linked": [], "kept_removed": [], "missing_items": []}
    if not requests:
        return outcome

    entities = {e["_id"]: e async for e in db[ndb.ENTITIES].find({"_id": {"$in": list({r[2] for r in requests})}})}
    for entity_id in {r[2] for r in requests}:
        entity = entities.get(entity_id)
        if entity is None:
            raise NotFound(f"Entity {entity_id} not found")
        if entity["status"] == "ignored":
            raise Invalid("This entity is set to ignored and cannot be linked")
        if entity["status"] == "merged":
            raise Invalid(f'"{entity["canonical_name"]}" was merged into another entity; link that one instead')
        if entity["status"] == "staged" and not activate:
            raise Conflict("This entity is switched off; link with activate=true to switch it on")

    suggestions = {s["_id"]: s async for s in db[ndb.ENTITY_SUGGESTIONS].find(
        {"_id": {"$in": [r[3] for r in requests if r[3]]}})}
    for source_type, source_id, entity_id, suggestion_id in requests:
        if not suggestion_id:
            continue
        suggestion = suggestions.get(suggestion_id)
        if suggestion is None or (suggestion["source_type"], suggestion["source_id"]) != (source_type, source_id):
            raise NotFound(f"Suggestion {suggestion_id} not found on this item")
        if suggestion["field"] != entities[entity_id]["kind"]:
            raise Invalid(f"A {suggestion['field']} name cannot be linked to a {entities[entity_id]['kind']}")

    infos = {}
    for source_type in {r[0] for r in requests}:
        found = await items_info(sources, source_type, [r[1] for r in requests if r[0] == source_type])
        infos.update({(source_type, i): info for i, info in found.items()})

    for entity_id, entity in entities.items():
        if entity["status"] == "staged":
            await ent.update_entity(db, entity_id, EntityUpdate(status="active"), actor)

    existing = {(m["source_type"], m["source_id"], m["entity_id"]): m["status"]
                async for m in db[ndb.ENTITY_MENTIONS].find(
                    {"entity_id": {"$in": list(entities)}, **items_query((r[0], r[1]) for r in requests)},
                    {"source_type": 1, "source_id": 1, "entity_id": 1, "status": 1})}

    now = _now()
    upserts, audits, chosen_linked, chosen_dismissed = [], [], [], []
    for source_type, source_id, entity_id, suggestion_id in requests:
        key = (source_type, source_id, entity_id)
        info = infos.get((source_type, source_id))
        if info is None:
            outcome["missing_items"].append(key)
            continue
        state = existing.get(key)
        if state == "linked":
            outcome["already_linked"].append(key)
        elif state is not None:
            outcome["kept_removed"].append(key)
            if suggestion_id:
                chosen_dismissed.append(suggestion_id)
            continue
        else:
            outcome["linked"].append(key)
            suggestion = suggestions.get(suggestion_id) if suggestion_id else None
            upserts.append(UpdateOne(
                {"entity_id": entity_id, "source_type": source_type, "source_id": source_id},
                {"$set": {
                    "kind": entities[entity_id]["kind"], "status": "linked", "link_source": "reviewer",
                    "matched_alias": suggestion.get("matched_alias") if suggestion else None,
                    "suggestion_id": suggestion_id, "title": info["title"], "snippet": info["snippet"],
                    "url": info["url"], "timestamp": info["timestamp"], "item_status": info["item_status"],
                    "linked_by": actor, "linked_at": now, "removed_by": None, "removed_at": None},
                 "$setOnInsert": {"_id": str(uuid.uuid4()), "topics": [], "location_ids": []}},
                upsert=True))
            audits.append((entity_id, {"source_type": source_type, "source_id": source_id}))
        if suggestion_id:
            chosen_linked.append(suggestion_id)

    if upserts:
        try:
            await db[ndb.ENTITY_MENTIONS].bulk_write(upserts, ordered=False)
        except BulkWriteError as e:
            # Linked at the same moment by another request; the result is the same.
            if any(err.get("code") != 11000 for err in e.details.get("writeErrors", [])):
                raise
        await ent.audit_many(db, actor, "link", audits)

    decided = {"status": "linked", "decided_at": now, "decided_by": actor}
    if chosen_linked:
        # A chosen name (also an unknown one from the New Names queue) now points at the entity.
        chosen = set(chosen_linked)
        await db[ndb.ENTITY_SUGGESTIONS].bulk_write([
            UpdateOne({"_id": s}, {"$set": {**decided, "entity_id": e, "in_new_names_queue": False}})
            for _, _, e, s in requests if s in chosen], ordered=False)
    if chosen_dismissed:
        await db[ndb.ENTITY_SUGGESTIONS].update_many(
            {"_id": {"$in": chosen_dismissed}},
            {"$set": {"status": "dismissed", "in_new_names_queue": False, "decided_at": now, "decided_by": actor}})
    done = outcome["linked"] + outcome["already_linked"]
    if done:
        await db[ndb.ENTITY_SUGGESTIONS].bulk_write([
            UpdateMany({"source_type": t, "source_id": i, "entity_id": e, "status": "open"}, {"$set": decided})
            for t, i, e in done], ordered=False)
    if outcome["linked"]:
        # New links take the item's places (and new place links change them).
        await refresh_items_locations(db, ((t, i) for t, i, _ in outcome["linked"]))
    return outcome


async def link_all(db, sources: ndb.ItemSources, source_type: str, source_id: str, actor: str) -> dict:
    """Links every open suggestion that points at a switched-on entity (the reviewer's bulk action)."""
    _check_source_type(source_type)
    open_ids = await db[ndb.ENTITY_SUGGESTIONS].find(
        {"source_type": source_type, "source_id": source_id, "status": "open",
         "entity_id": {"$ne": None}}, {"entity_id": 1}).to_list(length=None)
    active = {e["_id"] async for e in db[ndb.ENTITIES].find(
        {"_id": {"$in": [s["entity_id"] for s in open_ids]}, "status": "active"}, {"_id": 1})}
    outcome = await link_many(db, sources, [(source_type, source_id, s["entity_id"], s["_id"])
                                            for s in open_ids if s["entity_id"] in active], actor)
    if outcome["missing_items"]:
        raise NotFound(f"{source_type} {source_id} not found")
    return await item_entities(db, source_type, source_id)


async def unlink(db, source_type: str, source_id: str, entity_id: str, actor: str) -> dict:
    _check_source_type(source_type)
    now = _now()
    res = await db[ndb.ENTITY_MENTIONS].update_one(
        {"entity_id": entity_id, "source_type": source_type, "source_id": source_id, "status": "linked"},
        {"$set": {"status": "removed", "removed_by": actor, "removed_at": now}},
    )
    if res.matched_count == 0:
        raise NotFound("This entity is not linked to this item")
    # Remembered: suggestions for this entity on this item are dismissed and never come back.
    await db[ndb.ENTITY_SUGGESTIONS].update_many(
        {"source_type": source_type, "source_id": source_id, "entity_id": entity_id},
        {"$set": {"status": "dismissed", "decided_at": now, "decided_by": actor}},
    )
    await ent.audit(db, actor, "unlink", entity_id, {"source_type": source_type, "source_id": source_id}, None)
    await refresh_item_locations(db, source_type, source_id)
    return await item_entities(db, source_type, source_id)


async def dismiss(db, suggestion_id: str, actor: str) -> dict:
    doc = await db[ndb.ENTITY_SUGGESTIONS].find_one({"_id": suggestion_id})
    if doc is None:
        raise NotFound(f"Suggestion {suggestion_id} not found")
    if doc["status"] != "open":
        raise Conflict(f"Suggestion is already {doc['status']}")
    await db[ndb.ENTITY_SUGGESTIONS].update_one(
        {"_id": suggestion_id},
        {"$set": {"status": "dismissed", "in_new_names_queue": False, "decided_at": _now(), "decided_by": actor}})
    return _out({**doc, "status": "dismissed", "in_new_names_queue": False})


# --- Item status and the new-names queue ---------------------------------------

async def on_item_status(db, source_type: str, source_id: str, new_status: str) -> None:
    """Keeps links' item status current; approval puts the item's unknown names into the new-names queue."""
    _check_source_type(source_type)
    await db[ndb.ENTITY_MENTIONS].update_many(
        {"source_type": source_type, "source_id": source_id}, {"$set": {"item_status": new_status}})
    queued = new_status == "approved"
    if new_status in ("rejected", "duplicated", "archived", "deleted"):
        # Composed pieces built on this item are flagged for re-checking (never changed automatically).
        await db[ndb.COMPOSE_RUNS].update_many(
            {"source_keys": f"{source_type}:{source_id}", "status": "succeeded", "source_rejected": False},
            {"$set": {"source_rejected": True, "source_rejected_at": _now(),
                      "source_rejected_key": f"{source_type}:{source_id}"}})
    await db[ndb.ENTITY_SUGGESTIONS].update_many(
        {"source_type": source_type, "source_id": source_id, "status": "open", "entity_id": None},
        {"$set": {"in_new_names_queue": queued}},
    )


async def item_changed(db, sources: ndb.ItemSources, source_type: str, source_id: str) -> bool:
    """
    Re-reads an item after another service changed it (status, text) and refreshes its links.
    The caller sends only ids; NewsAgent reads the item itself. Returns False if the item no longer exists.
    """
    _check_source_type(source_type)
    query = {"source_type": source_type, "source_id": source_id}
    info = await item_info(sources, source_type, source_id)
    if info is None:
        await db[ndb.ENTITY_MENTIONS].update_many(query, {"$set": {"item_status": "missing"}})
        await db[ndb.ENTITY_SUGGESTIONS].update_many(
            {**query, "status": "open", "entity_id": None}, {"$set": {"in_new_names_queue": False}})
        return False
    await on_item_status(db, source_type, source_id, info["item_status"])
    await db[ndb.ENTITY_MENTIONS].update_many(query, {"$set": {
        "title": info["title"], "snippet": info["snippet"], "url": info["url"], "timestamp": info["timestamp"]}})
    return True


async def items_summary(db, source_type: str, source_ids: List[str]) -> Dict[str, dict]:
    """Per item: number of confirmed links and of open suggestions (for badges in list screens)."""
    _check_source_type(source_type)
    ids = list(dict.fromkeys(source_ids))
    summary = {i: {"links": 0, "open_suggestions": 0} for i in ids}
    links = db[ndb.ENTITY_MENTIONS].aggregate([
        {"$match": {"source_type": source_type, "source_id": {"$in": ids}, "status": "linked"}},
        {"$group": {"_id": "$source_id", "n": {"$sum": 1}}},
    ])
    async for row in await links:
        summary[row["_id"]]["links"] = row["n"]
    suggestions = db[ndb.ENTITY_SUGGESTIONS].aggregate([
        {"$match": {"source_type": source_type, "source_id": {"$in": ids}, "status": "open"}},
        {"$group": {"_id": "$source_id", "n": {"$sum": 1}}},
    ])
    async for row in await suggestions:
        summary[row["_id"]]["open_suggestions"] = row["n"]
    return summary


async def list_new_names(db, limit: int = 100, sources: Optional[ndb.ItemSources] = None,
                         items_per_group: int = 5) -> List[dict]:
    """Unknown names from approved items, grouped by name, most frequent first.
    Each group lists its first few items (with titles) and the full count."""
    pipeline = [
        {"$match": {"status": "open", "entity_id": None, "in_new_names_queue": True}},
        {"$sort": {"created_at": 1}},
        {"$group": {
            "_id": {"field": "$field", "name_norm": "$name_norm"},
            "name": {"$first": "$name"},
            "count": {"$sum": 1},
            "items": {"$push": {"source_type": "$source_type", "source_id": "$source_id", "suggestion_id": "$_id"}},
        }},
        {"$sort": {"count": -1, "_id.name_norm": 1}},
        {"$limit": limit},
        # A name found on hundreds of items sends back only its first few examples.
        {"$project": {"name": 1, "count": 1, "items": {"$slice": ["$items", items_per_group]}}},
    ]
    groups = await (await db[ndb.ENTITY_SUGGESTIONS].aggregate(pipeline)).to_list(length=limit)
    if sources is not None:
        # Titles so a reviewer can see where the name was found: one batch per item type.
        for source_type in SOURCE_TYPES:
            ids = [i["source_id"] for g in groups for i in g["items"] if i["source_type"] == source_type]
            infos = await items_info(sources, source_type, ids) if ids else {}
            for g in groups:
                for item in g["items"]:
                    if item["source_type"] == source_type:
                        item["title"] = (infos.get(item["source_id"]) or {}).get("title")
    return [{"field": g["_id"]["field"], "name_norm": g["_id"]["name_norm"], "name": g["name"],
             "count": g["count"], "items": g["items"]} for g in groups]


async def link_new_name(db, sources: ndb.ItemSources, field: str, name_norm: str, entity_id: str, actor: str,
                        activate: bool = False) -> int:
    """Links every queued item carrying this unknown name to the chosen entity. Returns the number linked."""
    queued = await db[ndb.ENTITY_SUGGESTIONS].find(
        {"status": "open", "entity_id": None, "in_new_names_queue": True, "field": field, "name_norm": name_norm}
    ).to_list(length=None)
    if not queued:
        raise NotFound("No queued items carry that name")
    outcome = await link_many(db, sources, [(s["source_type"], s["source_id"], entity_id, s["_id"]) for s in queued],
                              actor, activate=activate)
    if outcome["missing_items"]:
        # Items deleted since they were queued leave the queue.
        missing = {(t, i) for t, i, _ in outcome["missing_items"]}
        await db[ndb.ENTITY_SUGGESTIONS].update_many(
            {"_id": {"$in": [s["_id"] for s in queued if (s["source_type"], s["source_id"]) in missing]}},
            {"$set": {"in_new_names_queue": False}})
    return len(outcome["linked"]) + len(outcome["already_linked"])


async def dismiss_new_name(db, field: str, name_norm: str, actor: str) -> int:
    res = await db[ndb.ENTITY_SUGGESTIONS].update_many(
        {"status": "open", "entity_id": None, "in_new_names_queue": True, "field": field, "name_norm": name_norm},
        {"$set": {"status": "dismissed", "in_new_names_queue": False, "decided_at": _now(), "decided_by": actor}},
    )
    if res.modified_count == 0:
        raise NotFound("No queued items carry that name")
    return res.modified_count
