"""Checks of the seed plan built from CSV files; no database needed."""
import csv

import pytest

from src.newsroom.seed import ENTITIES_CSV, IGNORED_CSV, LOCATIONS_CSV, build_plan, seed_entity_id

ENTITY_HEADER = ["canonical_name", "type", "status", "aliases (exact, auto-link)", "ambiguous_aliases (never auto-link)",
                 "parent", "country", "context_slug", "mentions_in_data", "ambiguous_mentions", "note"]
LOCATION_HEADER = ["canonical_name", "type", "parent", "status", "aliases (exact)", "mentions_in_data", "note"]
IGNORE_HEADER = ["string", "mentions_in_data", "reason"]


def _write(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _seed_dir(tmp_path, entities, locations=None, ignored=None):
    locations = locations if locations is not None else [
        ["United Arab Emirates", "country", "", "active", "UAE", "1", ""],
        ["Dubai", "city", "United Arab Emirates", "active", "", "1", ""],
    ]
    _write(tmp_path / ENTITIES_CSV, ENTITY_HEADER, entities)
    _write(tmp_path / LOCATIONS_CSV, LOCATION_HEADER, locations)
    _write(tmp_path / IGNORED_CSV, IGNORE_HEADER, ignored or [])
    return tmp_path


def _org(name, status="active", etype="developer", aliases="", ambiguous="", parent="", country="UAE"):
    return [name, etype, status, aliases, ambiguous, parent, country, "", "1", "0", ""]


# --- The real starting lists -------------------------------------------------

@pytest.fixture(scope="module")
def real_plan():
    return build_plan()


def _entity(plan, kind, name):
    return next(e for e in plan.entities if e["kind"] == kind and e["canonical_name"] == name)


def _aliases(plan, entity, ambiguous):
    return sorted(a["alias"] for a in plan.aliases if a["entity_id"] == entity["_id"] and a["ambiguous"] is ambiguous)


def test_real_lists_have_no_errors(real_plan):
    assert real_plan.errors == []
    assert real_plan.warnings == []


def test_real_list_counts(real_plan):
    counts = real_plan.counts()
    assert (counts["organisations"], counts["organisations_active"], counts["organisations_staged"]) == (182, 49, 133)
    assert (counts["locations"], counts["locations_active"], counts["locations_staged"]) == (71, 38, 33)
    # 22 rows; three pairs differ only by capital letters (DubaiSouth, WaslGroup, UAE PASS)
    assert counts["ignored_strings"] == 19


def test_canonical_name_is_alias_unless_ambiguous(real_plan):
    marjan = _entity(real_plan, "organisation", "Marjan")
    assert "Marjan" not in _aliases(real_plan, marjan, False)
    assert _aliases(real_plan, marjan, True) == ["Marjan"]
    assert "Marjan LLC" in _aliases(real_plan, marjan, False)
    emaar = _entity(real_plan, "organisation", "Emaar Properties")
    assert _aliases(real_plan, emaar, False) == ["Emaar", "Emaar Properties"]


def test_wasl_fix_applied(real_plan):
    wasl = _entity(real_plan, "organisation", "Wasl Properties")
    assert _aliases(real_plan, wasl, True) == ["Wasl"]
    group = _entity(real_plan, "organisation", "Wasl Group")
    assert _aliases(real_plan, group, False) == ["Wasl Group"]


def test_location_paths_and_org_country(real_plan):
    uae = _entity(real_plan, "location", "United Arab Emirates")
    dubai = _entity(real_plan, "location", "Dubai")
    creek = _entity(real_plan, "location", "Dubai Creek Harbour")
    assert creek["path_ids"] == [uae["_id"], dubai["_id"], creek["_id"]]
    assert _entity(real_plan, "organisation", "DAMAC Properties")["country_id"] == uae["_id"]
    meraas = _entity(real_plan, "organisation", "Meraas")
    holding = _entity(real_plan, "organisation", "Dubai Holding")
    assert meraas["parent_id"] == holding["_id"]
    assert meraas["path_ids"] == [holding["_id"], meraas["_id"]]


def test_ids_are_stable(real_plan):
    again = build_plan()
    assert [e["_id"] for e in again.entities] == [e["_id"] for e in real_plan.entities]
    assert _entity(real_plan, "organisation", "Stake")["_id"] == seed_entity_id("organisation", "Stake")


def test_same_name_allowed_in_both_kinds(real_plan):
    org = _entity(real_plan, "organisation", "Dubai South")
    loc = _entity(real_plan, "location", "Dubai South")
    assert org["_id"] != loc["_id"]


# --- Error detection ------------------------------------------------------------

def test_missing_parent(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Child Co", parent="Nobody")]))
    assert any("parent 'Nobody' is not in the list" in e for e in plan.errors)


def test_parent_loop(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("A Co", parent="B Co"), _org("B Co", parent="A Co")]))
    assert any("loops" in e for e in plan.errors)


def test_exact_alias_owned_twice(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Alpha Properties", aliases="Alpha"), _org("Alpha Group", aliases="Alpha")]))
    assert any("already an exact alias of 'Alpha Properties'" in e for e in plan.errors)


def test_exact_and_ambiguous_collision(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Wasl", status="staged"), _org("Wasl Properties", ambiguous="Wasl")]))
    assert any("exact for 'Wasl' and ambiguous elsewhere" in e for e in plan.errors)


def test_active_unknown_type(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Mystery Co", etype="unknown")]))
    assert any("active with type 'unknown'" in e for e in plan.errors)
    staged = build_plan(_seed_dir(tmp_path, [_org("Mystery Co", etype="unknown", status="staged")]))
    assert staged.errors == []


def test_ignored_string_equal_to_alias(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Dubai South", aliases="dubaisouth")], ignored=[["DubaiSouth", "1", "handle"]]))
    assert any("ignored string 'DubaiSouth' is also an alias" in e for e in plan.errors)


def test_ignore_list_does_not_block_real_company(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("BNW Developments")], ignored=[["bnw.developments", "1", "handle"]]))
    assert plan.errors == []


def test_handle_alias_rejected(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Momah", aliases="@Momah_SA")]))
    assert any("website or social handle" in e for e in plan.errors)


def test_unknown_country_is_a_warning(tmp_path):
    plan = build_plan(_seed_dir(tmp_path, [_org("Far Co", country="Atlantis")]))
    assert plan.errors == []
    assert any("country 'Atlantis' not found" in w for w in plan.warnings)
