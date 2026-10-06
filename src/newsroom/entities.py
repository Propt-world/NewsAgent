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
"""
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from pymongo.errors import DuplicateKeyError

from src.newsroom import db as ndb
from src.newsroom.locations import refresh_items_for_places
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


async def _aliases_for(db, entity_ids: List[str]) -> dict:
    grouped = {eid: [] for eid in entity_ids}
    cursor = db[ndb.ENTITY_ALIASES].find({"entity_id": {"$in": entity_ids}}).sort([("ambiguous", 1), ("alias", 1)])
    async for alias in cursor:
        grouped.setdefault(alias["entity_id"], []).append(alias)
    return grouped


async def _load(db, entity_id: str) -> dict:
    doc = await db[ndb.ENTITIES].find_one({"_id": entity_id})
    if doc is None:
        raise NotFound(f"Entity {entity_id} not found")
    return doc


# --- Reads ---------------------------------------------------------------

async def list_entities(db, kind=None, status=None, etype=None, q=None, limit=100, skip=0) -> dict:
    query = {}
    if kind:
        query["kind"] = kind
    if status:
        query["status"] = status
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

async def _check_alias(db, kind: str, alias: str, ambiguous: bool, entity_id: Optional[str]) -> str:
    norm = normalize_alias(alias)
    if not norm:
        raise Invalid(f"Alias '{alias}' is empty after normalising")
    if looks_like_handle(alias):
        raise Invalid(f"'{alias}' looks like a website or social handle; those are never entities")
    if await db[ndb.ENTITY_IGNORE].find_one({"string_key": raw_key(alias)}):
        raise Conflict(f"'{alias}' is on the ignore list")
    if entity_id and await db[ndb.ENTITY_ALIASES].find_one({"entity_id": entity_id, "alias_norm": norm}):
        raise Conflict(f"'{alias}' is already an alias of this entity")
    exact = await db[ndb.ENTITY_ALIASES].find_one({"kind": kind, "alias_norm": norm, "ambiguous": False})
    if exact:
        raise Conflict(f"'{alias}' is already an exact alias of entity {exact['entity_id']}")
    if not ambiguous and await db[ndb.ENTITY_ALIASES].find_one({"kind": kind, "alias_norm": norm, "ambiguous": True}):
        raise Conflict(f"'{alias}' is marked ambiguous on another entity, so it cannot be an exact alias")
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
    if country is None or country["kind"] != "location" or country["type"] != "country":
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
    current = await _load(db, entity_id)
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
        moved = [entity_id]
        async for child in db[ndb.ENTITIES].find({"path_ids": entity_id, "_id": {"$ne": entity_id}}):
            old = child["path_ids"]
            new_path = new_prefix + old[old.index(entity_id) + 1:]
            await db[ndb.ENTITIES].update_one({"_id": child["_id"]}, {"$set": {"path_ids": new_path}})
            moved.append(child["_id"])
        if current["kind"] == "location":
            # Links store each place's ancestors, so items with any moved place are recomputed.
            await refresh_items_for_places(db, moved)

    audited = {k: v for k, v in changes.items() if k not in ("updated_at", "updated_by", "canonical_norm", "path_ids")}
    await audit(db, actor, "update", entity_id, {k: current.get(k) for k in audited}, audited)
    return await get_entity(db, entity_id)


async def add_alias(db, entity_id: str, payload: AliasCreate, actor: str) -> dict:
    entity = await _load(db, entity_id)
    norm = await _check_alias(db, entity["kind"], payload.alias, payload.ambiguous, entity_id)
    doc = _alias_doc(entity["kind"], entity_id, payload.alias, norm, payload.ambiguous, actor)
    try:
        await db[ndb.ENTITY_ALIASES].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict(f"'{payload.alias}' was taken by another entity at the same time")
    await audit(db, actor, "add_alias", entity_id, None, {"alias": doc["alias"], "ambiguous": doc["ambiguous"]})
    return await get_entity(db, entity_id)


async def delete_alias(db, entity_id: str, alias_id: str, actor: str) -> dict:
    await _load(db, entity_id)
    alias = await db[ndb.ENTITY_ALIASES].find_one({"_id": alias_id, "entity_id": entity_id})
    if alias is None:
        raise NotFound(f"Alias {alias_id} not found on entity {entity_id}")
    await db[ndb.ENTITY_ALIASES].delete_one({"_id": alias_id})
    await audit(db, actor, "delete_alias", entity_id, {"alias": alias["alias"], "ambiguous": alias.get("ambiguous", False)}, None)
    return await get_entity(db, entity_id)


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
            raise Conflict(f"'{string}' is an alias of entity {alias['entity_id']}; remove the alias first")
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
