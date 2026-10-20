"""CSV export and add-only import of entities (needs TEST_MONGO_URL)."""
import csv
import io

import pytest

from src.newsroom import entity_csv
from src.newsroom.seed import SEED_DIR, apply_plan, build_plan, seed_entity_id

ORG, LOC = "organisation", "location"
HEADER = "name,type,on_off,aliases,ambiguous_aliases,parent,country,notes\n"


@pytest.fixture
def seeded(api, sync_db):
    apply_plan(build_plan(), sync_db)
    return api


def _export(api, kind):
    resp = api.get("/newsroom/entities/export", params={"kind": kind})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def _import(api, text, kind=ORG, dry_run=True):
    return api.post("/newsroom/entities/import", json={"kind": kind, "csv_text": text, "dry_run": dry_run})


def _outcomes(result):
    return {r["name"]: (r["outcome"], r["message"]) for r in result["rows"]}


# --- Export ------------------------------------------------------------------

def test_export_layout(seeded):
    body = _export(seeded, ORG)
    assert body["filename"].startswith("newsroom-companies-people-") and body["filename"].endswith(".csv")
    assert body["csv"].startswith("﻿")
    rows = {r["name"]: r for r in _rows(body["csv"])}
    assert len(rows) == 182
    damac = rows["DAMAC Properties"]
    assert (damac["type"], damac["on_off"], damac["country"]) == ("developer", "on", "United Arab Emirates")
    assert damac["aliases"] == "DAMAC" and damac["ambiguous_aliases"] == "DAMAC Group"
    assert rows["Meraas"]["parent"] == "Dubai Holding"

    places = _rows(_export(seeded, LOC)["csv"])
    assert "country" not in places[0] and len(places) == 71


def test_export_makes_formula_cells_safe(seeded):
    resp = seeded.post("/newsroom/entities", json={
        "kind": ORG, "type": "developer", "canonical_name": "=HYPERLINK(1) Co", "aliases": ["-Minus Developments"],
        "notes": "+ check this"})
    assert resp.status_code == 201, resp.text
    row = next(r for r in _rows(_export(seeded, ORG)["csv"]) if "HYPERLINK" in r["name"])
    assert (row["name"], row["aliases"], row["notes"]) == ("'=HYPERLINK(1) Co", "'-Minus Developments", "'+ check this")


def test_merged_entries_are_not_exported(seeded):
    seeded.post(f"/newsroom/entities/{seed_entity_id(ORG, 'Deyaar')}/merge", json={"into_id": seed_entity_id(ORG, "Arada")})
    names = [r["name"] for r in _rows(_export(seeded, ORG)["csv"])]
    assert "Deyaar" not in names and "Arada" in names


# --- Import --------------------------------------------------------------------

def test_reimporting_an_export_changes_nothing(seeded, sync_db):
    before = sync_db["entities"].count_documents({})
    for kind in (ORG, LOC):
        result = _import(seeded, _export(seeded, kind)["csv"], kind, dry_run=False).json()
        assert (result["added"], result["problems"]) == (0, 0)
        assert result["skipped"] == len(result["rows"])
    assert sync_db["entities"].count_documents({}) == before


@pytest.mark.parametrize("kind, filename", [(ORG, "final_entities.csv"), (LOC, "final_locations.csv")])
def test_starting_list_files_are_accepted(seeded, kind, filename):
    text = (SEED_DIR / filename).read_text(encoding="utf-8")
    result = _import(seeded, text, kind).json()
    assert (result["to_add"], result["problems"]) == (0, 0)
    assert all(r["message"] == "Already in the list (left unchanged)" for r in result["rows"])


def test_import_adds_new_rows_only_after_the_check(seeded, sync_db):
    text = HEADER + (
        "Nshama Town Square,project,off,,,Nshama,,\n"                     # child before its parent row
        "Nshama,developer,on,Nshama Developments,Nshama Group,,UAE,From the CSV\n"
        "Emaar Properties,developer,off,,,,,\n"                           # exists: left unchanged
    )
    preview = _import(seeded, text).json()
    assert (preview["to_add"], preview["skipped"], preview["problems"], preview["added"]) == (2, 1, 0, 0)
    assert sync_db["entities"].count_documents({"canonical_name": "Nshama"}) == 0  # the check wrote nothing

    result = _import(seeded, text, dry_run=False).json()
    assert result["added"] == 2
    assert _outcomes(result)["Emaar Properties"] == ("skip", "Already in the list (left unchanged)")
    nshama = sync_db["entities"].find_one({"canonical_name": "Nshama"})
    assert (nshama["status"], nshama["country_id"], nshama["notes"]) == (
        "active", seed_entity_id(LOC, "United Arab Emirates"), "From the CSV")
    aliases = {(a["alias"], a["ambiguous"]) for a in sync_db["entity_aliases"].find({"entity_id": nshama["_id"]})}
    assert aliases == {("Nshama", False), ("Nshama Developments", False), ("Nshama Group", True)}
    town_square = sync_db["entities"].find_one({"canonical_name": "Nshama Town Square"})
    assert town_square["parent_id"] == nshama["_id"] and town_square["status"] == "staged"
    emaar = sync_db["entities"].find_one({"_id": seed_entity_id(ORG, "Emaar Properties")})
    assert emaar["status"] == "active"  # the "off" in the file changed nothing
    audit = sync_db["entity_audit"].find_one({"action": "csv_import"})
    assert (audit["actor"], audit["after"]["added"]) == ("tester@propt.global", 2)


def test_rows_with_problems_are_reported_and_left_out(seeded, sync_db):
    text = HEADER + "\n".join([
        "Good Co,developer,off,,,,,",
        "Good Co,developer,off,,,,,",                          # same name again
        "Taken Alias Co,developer,off,Emaar,,,,",              # Emaar's name
        "Row A,developer,off,Shared Name,,,,",
        "Row B,developer,off,Shared Name,,,,",                 # clashes with Row A
        "Bad Switch Co,developer,maybe,,,,,",
        "Unknown On Co,unknown,on,,,,,",
        "Nowhere Co,developer,off,,,,Atlantis,",
        "Orphan Co,developer,off,,,No Such Parent,,",
        "Child Of Bad,developer,off,,,Bad Switch Co,,",
        "Loop One,developer,off,,,Loop Two,,",
        "Loop Two,developer,off,,,Loop One,,",
        "Handle Co,developer,off,www.example.com,,,,",
    ]) + "\n"
    result = _import(seeded, text, dry_run=False).json()
    outcomes = _outcomes(result)
    good = [(r["outcome"], r["message"]) for r in result["rows"] if r["name"] == "Good Co"]
    assert good == [("added", ""), ("skip", "Same name as row 2")]
    assert "already an exact alias of" in outcomes["Taken Alias Co"][1]
    assert outcomes["Row A"][0] == "added" and "another row" in outcomes["Row B"][1]
    assert "On/off" in outcomes["Bad Switch Co"][1]
    assert "type" in outcomes["Unknown On Co"][1]
    assert "Atlantis" in outcomes["Nowhere Co"][1]
    assert "No Such Parent" in outcomes["Orphan Co"][1]
    assert "cannot be added" in outcomes["Child Of Bad"][1]
    assert "loop" in outcomes["Loop One"][1] and "loop" in outcomes["Loop Two"][1]
    assert "website" in outcomes["Handle Co"][1]
    assert (result["added"], result["problems"]) == (2, 10)
    added = {d["canonical_name"] for d in sync_db["entities"].find({"created_by": "tester@propt.global"})}
    assert added == {"Good Co", "Row A"}


def test_import_checks_names_against_the_list_read_once(seeded, run_counted):
    """Ignored and ambiguous names are caught as on the screen; 300 rows need no lookup per name."""
    text = HEADER + "\n".join([
        "Ignored Name Co,developer,off,waslgroup,,,,",       # on the ignore list
        "Wasl Exact Co,developer,off,Wasl,,,,",              # marked ambiguous on Wasl, so not exact here
        "Wasl Too Co,developer,off,,Wasl,,,",                # ambiguous names may repeat
    ] + [f"Bulk Co {n},developer,off,Bulk Alias {n},,,," for n in range(300)]) + "\n"
    result, queries = run_counted(lambda db, sources: entity_csv.import_csv(db, ORG, text, "t", dry_run=True))
    outcomes = _outcomes(result)
    assert "ignore list" in outcomes["Ignored Name Co"][1]
    assert "marked ambiguous" in outcomes["Wasl Exact Co"][1]
    assert outcomes["Wasl Too Co"][0] == "add"
    assert (result["to_add"], result["problems"]) == (301, 2)
    assert queries <= 15, queries  # was up to 4 lookups per name (about 1,800 here)


def test_apostrophe_from_an_export_is_removed(seeded, sync_db):
    text = HEADER + "'-Minus Developments,developer,off,'=Odd Name | Plain Name,,,,'+ note\n"
    assert _import(seeded, text, dry_run=False).json()["added"] == 1
    doc = sync_db["entities"].find_one({"canonical_name": "-Minus Developments"})
    assert doc["notes"] == "+ note"
    assert {a["alias"] for a in sync_db["entity_aliases"].find({"entity_id": doc["_id"]})} == {
        "-Minus Developments", "=Odd Name", "Plain Name"}


@pytest.mark.parametrize("text, message", [
    ("", "empty"),
    ("company,kind\nX,developer\n", "column names"),
    (HEADER, "no rows"),
    (HEADER + "".join(f"X{n},developer,off,,,,,\n" for n in range(1001)), "at most 1000"),
])
def test_unreadable_files_are_refused(seeded, text, message):
    resp = _import(seeded, text)
    assert resp.status_code == 422 and message in resp.json()["detail"]


# --- Template and types -----------------------------------------------------------

@pytest.mark.parametrize("kind", [ORG, LOC])
def test_template_can_be_imported_as_is(seeded, kind):
    body = seeded.get("/newsroom/entities/template", params={"kind": kind}).json()
    assert body["filename"].endswith("-template.csv")
    assert body["csv"].lstrip("﻿").splitlines()[0] == ",".join(body["columns"])
    assert "developer" in body["types"] if kind == ORG else "community" in body["types"]
    result = _import(seeded, body["csv"], kind, dry_run=False).json()
    assert (result["added"], result["problems"]) == (0, 0)
    assert all(r["message"] == "Example row (never imported)" for r in result["rows"])


def test_type_must_be_a_known_one(seeded, sync_db):
    text = HEADER + "Typo Co,develper,off,,,,,\nCase Co,Developer,off,,,,,\nNo Type Co,,off,,,,,\n"
    outcomes = _outcomes(_import(seeded, text, dry_run=False).json())
    assert outcomes["Typo Co"][0] == "error" and "Type 'develper' is not one of: developer" in outcomes["Typo Co"][1]
    assert outcomes["Case Co"][0] == "added"
    assert sync_db["entities"].find_one({"canonical_name": "Case Co"})["type"] == "developer"
    assert outcomes["No Type Co"] == ("error", "No type")
