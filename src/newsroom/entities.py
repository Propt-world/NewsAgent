"""
Entity list operations used by the entity manager.

Rules enforced here:
- an exact alias belongs to one entity per kind; a name is never exact on one
  entity and ambiguous on another; websites/handles and ignored strings are
  never aliases
- an entity with type "unknown" cannot be switched on
- parents are the same kind and never form a loop; path_ids (ancestors + self)
  are recomputed for the whole subtree when a parent changes
- entities are switched off (status) instead of deleted; every change is audited
- delete needs the word "delete"; it removes the entity, its aliases and links
  (never the articles or signals), is refused while other entries depend on
  it, and keeps a full copy in the audit log so the seed never re-adds it
- a merge moves a duplicate's aliases, links, suggestions and children to the
  kept entity; the duplicate stays as status "merged" (so the seed never
  re-creates it) and can no longer be edited or linked
"""
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from pymongo import UpdateMany, UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError

from src.newsroom import db as ndb
from src.newsroom.locations import items_query, refresh_items_for_places, refresh_items_locations
from src.newsroom.models import AliasCreate, EntityCreate, EntityUpdate, IgnoreCreate
from src.newsroom.normalize import looks_like_handle, normalize_alias, raw_key


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _alias_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "alias": doc["alias"],
        "alias_norm": doc["alias_norm"],
        "ambiguous": doc.get("ambiguous", False),
        "lang": doc.get("lang"),
    }


def _entity_out(doc: dict, aliases: List[dict]) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    out["aliases"] = [_alias_out(a) for a in aliases]
    return out


async def audit(db, actor: str, action: str, entity_id: Optional[str], before=None, after=None) -> None:
    await db[ndb.ENTITY_AUDIT].insert_one({
        "_id": str(uuid.uuid4()),
        "at": _now(),
        "actor": actor,
        "action": action,
        "entity_id": entity_id,
        "before": before,
        "after": after,
    })


async def audit_many(db, actor: str, action: str, records: List[tuple]) -> None:
    """One audit entry per (entity_id, after) record, written in one call."""
    if records:
        now = _now()
        await db[ndb.ENTITY_AUDIT].insert_many([{
            "_id": str(uuid.uuid4()), "at": now, "actor": actor, "action": action,
            "entity_id": entity_id, "before": None, "after": after} for entity_id, after in records])


async def _aliases_for(db, entity_ids: List[str]) -> dict:
    grouped = {eid: [] for eid in entity_ids}
    cursor = db[ndb.ENTITY_ALIASES].find({"entity_id": {"$in": entity_ids}}).sort([("ambiguous", 1), ("alias", 1)])
    async for alias in cursor:
        grouped.setdefault(alias["entity_id"], []).append(alias)
    return grouped


async def _entity_label(db, entity_id: str) -> str:
    """Name for messages, e.g. "DAMAC Properties"; falls back to the id."""
    doc = await db[ndb.ENTITIES].find_one({"_id": entity_id}, {"canonical_name": 1})
    return f'"{doc["canonical_name"]}"' if doc else f"entity {entity_id}"


async def _load(db, entity_id: str) -> dict:
    doc = await db[ndb.ENTITIES].find_one({"_id": entity_id})
    if doc is None:
        raise NotFound(f"Entity {entity_id} not found")
    return doc


async def _load_editable(db, entity_id: str) -> dict:
    doc = await _load(db, entity_id)
    if doc.get("status") == "merged":
        raise Invalid(f'"{doc["canonical_name"]}" was merged into {await _entity_label(db, doc.get("merged_into"))}; '
                      "edit that one instead")
    return doc


# --- Reads ---------------------------------------------------------------

async def list_entities(db, kind=None, status=None, etype=None, q=None, limit=100, skip=0) -> dict:
    query = {}
    if kind:
        query["kind"] = kind
    # Merged duplicates are listed only when asked for.
    query["status"] = status if status else {"$ne": "merged"}
    if etype:
        query["type"] = etype
    if q:
        pattern = re.escape(normalize_alias(q))
        alias_match = {"alias_norm": {"$regex": pattern}}
        if kind:
            alias_match["kind"] = kind
        alias_ids = await db[ndb.ENTITY_ALIASES].distinct("entity_id", alias_match)
        query["$or"] = [{"canonical_norm": {"$regex": pattern}}, {"_id": {"$in": alias_ids}}]

    total = await db[ndb.ENTITIES].count_documents(query)
    docs = await db[ndb.ENTITIES].find(query).sort([("kind", 1), ("canonical_norm", 1)]).skip(skip).limit(limit).to_list(length=limit)
    aliases = await _aliases_for(db, [d["_id"] for d in docs])
    return {
        "items": [_entity_out(d, aliases.get(d["_id"], [])) for d in docs],
        "total": total,
        "limit": limit,
        "skip": skip,
    }


async def get_entity(db, entity_id: str) -> dict:
    doc = await _load(db, entity_id)
    aliases = await _aliases_for(db, [entity_id])
    return _entity_out(doc, aliases.get(entity_id, []))


async def list_audit(db, entity_id: str, limit: int = 50) -> List[dict]:
    await _load(db, entity_id)
    docs = await db[ndb.ENTITY_AUDIT].find({"entity_id": entity_id}).sort("at", -1).limit(limit).to_list(length=limit)
    return [{**{k: v for k, v in d.items() if k != "_id"}, "id": d["_id"]} for d in docs]


# --- Validation ----------------------------------------------------------

class AliasSnapshot:
    """One kind's aliases and the ignore list, read once, so checking thousands of names (CSV import)
    needs no lookup per name. Same rules as _check_alias without it."""

    def __init__(self, exact: dict, marked: dict, ignored: set):
        self.exact, self.marked, self.ignored = exact, marked, ignored

    @classmethod
    async def load(cls, db, kind: str) -> "AliasSnapshot":
        exact, marked = {}, {}
        async for a in db[ndb.ENTITY_ALIASES].find({"kind": kind}, {"alias_norm": 1, "ambiguous": 1, "entity_id": 1}):
            (marked if a.get("ambiguous") else exact).setdefault(a["alias_norm"], a)
        ignored = set(await db[ndb.ENTITY_IGNORE].distinct("string_key"))
        return cls(exact, marked, ignored)


async def _check_alias(db, kind: str, alias: str, ambiguous: bool, entity_id: Optional[str],
                       snapshot: Optional[AliasSnapshot] = None) -> str:
    norm = normalize_alias(alias)
    if not norm:
        raise Invalid(f"Alias '{alias}' is empty after normalising")
    if looks_like_handle(alias):
        raise Invalid(f"'{alias}' looks like a website or social handle; those are never entities")
    ignored = (raw_key(alias) in snapshot.ignored) if snapshot else \
        await db[ndb.ENTITY_IGNORE].find_one({"string_key": raw_key(alias)})
    if ignored:
        raise Conflict(f"'{alias}' is on the ignore list")
    if entity_id and await db[ndb.ENTITY_ALIASES].find_one({"entity_id": entity_id, "alias_norm": norm}):
        raise Conflict(f"'{alias}' is already an alias of this entity")
    exact = snapshot.exact.get(norm) if snapshot else \
        await db[ndb.ENTITY_ALIASES].find_one({"kind": kind, "alias_norm": norm, "ambiguous": False})
    if exact:
        raise Conflict(f"'{alias}' is already an exact alias of {await _entity_label(db, exact['entity_id'])}")
    if ambiguous:
        marked = None
    else:
        marked = snapshot.marked.get(norm) if snapshot else \
            await db[ndb.ENTITY_ALIASES].find_one({"kind": kind, "alias_norm": norm, "ambiguous": True})
    if marked:
        raise Conflict(f"'{alias}' is marked ambiguous on {await _entity_label(db, marked['entity_id'])}, "
                       "so it cannot be an exact alias")
    return norm


def _check_active_type(status: str, etype: str) -> None:
    if status == "active" and etype == "unknown":
        raise Invalid("Choose a type before switching this entity on")


async def _check_parent(db, kind: str, parent_id: Optional[str], entity_id: Optional[str]) -> Optional[dict]:
    if not parent_id:
        return None
    parent = await db[ndb.ENTITIES].find_one({"_id": parent_id})
    if parent is None:
        raise Invalid(f"Parent {parent_id} not found")
    if parent.get("status") == "merged":
        raise Invalid(f'"{parent["canonical_name"]}" was merged; choose the entity it was merged into')
    if parent["kind"] != kind:
        raise Invalid("Parent must be the same kind (organisation or location)")
    if entity_id and (parent_id == entity_id or entity_id in parent.get("path_ids", [])):
        raise Invalid("That parent would create a loop")
    return parent


async def _check_country(db, kind: str, country_id: Optional[str]) -> None:
    if not country_id:
        return
    if kind != "organisation":
        raise Invalid("Only organisations have a country")
    country = await db[ndb.ENTITIES].find_one({"_id": country_id})
    if country is None or country["kind"] != "location" or country["type"] != "country" \
            or country.get("status") == "merged":
        raise Invalid(f"Country {country_id} is not a country location")


def _alias_doc(kind: str, entity_id: str, alias: str, norm: str, ambiguous: bool, actor: str) -> dict:
    return {
        "_id": str(uuid.uuid4()),
        "entity_id": entity_id,
        "kind": kind,
        "alias": alias.strip(),
        "alias_norm": norm,
        "ambiguous": ambiguous,
        "lang": "ar" if any("؀" <= ch <= "ۿ" for ch in alias) else "en",
        "source": "reviewer",
        "created_at": _now(),
        "created_by": actor,
    }


# --- Writes --------------------------------------------------------------

async def create_entity(db, payload: EntityCreate, actor: str) -> dict:
    name = payload.canonical_name.strip()
    canonical_norm = normalize_alias(name)
    if not canonical_norm:
        raise Invalid("Canonical name is empty after normalising")
    _check_active_type(payload.status, payload.type)
    if await db[ndb.ENTITIES].find_one({"kind": payload.kind, "canonical_norm": canonical_norm}):
        raise Conflict(f"A {payload.kind} named '{name}' already exists")
    parent = await _check_parent(db, payload.kind, payload.parent_id, None)
    await _check_country(db, payload.kind, payload.country_id)

    # The canonical name is an exact alias unless it is marked ambiguous (same rule as the seed).
    ambiguous = [a.strip() for a in payload.ambiguous_aliases if a.strip()]
    ambiguous_norms = {normalize_alias(a) for a in ambiguous}
    exact = [a.strip() for a in payload.aliases if a.strip()]
    if canonical_norm not in ambiguous_norms:
        exact = [name] + exact

    entity_id = str(uuid.uuid4())
    alias_docs, exact_norms, done_ambiguous = [], set(), set()
    for alias in exact:
        if normalize_alias(alias) in exact_norms:
            continue
        norm = await _check_alias(db, payload.kind, alias, False, None)
        exact_norms.add(norm)
        alias_docs.append(_alias_doc(payload.kind, entity_id, alias, norm, False, actor))
    for alias in ambiguous:
        norm = normalize_alias(alias)
        if norm in exact_norms:
            raise Invalid(f"'{alias}' cannot be both an exact and an ambiguous alias")
        if norm in done_ambiguous:
            continue
        norm = await _check_alias(db, payload.kind, alias, True, None)
        done_ambiguous.add(norm)
        alias_docs.append(_alias_doc(payload.kind, entity_id, alias, norm, True, actor))

    now = _now()
    doc = {
        "_id": entity_id,
        "kind": payload.kind,
        "type": payload.type.strip(),
        "status": payload.status,
        "canonical_name": name,
        "canonical_norm": canonical_norm,
        "parent_id": payload.parent_id or None,
        "path_ids": (parent["path_ids"] if parent else []) + [entity_id],
        "country_id": payload.country_id or None,
        "context_slug": None,
        "notes": payload.notes,
        "mentions_in_data": 0,
        "curated": True,
        "seed": False,
        "created_at": now,
        "created_by": actor,
        "updated_at": now,
        "updated_by": actor,
    }
    try:
        await db[ndb.ENTITIES].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict(f"A {payload.kind} named '{name}' already exists")
    try:
        if alias_docs:
            await db[ndb.ENTITY_ALIASES].insert_many(alias_docs)
    except DuplicateKeyError:
        # Lost a race with another write: undo so no half-created entity remains.
        await db[ndb.ENTITY_ALIASES].delete_many({"entity_id": entity_id})
        await db[ndb.ENTITIES].delete_one({"_id": entity_id})
        raise Conflict("One of the aliases was taken by another entity at the same time; try again")

    await audit(db, actor, "create", entity_id, None, {
        "canonical_name": name, "kind": payload.kind, "type": doc["type"], "status": payload.status,
        "parent_id": doc["parent_id"], "country_id": doc["country_id"],
        "aliases": [a["alias"] for a in alias_docs if not a["ambiguous"]],
        "ambiguous_aliases": [a["alias"] for a in alias_docs if a["ambiguous"]],
    })
    return await get_entity(db, entity_id)


async def update_entity(db, entity_id: str, payload: EntityUpdate, actor: str) -> dict:
    current = await _load_editable(db, entity_id)
    sent = payload.model_fields_set
    changes = {}

    if "canonical_name" in sent and payload.canonical_name is not None:
        name = payload.canonical_name.strip()
        norm = normalize_alias(name)
        if not norm:
            raise Invalid("Canonical name is empty after normalising")
        if norm != current["canonical_norm"]:
            other = await db[ndb.ENTITIES].find_one({"kind": current["kind"], "canonical_norm": norm, "_id": {"$ne": entity_id}})
            if other:
                raise Conflict(f"A {current['kind']} named '{name}' already exists")
        changes["canonical_name"] = name
        changes["canonical_norm"] = norm
    if "type" in sent and payload.type is not None:
        changes["type"] = payload.type.strip()
    if "status" in sent and payload.status is not None:
        changes["status"] = payload.status
    if "notes" in sent:
        changes["notes"] = payload.notes
    if "country_id" in sent:
        await _check_country(db, current["kind"], payload.country_id)
        changes["country_id"] = payload.country_id or None

    _check_active_type(changes.get("status", current["status"]), changes.get("type", current["type"]))

    parent_changed = "parent_id" in sent and (payload.parent_id or None) != current.get("parent_id")
    if parent_changed:
        parent = await _check_parent(db, current["kind"], payload.parent_id, entity_id)
        changes["parent_id"] = payload.parent_id or None
        changes["path_ids"] = (parent["path_ids"] if parent else []) + [entity_id]

    changes = {k: v for k, v in changes.items() if current.get(k) != v}
    if not changes:
        return await get_entity(db, entity_id)

    changes["updated_at"] = _now()
    changes["updated_by"] = actor
    try:
        await db[ndb.ENTITIES].update_one({"_id": entity_id}, {"$set": changes})
    except DuplicateKeyError:
        raise Conflict("Another entity with that name was created at the same time")

    if parent_changed:
        # Rewrite the ancestor path of every descendant (they all contain this entity's id).
        new_prefix = changes["path_ids"]
        moved, path_updates = [entity_id], []
        async for child in db[ndb.ENTITIES].find({"path_ids": entity_id, "_id": {"$ne": entity_id}}, {"path_ids": 1}):
            old = child["path_ids"]
            path_updates.append(UpdateOne({"_id": child["_id"]},
                                          {"$set": {"path_ids": new_prefix + old[old.index(entity_id) + 1:]}}))
            moved.append(child["_id"])
        if path_updates:
            await db[ndb.ENTITIES].bulk_write(path_updates, ordered=False)
        if current["kind"] == "location":
            # Links store each place's ancestors, so items with any moved place are recomputed.
            await refresh_items_for_places(db, moved)

    audited = {k: v for k, v in changes.items() if k not in ("updated_at", "updated_by", "canonical_norm", "path_ids")}
    await audit(db, actor, "update", entity_id, {k: current.get(k) for k in audited}, audited)
    return await get_entity(db, entity_id)


async def add_alias(db, entity_id: str, payload: AliasCreate, actor: str) -> dict:
    entity = await _load_editable(db, entity_id)
    norm = await _check_alias(db, entity["kind"], payload.alias, payload.ambiguous, entity_id)
    doc = _alias_doc(entity["kind"], entity_id, payload.alias, norm, payload.ambiguous, actor)
    try:
        await db[ndb.ENTITY_ALIASES].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict(f"'{payload.alias}' was taken by another entity at the same time")
    await audit(db, actor, "add_alias", entity_id, None, {"alias": doc["alias"], "ambiguous": doc["ambiguous"]})
    return await get_entity(db, entity_id)


async def delete_alias(db, entity_id: str, alias_id: str, actor: str) -> dict:
    await _load_editable(db, entity_id)
    alias = await db[ndb.ENTITY_ALIASES].find_one({"_id": alias_id, "entity_id": entity_id})
    if alias is None:
        raise NotFound(f"Alias {alias_id} not found on entity {entity_id}")
    await db[ndb.ENTITY_ALIASES].delete_one({"_id": alias_id})
    await audit(db, actor, "delete_alias", entity_id, {"alias": alias["alias"], "ambiguous": alias.get("ambiguous", False)}, None)
    return await get_entity(db, entity_id)


# --- Merge ---------------------------------------------------------------

async def merge_entities(db, entity_id: str, into_id: str, actor: str, dry_run: bool = False) -> dict:
    """
    Merges a duplicate (entity_id) into the entity that is kept (into_id).

    Where both had a decision on the same item, the kept entity's decision
    stays, so a reviewer's "remove" is never turned back into a link. Steps
    are safe to repeat: if a merge stops halfway, the same merge finishes it.
    """
    if entity_id == into_id:
        raise Invalid("Choose a different entity to merge into")
    source = await _load(db, entity_id)
    target = await _load(db, into_id)
    resuming = source.get("status") == "merged"
    if resuming and source.get("merged_into") != into_id:
        raise Conflict(f'"{source["canonical_name"]}" was already merged into '
                       f'{await _entity_label(db, source.get("merged_into"))}')
    if target.get("status") == "merged":
        raise Invalid(f'"{target["canonical_name"]}" was itself merged; choose the entity it was merged into')
    if source["kind"] != target["kind"]:
        raise Invalid("A company or person cannot be merged with a place")
    if into_id in source.get("path_ids", []) or entity_id in target.get("path_ids", []):
        raise Invalid(f'"{target["canonical_name"]}" and "{source["canonical_name"]}" are parent and child, '
                      "not duplicates; change the parent instead")
    countries = await db[ndb.ENTITIES].count_documents({"country_id": entity_id})
    if countries and target.get("type") != "country":
        raise Invalid(f'{countries} companies use "{source["canonical_name"]}" as their country, '
                      f'but "{target["canonical_name"]}" is not a country')

    aliases = await db[ndb.ENTITY_ALIASES].find({"entity_id": entity_id}).to_list(length=None)
    target_norms = set(await db[ndb.ENTITY_ALIASES].distinct("alias_norm", {"entity_id": into_id}))
    alias_moves = [a for a in aliases if a["alias_norm"] not in target_norms]
    alias_drops = [a for a in aliases if a["alias_norm"] in target_norms]

    mentions = await db[ndb.ENTITY_MENTIONS].find({"entity_id": entity_id}).to_list(length=None)
    target_items = {(m["source_type"], m["source_id"]) async for m in db[ndb.ENTITY_MENTIONS].find(
        {"entity_id": into_id}, {"source_type": 1, "source_id": 1})}
    both = [m for m in mentions if (m["source_type"], m["source_id"]) in target_items]
    moves = [m for m in mentions if (m["source_type"], m["source_id"]) not in target_items]

    suggestions = await db[ndb.ENTITY_SUGGESTIONS].count_documents({"entity_id": entity_id})
    children = await db[ndb.ENTITIES].find(
        {"parent_id": entity_id, "status": {"$ne": "merged"}}, {"_id": 1}).to_list(length=None)

    result = {
        "dry_run": dry_run,
        "merged_id": entity_id,
        "into_id": into_id,
        "aliases_moved": len(alias_moves),
        "aliases_already_there": len(alias_drops),
        "links_moved": len(moves),
        "links_both_decided": len(both),
        "suggestions_moved": suggestions,
        "children_moved": len(children),
        "countries_moved": countries,
    }
    if dry_run:
        return {**result, "entity": await get_entity(db, into_id)}

    # 1. The duplicate stops taking new links or edits before anything moves.
    now = _now()
    if not resuming:
        res = await db[ndb.ENTITIES].update_one(
            {"_id": entity_id, "status": {"$ne": "merged"}},
            {"$set": {"status": "merged", "merged_into": into_id, "merged_at": now, "merged_by": actor,
                      "status_before_merge": source["status"], "updated_at": now, "updated_by": actor}},
        )
        if res.modified_count == 0:
            raise Conflict(f'"{source["canonical_name"]}" was merged by someone else at the same time')

    # 2. Names: moving keeps alias_norm, so the exact-alias unique index is never hit.
    if alias_drops:
        await db[ndb.ENTITY_ALIASES].delete_many({"_id": {"$in": [a["_id"] for a in alias_drops]}})
    for alias in alias_moves:
        try:
            await db[ndb.ENTITY_ALIASES].update_one({"_id": alias["_id"]}, {"$set": {"entity_id": into_id}})
        except DuplicateKeyError:
            await db[ndb.ENTITY_ALIASES].delete_one({"_id": alias["_id"]})  # added to the target meanwhile

    # 3. Links: the kept entity's decision wins where both had one.
    if both:
        await db[ndb.ENTITY_MENTIONS].delete_many({"_id": {"$in": [m["_id"] for m in both]}})
    if moves:
        try:
            await db[ndb.ENTITY_MENTIONS].bulk_write(
                [UpdateOne({"_id": m["_id"]}, {"$set": {"entity_id": into_id}}) for m in moves], ordered=False)
        except BulkWriteError as e:
            errors = e.details.get("writeErrors", [])
            if any(err.get("code") != 11000 for err in errors):
                raise
            # Linked to the target meanwhile: the target's decision stays.
            await db[ndb.ENTITY_MENTIONS].delete_many({"_id": {"$in": [moves[err["index"]]["_id"] for err in errors]}})

    # 4. Suggestions point at the kept entity; open ones on items it is already decided on are closed the same way.
    touched = await db[ndb.ENTITY_SUGGESTIONS].find(
        {"entity_id": entity_id}, {"source_type": 1, "source_id": 1}).to_list(length=None)
    await db[ndb.ENTITY_SUGGESTIONS].update_many(
        {"entity_id": entity_id}, {"$set": {"entity_id": into_id, "entity_status": target["status"]}})
    touched_items = {(s["source_type"], s["source_id"]) for s in touched}
    decisions = {}
    if touched_items:
        async for m in db[ndb.ENTITY_MENTIONS].find(
            {"entity_id": into_id, **items_query(touched_items)}, {"source_type": 1, "source_id": 1, "status": 1}
        ):
            decisions[(m["source_type"], m["source_id"])] = m["status"]
    if decisions:
        await db[ndb.ENTITY_SUGGESTIONS].bulk_write([
            UpdateMany({"entity_id": into_id, "source_type": t, "source_id": i, "status": "open"},
                       {"$set": {"status": "linked" if status == "linked" else "dismissed",
                                 "decided_at": now, "decided_by": actor}})
            for (t, i), status in decisions.items()
        ], ordered=False)

    # 5. Children move under the kept entity (their paths and items' places are recomputed).
    for child in children:
        await update_entity(db, child["_id"], EntityUpdate(parent_id=into_id), actor)
    if countries:
        await db[ndb.ENTITIES].update_many({"country_id": entity_id}, {"$set": {"country_id": into_id}})

    # 6. Items that had the duplicate place now carry the kept place's decision and ancestors.
    if source["kind"] == "location":
        await refresh_items_locations(db, ((m["source_type"], m["source_id"]) for m in mentions))

    audited = {k: v for k, v in result.items() if k not in ("dry_run", "merged_id", "into_id")}
    status_before = source.get("status_before_merge") if resuming else source["status"]
    await audit(db, actor, "merge_into", entity_id, {"status": status_before},
                {"merged_into": into_id, "into_name": target["canonical_name"], **audited})
    await audit(db, actor, "merge_from", into_id, None,
                {"merged_id": entity_id, "merged_name": source["canonical_name"], **audited})
    return {**result, "entity": await get_entity(db, into_id)}


# --- Delete --------------------------------------------------------------

DELETE_WORD = "delete"


async def delete_entity(db, entity_id: str, actor: str, confirm: Optional[str], dry_run: bool = False) -> dict:
    """
    Deletes an entity for good. Articles and signals are never touched: only the
    entity, its aliases and its links go; open suggestions for it become unknown names.
    """
    entity = await _load(db, entity_id)
    name = entity["canonical_name"]
    if entity.get("status") == "merged":
        raise Invalid(f'"{name}" is a merged record; it has no names or links to delete')
    children = await db[ndb.ENTITIES].count_documents({"parent_id": entity_id, "status": {"$ne": "merged"}})
    if children:
        raise Invalid(f'{children} {"entry is" if children == 1 else "entries are"} inside "{name}"; '
                      "move or delete them first")
    countries = await db[ndb.ENTITIES].count_documents({"country_id": entity_id})
    if countries:
        raise Invalid(f'{countries} {"company uses" if countries == 1 else "companies use"} "{name}" as their country; '
                      "change that first")

    aliases = await db[ndb.ENTITY_ALIASES].find({"entity_id": entity_id}).to_list(length=None)
    mentions = await db[ndb.ENTITY_MENTIONS].find(
        {"entity_id": entity_id}, {"source_type": 1, "source_id": 1, "status": 1}).to_list(length=None)
    open_query = {"entity_id": entity_id, "status": "open"}
    result = {
        "dry_run": dry_run,
        "entity_id": entity_id,
        "canonical_name": name,
        "aliases_removed": len(aliases),
        "links_removed": sum(1 for m in mentions if m["status"] == "linked"),
        "decisions_removed": sum(1 for m in mentions if m["status"] != "linked"),
        "suggestions_reset": await db[ndb.ENTITY_SUGGESTIONS].count_documents(open_query),
    }
    if dry_run:
        return result
    if (confirm or "").strip().lower() != DELETE_WORD:
        raise Invalid(f'Type "{DELETE_WORD}" to confirm')

    # The full copy goes in first, so the record exists even if a later step fails.
    snapshot = {k: v for k, v in entity.items() if k != "_id"}
    snapshot["aliases"] = [{"alias": a["alias"], "ambiguous": a.get("ambiguous", False)} for a in aliases]
    counts = {k: v for k, v in result.items() if k not in ("dry_run", "entity_id", "canonical_name")}
    await audit(db, actor, "delete", entity_id, snapshot, counts)

    await db[ndb.ENTITY_SUGGESTIONS].update_many(open_query, {"$set": {
        "entity_id": None, "entity_status": None, "matched_alias": None,
        "reason": "not in the list (entry was deleted)"}})
    await db[ndb.ENTITY_MENTIONS].delete_many({"entity_id": entity_id})
    await db[ndb.ENTITY_ALIASES].delete_many({"entity_id": entity_id})
    await db[ndb.ENTITIES].delete_one({"_id": entity_id})
    if entity["kind"] == "location":
        await refresh_items_locations(db, ((m["source_type"], m["source_id"]) for m in mentions if m["status"] == "linked"))
    return result


# --- Ignore list ---------------------------------------------------------

def _ignore_out(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "string": doc["string"],
        "reason": doc.get("reason"),
        "created_at": doc.get("created_at"),
        "created_by": doc.get("created_by"),
    }


async def list_ignore(db) -> List[dict]:
    docs = await db[ndb.ENTITY_IGNORE].find({}).sort("string_key", 1).to_list(length=None)
    return [_ignore_out(d) for d in docs]


async def add_ignore(db, payload: IgnoreCreate, actor: str) -> dict:
    string = payload.string.strip()
    key = raw_key(string)
    if not key:
        raise Invalid("Empty string")
    async for alias in db[ndb.ENTITY_ALIASES].find({"alias_norm": normalize_alias(string)}):
        if raw_key(alias["alias"]) == key:
            raise Conflict(f"'{string}' is an alias of {await _entity_label(db, alias['entity_id'])}; remove the alias first")
    doc = {
        "_id": str(uuid.uuid4()),
        "string": string,
        "string_key": key,
        "reason": payload.reason,
        "created_at": _now(),
        "created_by": actor,
    }
    try:
        await db[ndb.ENTITY_IGNORE].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict(f"'{string}' is already on the ignore list")
    await audit(db, actor, "add_ignore", None, None, {"string": string, "reason": payload.reason})
    return _ignore_out(doc)


async def delete_ignore(db, ignore_id: str, actor: str) -> None:
    doc = await db[ndb.ENTITY_IGNORE].find_one({"_id": ignore_id})
    if doc is None:
        raise NotFound(f"Ignore entry {ignore_id} not found")
    await db[ndb.ENTITY_IGNORE].delete_one({"_id": ignore_id})
    await audit(db, actor, "delete_ignore", None, {"string": doc["string"]}, None)
