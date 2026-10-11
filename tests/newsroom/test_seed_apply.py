"""Applying the seed to a throwaway MongoDB database (needs TEST_MONGO_URL)."""
import pytest
from pymongo.errors import DuplicateKeyError

from src.newsroom import db as ndb
from src.newsroom.seed import apply_plan, build_plan, compare_with_db, seed_entity_id


@pytest.fixture(scope="module")
def plan():
    return build_plan()


def test_dry_run_compare_writes_nothing(sync_db, plan):
    result = compare_with_db(plan, sync_db)
    assert result["entities_new"] == 253
    assert result["entities_existing"] == 0
    assert sync_db.list_collection_names() == []


def test_apply_then_reapply_is_safe(sync_db, plan):
    first = apply_plan(plan, sync_db)
    assert first["entities_inserted"] == 253
    assert first["aliases_inserted"] == len(plan.aliases)
    assert first["ignore_inserted"] == 19
    assert first["aliases_conflicting"] == 0

    # A reviewer switches an entity on and renames it; a re-run must not undo that.
    stake_id = seed_entity_id("organisation", "Stake")
    sync_db[ndb.ENTITIES].update_one({"_id": stake_id}, {"$set": {"status": "staged", "notes": "reviewer note"}})

    second = apply_plan(plan, sync_db)
    assert second["entities_inserted"] == 0
    assert second["entities_skipped"] == 253
    assert second["aliases_inserted"] == 0
    assert second["ignore_inserted"] == 0
    stake = sync_db[ndb.ENTITIES].find_one({"_id": stake_id})
    assert (stake["status"], stake["notes"]) == ("staged", "reviewer note")

    after = compare_with_db(plan, sync_db)
    assert after["entities_existing"] == 253 and after["entities_new"] == 0
    assert sync_db[ndb.ENTITY_AUDIT].count_documents({"action": "seed_import"}) == 2


def test_apply_refuses_plan_with_errors(sync_db, plan):
    plan_with_error = build_plan()
    plan_with_error.errors.append("broken")
    with pytest.raises(ValueError):
        apply_plan(plan_with_error, sync_db)
    assert sync_db[ndb.ENTITIES].count_documents({}) == 0


def test_unique_exact_alias_enforced_by_index(sync_db, plan):
    apply_plan(plan, sync_db)
    with pytest.raises(DuplicateKeyError):
        sync_db[ndb.ENTITY_ALIASES].insert_one({
            "_id": "x", "entity_id": "someone-else", "kind": "organisation",
            "alias": "Emaar", "alias_norm": "emaar", "ambiguous": False,
        })
    # Ambiguous aliases may repeat across entities.
    sync_db[ndb.ENTITY_ALIASES].insert_one({
        "_id": "y", "entity_id": "someone-else", "kind": "organisation",
        "alias": "Wasl", "alias_norm": "wasl", "ambiguous": True,
    })


def test_names_of_a_company_added_by_hand_first_are_skipped(sync_db, plan):
    """A reviewer added "Arada" before the starting list was loaded: no names may point at the skipped seed id."""
    ndb.ensure_indexes_sync(sync_db)
    sync_db[ndb.ENTITIES].insert_one({"_id": "by-hand", "kind": "organisation", "canonical_name": "Arada",
                                      "canonical_norm": "arada", "type": "developer", "status": "active", "path_ids": ["by-hand"]})
    seed_arada = seed_entity_id("organisation", "Arada")
    assert compare_with_db(plan, sync_db)["aliases_name_taken"] == 2  # "Arada" and "Arada Group"

    result = apply_plan(plan, sync_db)
    assert result["aliases_name_taken"] == 2
    assert sync_db[ndb.ENTITIES].find_one({"_id": seed_arada}) is None
    assert sync_db[ndb.ENTITY_ALIASES].count_documents({"entity_id": seed_arada}) == 0
    # Nothing points at an entity that does not exist.
    entity_ids = set(sync_db[ndb.ENTITIES].distinct("_id"))
    assert set(sync_db[ndb.ENTITY_ALIASES].distinct("entity_id")) <= entity_ids
