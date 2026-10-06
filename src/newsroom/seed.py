"""
Starting lists for the Newsroom knowledge graph.

build_plan() reads the three CSVs and checks them without touching a database.
apply_plan() inserts what is missing and never changes existing records, so a
re-run is safe and reviewer edits are never overwritten.
"""
import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from src.newsroom import db as ndb
from src.newsroom.normalize import looks_like_handle, normalize_alias, raw_key

SEED_DIR = Path(__file__).resolve().parent / "seed"
ENTITIES_CSV = "final_entities.csv"
LOCATIONS_CSV = "final_locations.csv"
IGNORED_CSV = "ignored_strings.csv"

# Stable ids, so the same seed gives the same ids in every environment.
SEED_NAMESPACE = uuid.UUID("6f1c0b52-6d2e-4b8e-9a51-3f2a7c9e4d10")
SEED_ACTOR = "seed"
VALID_STATUSES = {"active", "staged", "ignored"}


def seed_entity_id(kind: str, canonical_name: str) -> str:
    return str(uuid.uuid5(SEED_NAMESPACE, f"{kind}:{normalize_alias(canonical_name)}"))


def _split(value: str) -> List[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def _int(value: str) -> int:
    try:
        return int((value or "0").strip() or 0)
    except ValueError:
        return 0


@dataclass
class SeedPlan:
    entities: List[dict] = field(default_factory=list)
    aliases: List[dict] = field(default_factory=list)
    ignore: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        def n(kind, status=None):
            return sum(1 for e in self.entities if e["kind"] == kind and (status is None or e["status"] == status))
        return {
            "organisations": n("organisation"),
            "organisations_active": n("organisation", "active"),
            "organisations_staged": n("organisation", "staged"),
            "locations": n("location"),
            "locations_active": n("location", "active"),
            "locations_staged": n("location", "staged"),
            "aliases_exact": sum(1 for a in self.aliases if not a["ambiguous"]),
            "aliases_ambiguous": sum(1 for a in self.aliases if a["ambiguous"]),
            "ignored_strings": len(self.ignore),
        }


def _read_csv(path: Path) -> List[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _column(row: dict, prefix: str) -> str:
    """CSV headers carry explanations, e.g. 'aliases (exact, auto-link)'; match on the start."""
    for key, value in row.items():
        if key and key.strip().startswith(prefix):
            return value or ""
    return ""


def build_plan(seed_dir: Path = SEED_DIR, now: Optional[datetime] = None) -> SeedPlan:
    now = now or datetime.now(timezone.utc)
    plan = SeedPlan()

    location_rows = _read_csv(seed_dir / LOCATIONS_CSV)
    org_rows = _read_csv(seed_dir / ENTITIES_CSV)
    ignore_rows = _read_csv(seed_dir / IGNORED_CSV)

    rows = [("location", r) for r in location_rows] + [("organisation", r) for r in org_rows]
    by_key: Dict[tuple, dict] = {}

    for kind, row in rows:
        name = (row.get("canonical_name") or "").strip()
        etype = (row.get("type") or "").strip()
        status = (row.get("status") or "").strip().lower()
        label = f"{kind} '{name}'"
        if not name:
            plan.errors.append(f"{kind} row without a canonical_name")
            continue
        if status not in VALID_STATUSES:
            plan.errors.append(f"{label}: invalid status '{status}'")
        if not etype:
            plan.errors.append(f"{label}: missing type")
        if status == "active" and etype == "unknown":
            plan.errors.append(f"{label}: active with type 'unknown' (choose a type first)")
        key = (kind, normalize_alias(name))
        if key in by_key:
            plan.errors.append(f"{label}: duplicate canonical name in the {kind} list")
            continue

        exact = _split(_column(row, "aliases"))
        ambiguous = _split(_column(row, "ambiguous_aliases")) if kind == "organisation" else []
        ambiguous_norms = {normalize_alias(a) for a in ambiguous}
        # The canonical name is an exact alias unless the same text is marked ambiguous.
        if normalize_alias(name) not in ambiguous_norms:
            exact = [name] + exact

        entity = {
            "_id": seed_entity_id(kind, name),
            "kind": kind,
            "type": etype,
            "status": status,
            "canonical_name": name,
            "canonical_norm": key[1],
            "parent_name": (row.get("parent") or "").strip() or None,
            "country_name": ((row.get("country") or "").strip() or None) if kind == "organisation" else None,
            "context_slug": (row.get("context_slug") or "").strip() or None,
            "mentions_in_data": _int(row.get("mentions_in_data")),
            "notes": (row.get("note") or "").strip() or None,
            "_exact": exact,
            "_ambiguous": ambiguous,
        }
        by_key[key] = entity

    # Parents (same kind only), cycles and ancestor paths.
    for entity in by_key.values():
        parent_name = entity.pop("parent_name")
        entity["parent_id"] = None
        if parent_name:
            parent = by_key.get((entity["kind"], normalize_alias(parent_name)))
            if parent is None:
                plan.errors.append(f"{entity['kind']} '{entity['canonical_name']}': parent '{parent_name}' is not in the list")
            else:
                entity["parent_id"] = parent["_id"]
    by_id = {e["_id"]: e for e in by_key.values()}
    for entity in by_key.values():
        path, current, seen = [], entity, set()
        while current is not None:
            if current["_id"] in seen:
                plan.errors.append(f"{entity['kind']} '{entity['canonical_name']}': parent chain loops")
                break
            seen.add(current["_id"])
            path.insert(0, current["_id"])
            current = by_id.get(current["parent_id"]) if current["parent_id"] else None
        entity["path_ids"] = path

    # Organisation country -> location entity (by name or exact location alias).
    location_alias_owner = {}
    for e in by_key.values():
        if e["kind"] == "location":
            for a in e["_exact"]:
                location_alias_owner.setdefault(normalize_alias(a), e["_id"])
    for entity in by_key.values():
        country_name = entity.pop("country_name")
        entity["country_id"] = None
        if country_name:
            location_id = location_alias_owner.get(normalize_alias(country_name))
            if location_id is None:
                plan.warnings.append(f"organisation '{entity['canonical_name']}': country '{country_name}' not found in locations; left empty")
            else:
                entity["country_id"] = location_id

    # Aliases: one exact owner per kind; exact and ambiguous never share a name; no website/handle aliases.
    exact_owner: Dict[tuple, str] = {}
    ambiguous_keys = set()
    for entity in by_key.values():
        seen_norms = set()
        for alias, is_ambiguous in [(a, False) for a in entity.pop("_exact")] + [(a, True) for a in entity.pop("_ambiguous")]:
            norm = normalize_alias(alias)
            label = f"{entity['kind']} '{entity['canonical_name']}' alias '{alias}'"
            if not norm or norm in seen_norms:
                continue
            seen_norms.add(norm)
            if looks_like_handle(alias):
                plan.errors.append(f"{label}: looks like a website or social handle")
            key = (entity["kind"], norm)
            if is_ambiguous:
                ambiguous_keys.add(key)
            else:
                owner = exact_owner.setdefault(key, entity["canonical_name"])
                if owner != entity["canonical_name"]:
                    plan.errors.append(f"{label}: already an exact alias of '{owner}'")
            plan.aliases.append({
                "entity_id": entity["_id"],
                "kind": entity["kind"],
                "alias": alias,
                "alias_norm": norm,
                "ambiguous": is_ambiguous,
                "lang": "ar" if any("؀" <= ch <= "ۿ" for ch in alias) else "en",
                "source": SEED_ACTOR,
                "created_at": now,
                "created_by": SEED_ACTOR,
            })
    for key in sorted(ambiguous_keys & set(exact_owner)):
        plan.errors.append(f"{key[0]} alias '{key[1]}' is exact for '{exact_owner[key]}' and ambiguous elsewhere")

    # Ignore list, compared on the original text (case-insensitive), never on normalised text.
    alias_raw = {raw_key(a["alias"]): a for a in plan.aliases}
    seen_ignore = set()
    for row in ignore_rows:
        string = (row.get("string") or "").strip()
        key = raw_key(string)
        if not key or key in seen_ignore:
            continue
        seen_ignore.add(key)
        if key in alias_raw:
            plan.errors.append(f"ignored string '{string}' is also an alias of an entity")
        plan.ignore.append({
            "_id": str(uuid.uuid5(SEED_NAMESPACE, f"ignore:{key}")),
            "string": string,
            "string_key": key,
            "reason": (row.get("reason") or "").strip() or None,
            "mentions_in_data": _int(row.get("mentions_in_data")),
            "created_at": now,
            "created_by": SEED_ACTOR,
        })

    for entity in by_key.values():
        entity.update({
            "curated": True,
            "seed": True,
            "created_at": now,
            "created_by": SEED_ACTOR,
            "updated_at": now,
            "updated_by": SEED_ACTOR,
        })
        plan.entities.append(entity)
    return plan


def compare_with_db(plan: SeedPlan, db) -> Dict[str, int]:
    """Read-only: how much of the plan already exists in the database."""
    existing_entities = {d["_id"] for d in db[ndb.ENTITIES].find({}, {"_id": 1})}
    existing_names = {(d["kind"], d["canonical_norm"]): d["_id"] for d in db[ndb.ENTITIES].find({}, {"kind": 1, "canonical_norm": 1})}
    exact_in_db = {(d["kind"], d["alias_norm"]): d["entity_id"] for d in db[ndb.ENTITY_ALIASES].find({"ambiguous": False}, {"kind": 1, "alias_norm": 1, "entity_id": 1})}
    alias_pairs = {(d["entity_id"], d["alias_norm"]) for d in db[ndb.ENTITY_ALIASES].find({}, {"entity_id": 1, "alias_norm": 1})}
    ignore_keys = {d["string_key"] for d in db[ndb.ENTITY_IGNORE].find({}, {"string_key": 1})}

    result = {"entities_new": 0, "entities_existing": 0, "entities_name_taken": 0,
              "aliases_new": 0, "aliases_existing": 0, "aliases_conflicting": 0,
              "ignore_new": 0, "ignore_existing": 0}
    for e in plan.entities:
        if e["_id"] in existing_entities:
            result["entities_existing"] += 1
        elif (e["kind"], e["canonical_norm"]) in existing_names:
            result["entities_name_taken"] += 1
        else:
            result["entities_new"] += 1
    for a in plan.aliases:
        if (a["entity_id"], a["alias_norm"]) in alias_pairs:
            result["aliases_existing"] += 1
        elif not a["ambiguous"] and exact_in_db.get((a["kind"], a["alias_norm"]), a["entity_id"]) != a["entity_id"]:
            result["aliases_conflicting"] += 1
        else:
            result["aliases_new"] += 1
    for i in plan.ignore:
        result["ignore_existing" if i["string_key"] in ignore_keys else "ignore_new"] += 1
    return result


def apply_plan(plan: SeedPlan, db) -> Dict[str, int]:
    """Inserts what is missing; existing records are never modified. Refuses a plan with errors."""
    if plan.errors:
        raise ValueError(f"Seed plan has {len(plan.errors)} error(s); fix the lists first.")
    index_errors = ndb.ensure_indexes_sync(db)
    if index_errors:
        raise RuntimeError("Index creation failed: " + "; ".join(index_errors))

    result = {"entities_inserted": 0, "entities_skipped": 0, "aliases_inserted": 0,
              "aliases_skipped": 0, "aliases_conflicting": 0, "ignore_inserted": 0, "ignore_skipped": 0}
    inserted_ids = set()
    for e in plan.entities:
        try:
            res = db[ndb.ENTITIES].update_one({"_id": e["_id"]}, {"$setOnInsert": e}, upsert=True)
        except DuplicateKeyError:
            # Same canonical name already exists under another id (added by a reviewer).
            result["entities_skipped"] += 1
            continue
        if res.upserted_id is not None:
            inserted_ids.add(e["_id"])
            result["entities_inserted"] += 1
        else:
            result["entities_skipped"] += 1
    for a in plan.aliases:
        try:
            res = db[ndb.ENTITY_ALIASES].update_one(
                {"entity_id": a["entity_id"], "alias_norm": a["alias_norm"]},
                {"$setOnInsert": {"_id": str(uuid.uuid4()), **a}},
                upsert=True,
            )
        except DuplicateKeyError:
            result["aliases_conflicting"] += 1
            continue
        result["aliases_inserted" if res.upserted_id is not None else "aliases_skipped"] += 1
    for i in plan.ignore:
        try:
            res = db[ndb.ENTITY_IGNORE].update_one({"string_key": i["string_key"]}, {"$setOnInsert": i}, upsert=True)
        except DuplicateKeyError:
            result["ignore_skipped"] += 1
            continue
        result["ignore_inserted" if res.upserted_id is not None else "ignore_skipped"] += 1

    db[ndb.ENTITY_AUDIT].insert_one({
        "_id": str(uuid.uuid4()),
        "at": datetime.now(timezone.utc),
        "actor": SEED_ACTOR,
        "action": "seed_import",
        "entity_id": None,
        "before": None,
        "after": result,
    })
    return result
