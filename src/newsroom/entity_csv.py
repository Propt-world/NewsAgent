"""
CSV export and add-only import for the entity manager.

Export writes one kind (companies/people or places) in the same layout the
import reads. Cells that a spreadsheet could run as a formula (starting with
=, +, -, @, tab or carriage return) get a leading apostrophe; import removes it.

A template (column names plus example rows) is offered for people who have
not seen the layout; rows whose name starts with "Example:" are never imported.

Import only ADDS entities whose name is not in the list yet. Rows for names
that already exist are skipped and nothing about the existing entity changes.
Every row is checked with the same rules as the screen (create_entity); rows
with a problem are reported and left out, the rest are added. The starting
list files (final_entities.csv / final_locations.csv) are accepted as well.
"""
import csv
import io
from typing import Dict, List, Optional

from pydantic import ValidationError

from src.newsroom import db as ndb
from src.newsroom import entities as ent
from src.newsroom.models import EntityCreate
from src.newsroom.normalize import normalize_alias

MAX_ROWS = 1000
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
STATUS_WORDS = {"on": "active", "active": "active", "off": "staged", "staged": "staged", "": "staged",
                "ignored": "ignored"}
STATUS_OUT = {"active": "on", "staged": "off", "ignored": "ignored"}

# Column -> accepted header beginnings (lower case). Other columns (e.g. mentions_in_data) are ignored.
HEADERS = {
    "name": ("name", "canonical_name"),
    "type": ("type",),
    "on_off": ("on_off", "on/off", "status"),
    "aliases": ("aliases",),
    "ambiguous_aliases": ("ambiguous_alias", "ambiguous alias"),
    "parent": ("parent",),
    "country": ("country",),
    "notes": ("notes", "note"),
}
ALIAS_COLUMNS = ("aliases", "ambiguous_aliases")
EXAMPLE_PREFIX = "example:"

# The types the entity manager offers (same list as the backoffice's entityOptions.ts).
STANDARD_TYPES = {
    "organisation": ["developer", "investment", "government", "finance", "brokerage", "proptech", "contractor",
                     "architecture_firm", "brand_partner", "project", "executive", "News/Media", "unknown"],
    "location": ["country", "city", "region", "community", "landmark"],
}

TEMPLATE_ROWS = {
    "organisation": [
        {"name": "Example: Nshama", "type": "developer", "on_off": "on",
         "aliases": "Nshama Developments | Nshama LLC", "ambiguous_aliases": "Nshama Group", "parent": "",
         "country": "United Arab Emirates", "notes": "Example row, never imported. One company per row."},
        {"name": "Example: Emaar Hospitality", "type": "investment", "on_off": "off", "aliases": "",
         "ambiguous_aliases": "", "parent": "Emaar Properties", "country": "UAE",
         "notes": "Example row, never imported. Parent must already be in the list or in this file."},
    ],
    "location": [
        {"name": "Example: Dubai Hills Estate", "type": "community", "on_off": "on", "aliases": "Dubai Hills | DHE",
         "ambiguous_aliases": "", "parent": "Dubai", "notes": "Example row, never imported. One place per row."},
    ],
}


def columns(kind: str) -> List[str]:
    cols = ["name", "type", "on_off", "aliases", "ambiguous_aliases", "parent", "country", "notes"]
    return cols if kind == "organisation" else [c for c in cols if c != "country"]


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _unsafe_cell(text: str) -> str:
    text = (text or "").strip()
    return text[1:] if len(text) > 1 and text[0] == "'" and text[1] in FORMULA_START else text


def _split(value: str) -> List[str]:
    return [_unsafe_cell(v) for v in (value or "").split("|") if v.strip()]


# --- Export ----------------------------------------------------------------

async def export_csv(db, kind: str) -> str:
    docs = await db[ndb.ENTITIES].find({"kind": kind, "status": {"$ne": "merged"}}).sort("canonical_norm", 1).to_list(length=None)
    ids = [d["_id"] for d in docs]
    ref_ids = [i for d in docs for i in (d.get("parent_id"), d.get("country_id")) if i]
    names = {d["_id"]: d["canonical_name"] async for d in db[ndb.ENTITIES].find({"_id": {"$in": ref_ids}}, {"canonical_name": 1})}
    aliases: Dict[str, List[dict]] = {i: [] for i in ids}
    async for a in db[ndb.ENTITY_ALIASES].find({"entity_id": {"$in": ids}}).sort("alias", 1):
        aliases[a["entity_id"]].append(a)

    def joined(items) -> str:
        # Each alias is made safe on its own: the import splits on "|" before removing the apostrophe.
        return " | ".join(safe_cell(a["alias"]) for a in items)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    cols = columns(kind)
    writer.writerow(cols)
    for d in docs:
        own = aliases.get(d["_id"], [])
        row = {
            "name": safe_cell(d["canonical_name"]),
            "type": safe_cell(d.get("type")),
            "on_off": STATUS_OUT.get(d.get("status"), d.get("status")),
            # The name itself is always an exact alias (unless listed as ambiguous), so it is not repeated.
            "aliases": joined(a for a in own if not a.get("ambiguous") and a["alias_norm"] != d["canonical_norm"]),
            "ambiguous_aliases": joined(a for a in own if a.get("ambiguous")),
            "parent": safe_cell(names.get(d.get("parent_id"), "")),
            "country": safe_cell(names.get(d.get("country_id"), "")),
            "notes": safe_cell(d.get("notes") or ""),
        }
        writer.writerow([row[c] for c in cols])
    # The byte-order mark lets Excel open Arabic names correctly.
    return "﻿" + out.getvalue()


async def allowed_types(db, kind: str) -> List[str]:
    """The standard types plus any other type already used in the list (so existing data stays importable)."""
    in_use = await db[ndb.ENTITIES].distinct("type", {"kind": kind})
    return STANDARD_TYPES[kind] + sorted(t for t in in_use if t and t not in STANDARD_TYPES[kind])


def template_csv(kind: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    cols = columns(kind)
    writer.writerow(cols)
    for row in TEMPLATE_ROWS[kind]:
        writer.writerow([row.get(c, "") for c in cols])
    return "\ufeff" + out.getvalue()


# --- Import ----------------------------------------------------------------

class _Row:
    def __init__(self, number: int, values: Dict[str, str]):
        self.number = number
        self.values = values
        self.name = values.get("name", "")
        self.norm = normalize_alias(self.name)
        self.outcome = "add"
        self.message = ""
        self.payload: Optional[dict] = None
        self.parent_norm: Optional[str] = None

    def fail(self, message: str) -> None:
        if self.outcome == "add":
            self.outcome, self.message = "error", message

    def out(self) -> dict:
        return {"row": self.number, "name": self.name, "outcome": self.outcome, "message": self.message}


def _header_map(fieldnames: List[str], kind: str) -> Dict[str, str]:
    found = {}
    for original in fieldnames:
        header = (original or "").strip().lstrip("﻿").lower()
        for column, starts in HEADERS.items():
            if column == "country" and kind != "organisation":
                continue
            if column not in found and header.startswith(starts):
                found[column] = original
                break
    return found


def _parse(text: str, kind: str) -> List[_Row]:
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise ent.Invalid("The file is empty")
    try:
        reader = csv.DictReader(io.StringIO(text))
        mapping = _header_map(reader.fieldnames or [], kind)
        if "name" not in mapping or "type" not in mapping:
            raise ent.Invalid("The first row must hold the column names, including name and type "
                              "(export the list to see the layout)")
        rows = []
        for number, record in enumerate(reader, start=2):
            # Alias cells stay raw here: they are split on "|" first, then each part is cleaned.
            values = {col: (record.get(header) or "") if col in ALIAS_COLUMNS else _unsafe_cell(record.get(header) or "")
                      for col, header in mapping.items()}
            if not any(v.strip() for v in values.values()):
                continue  # blank line
            rows.append(_Row(number, values))
    except csv.Error as e:
        raise ent.Invalid(f"The file could not be read as CSV: {e}")
    if len(rows) > MAX_ROWS:
        raise ent.Invalid(f"The file has {len(rows)} rows; import at most {MAX_ROWS} at a time")
    if not rows:
        raise ent.Invalid("The file has no rows under the column names")
    return rows


async def _check_row(db, row: _Row, kind: str, countries: Dict[str, str], types: Dict[str, str],
                     in_file_exact: Dict[str, int], in_file_ambiguous: Dict[str, int],
                     aliases: ent.AliasSnapshot) -> None:
    """Same checks as adding on the screen, plus clashes with other rows of the file."""
    v = row.values
    given_type = v.get("type", "").strip()
    etype = types.get(given_type.lower())
    if etype is None:
        return row.fail(f"Type '{given_type}' is not one of: {', '.join(types.values())}" if given_type
                        else "No type")
    status = STATUS_WORDS.get(v.get("on_off", "").strip().lower())
    if status is None:
        return row.fail(f"On/off must be on, off or ignored, not '{v.get('on_off')}'")
    try:
        payload = EntityCreate(
            kind=kind,
            type=etype,
            canonical_name=row.name,
            status=status,
            notes=v.get("notes") or None,
            aliases=_split(v.get("aliases", "")),
            ambiguous_aliases=_split(v.get("ambiguous_aliases", "")),
        )
    except ValidationError as e:
        first = e.errors()[0]
        return row.fail(f"{first['loc'][-1]}: {first['msg']}")
    if status == "active" and payload.type == "unknown":
        return row.fail("Choose a type before switching it on")

    ambiguous_norms = {normalize_alias(a) for a in payload.ambiguous_aliases}
    exact = ([row.name] if row.norm not in ambiguous_norms else []) + payload.aliases
    try:
        for alias in exact:
            norm = normalize_alias(alias)
            if norm in ambiguous_norms:
                raise ent.Invalid(f"'{alias}' cannot be both an exact and an ambiguous alias")
            if in_file_exact.get(norm, row.number) != row.number or norm in in_file_ambiguous:
                raise ent.Conflict(f"'{alias}' is also used on another row")
            await ent._check_alias(db, kind, alias, False, None, aliases)
            in_file_exact[norm] = row.number
        for alias in payload.ambiguous_aliases:
            norm = normalize_alias(alias)
            if norm in in_file_exact:
                raise ent.Conflict(f"'{alias}' is an exact name on another row")
            await ent._check_alias(db, kind, alias, True, None, aliases)
            in_file_ambiguous[norm] = row.number
    except (ent.Invalid, ent.Conflict) as e:
        return row.fail(str(e))

    country = (v.get("country") or "").strip()
    if country:
        country_id = countries.get(normalize_alias(country))
        if country_id is None:
            return row.fail(f"Country '{country}' is not a country in the place list")
        payload = payload.model_copy(update={"country_id": country_id})
    row.payload = payload.model_dump()
    parent = (v.get("parent") or "").strip()
    row.parent_norm = normalize_alias(parent) if parent else None


def _creation_order(rows: List[_Row], parents: Dict[str, str]) -> List[_Row]:
    """Rows to add, parents before children. Rows whose parent is missing, has a problem or loops get an error."""
    adding = {r.norm: r for r in rows if r.outcome == "add"}
    problems = {r.norm: r for r in rows if r.outcome == "error" and r.norm}
    for r in list(adding.values()):
        if r.parent_norm and r.parent_norm not in parents and r.parent_norm not in adding:
            bad = problems.get(r.parent_norm)
            r.fail(f"Parent '{r.values.get('parent')}' (row {bad.number}) cannot be added" if bad
                   else f"Parent '{r.values.get('parent')}' is not in the list or in this file")
    known, order = set(parents), []
    pending = [r for r in rows if r.outcome == "add"]
    while pending:
        ready = [r for r in pending if not r.parent_norm or r.parent_norm in known]
        if not ready:
            # Decide every message first, so one row's failure does not change the next row's reason.
            messages = []
            for r in pending:
                parent_row = adding.get(r.parent_norm)
                messages.append(f"Parent '{r.values.get('parent')}' (row {parent_row.number}) cannot be added"
                                if parent_row and parent_row.outcome == "error"
                                else "Its parent rows point at each other in a loop")
            for r, message in zip(pending, messages):
                r.fail(message)
            break
        for r in ready:
            pending.remove(r)
            order.append(r)
            known.add(r.norm)
    return order


async def import_csv(db, kind: str, text: str, actor: str, dry_run: bool = True) -> dict:
    rows = _parse(text, kind)

    existing = {d["canonical_norm"]: d.get("status") async for d in db[ndb.ENTITIES].find(
        {"kind": kind}, {"canonical_norm": 1, "status": 1})}
    parents: Dict[str, str] = {}
    async for d in db[ndb.ENTITIES].find({"kind": kind, "status": {"$ne": "merged"}}, {"canonical_norm": 1}):
        parents[d["canonical_norm"]] = d["_id"]
    countries: Dict[str, str] = {}
    if kind == "organisation":
        country_ids = await db[ndb.ENTITIES].distinct("_id", {"kind": "location", "type": "country", "status": {"$ne": "merged"}})
        async for d in db[ndb.ENTITIES].find({"_id": {"$in": country_ids}}, {"canonical_norm": 1}):
            countries[d["canonical_norm"]] = d["_id"]
        async for a in db[ndb.ENTITY_ALIASES].find({"entity_id": {"$in": country_ids}, "ambiguous": False}):
            countries.setdefault(a["alias_norm"], a["entity_id"])

    types = {t.lower(): t for t in await allowed_types(db, kind)}
    aliases = await ent.AliasSnapshot.load(db, kind)
    in_file_exact: Dict[str, int] = {}
    in_file_ambiguous: Dict[str, int] = {}
    first_row: Dict[str, int] = {}
    for row in rows:
        if row.name.strip().lower().startswith(EXAMPLE_PREFIX):
            row.outcome, row.message = "skip", "Example row (never imported)"
        elif not row.norm:
            row.fail("No name")
        elif row.norm in existing:
            row.outcome = "skip"
            row.message = ("Merged into another entry (left unchanged)" if existing[row.norm] == "merged"
                           else "Already in the list (left unchanged)")
        elif row.norm in first_row:
            row.outcome, row.message = "skip", f"Same name as row {first_row[row.norm]}"
        else:
            first_row[row.norm] = row.number
            await _check_row(db, row, kind, countries, types, in_file_exact, in_file_ambiguous, aliases)

    order = _creation_order(rows, parents)

    added = 0
    if not dry_run:
        for row in order:
            payload = dict(row.payload)
            if row.parent_norm:
                payload["parent_id"] = parents.get(row.parent_norm)
                if payload["parent_id"] is None:
                    row.fail(f"Parent '{row.values.get('parent')}' could not be added")
                    continue
            try:
                created = await ent.create_entity(db, EntityCreate(**payload), actor)
            except (ent.Invalid, ent.Conflict) as e:
                row.fail(str(e))  # e.g. a name taken by someone else at the same moment
                continue
            parents[row.norm] = created["id"]
            row.outcome, row.message = "added", ""
            added += 1
        await ent.audit(db, actor, "csv_import", None, None, {
            "kind": kind, "rows": len(rows), "added": added,
            "skipped": sum(1 for r in rows if r.outcome == "skip"),
            "problems": sum(1 for r in rows if r.outcome == "error"),
        })

    return {
        "dry_run": dry_run,
        "kind": kind,
        "rows": [r.out() for r in rows],
        "to_add": sum(1 for r in rows if r.outcome in ("add", "added")),
        "added": added,
        "skipped": sum(1 for r in rows if r.outcome == "skip"),
        "problems": sum(1 for r in rows if r.outcome == "error"),
    }
