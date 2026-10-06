"""Entity manager endpoints against a throwaway MongoDB database (needs TEST_MONGO_URL)."""
import pytest

from src.newsroom.seed import apply_plan, build_plan, seed_entity_id

ORG = "organisation"


@pytest.fixture
def seeded(api, sync_db):
    apply_plan(build_plan(), sync_db)
    return api


def _id(kind, name):
    return seed_entity_id(kind, name)


def _create(api, **body):
    payload = {"kind": ORG, "type": "developer", "status": "staged", **body}
    return api.post("/newsroom/entities", json=payload)


def test_requires_api_key(seeded):
    assert seeded.get("/newsroom/entities", headers={"X-API-Key": "wrong"}).status_code == 401


# --- Reads -------------------------------------------------------------------

def test_list_filters(seeded):
    body = seeded.get("/newsroom/entities", params={"kind": ORG, "status": "active", "limit": 500}).json()
    assert body["total"] == 49
    assert all(e["kind"] == ORG and e["status"] == "active" for e in body["items"])
    assert seeded.get("/newsroom/entities", params={"kind": "location"}).json()["total"] == 71


def test_search_by_name_and_alias(seeded):
    by_alias = seeded.get("/newsroom/entities", params={"kind": "location", "q": "UAE"}).json()
    assert "United Arab Emirates" in [e["canonical_name"] for e in by_alias["items"]]
    by_name = seeded.get("/newsroom/entities", params={"q": "emaar"}).json()
    assert "Emaar Properties" in [e["canonical_name"] for e in by_name["items"]]


def test_get_entity_with_aliases(seeded):
    body = seeded.get(f"/newsroom/entities/{_id(ORG, 'DAMAC Properties')}").json()
    exact = sorted(a["alias"] for a in body["aliases"] if not a["ambiguous"])
    assert exact == ["DAMAC", "DAMAC Properties"]  # "Damac" and "Damac Properties" normalise to the same names
    assert [a["alias"] for a in body["aliases"] if a["ambiguous"]] == ["DAMAC Group"]
    assert seeded.get("/newsroom/entities/does-not-exist").status_code == 404


# --- Create ------------------------------------------------------------------

def test_create_entity_and_audit(seeded):
    resp = _create(seeded, canonical_name="Nshama", aliases=["Nshama Developments"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert sorted(a["alias"] for a in body["aliases"]) == ["Nshama", "Nshama Developments"]
    assert body["path_ids"] == [body["id"]]
    audit = seeded.get(f"/newsroom/entities/{body['id']}/audit").json()
    assert audit[0]["action"] == "create"
    assert audit[0]["actor"] == "tester@propt.global"


@pytest.mark.parametrize(
    "body, code",
    [
        ({"canonical_name": "Emaar Properties"}, 409),                    # name exists
        ({"canonical_name": "New Co", "aliases": ["DAMAC"]}, 409),       # exact alias taken
        ({"canonical_name": "Wasl"}, 409),                                # ambiguous elsewhere
        ({"canonical_name": "New Co", "aliases": ["waslgroup"]}, 409),   # on the ignore list
        ({"canonical_name": "New Co", "aliases": ["@newco"]}, 422),      # handle
        ({"canonical_name": "New Co", "type": "unknown", "status": "active"}, 422),
        ({"canonical_name": "New Co", "parent_id": "missing"}, 422),
        ({"canonical_name": "New Co", "country_id": seed_entity_id("location", "Dubai")}, 422),  # a city, not a country
    ],
)
def test_create_rejections(seeded, body, code):
    assert _create(seeded, **body).status_code == code


def test_create_with_ambiguous_canonical(seeded):
    resp = _create(seeded, canonical_name="Wasl Holding Two", ambiguous_aliases=["Wasl"])
    assert resp.status_code == 201, resp.text
    ambiguous = [a["alias"] for a in resp.json()["aliases"] if a["ambiguous"]]
    assert ambiguous == ["Wasl"]


# --- Update ------------------------------------------------------------------

def test_switch_on_requires_a_type(seeded):
    created = _create(seeded, canonical_name="Mystery Co", type="unknown").json()
    url = f"/newsroom/entities/{created['id']}"
    assert seeded.patch(url, json={"status": "active"}).status_code == 422
    resp = seeded.patch(url, json={"type": "developer", "status": "active"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_rename_conflict(seeded):
    resp = seeded.patch(f"/newsroom/entities/{_id(ORG, 'Meraas')}", json={"canonical_name": "Emaar Properties"})
    assert resp.status_code == 409


def test_parent_loop_rejected(seeded):
    resp = seeded.patch(f"/newsroom/entities/{_id(ORG, 'Dubai Holding')}", json={"parent_id": _id(ORG, "Meraas")})
    assert resp.status_code == 422


def test_parent_change_rewrites_descendant_paths(seeded):
    a = _create(seeded, canonical_name="Tree A").json()
    b = _create(seeded, canonical_name="Tree B", parent_id=a["id"]).json()
    c = _create(seeded, canonical_name="Tree C", parent_id=b["id"]).json()
    assert c["path_ids"] == [a["id"], b["id"], c["id"]]

    moved = seeded.patch(f"/newsroom/entities/{b['id']}", json={"parent_id": None}).json()
    assert moved["path_ids"] == [b["id"]]
    assert seeded.get(f"/newsroom/entities/{c['id']}").json()["path_ids"] == [b["id"], c["id"]]


def test_update_is_audited_with_before_and_after(seeded):
    url = f"/newsroom/entities/{_id(ORG, 'Stake')}"
    seeded.patch(url, json={"status": "staged"})
    entry = seeded.get(f"{url}/audit").json()[0]
    assert entry["before"] == {"status": "active"}
    assert entry["after"] == {"status": "staged"}


def test_no_change_writes_no_audit(seeded):
    url = f"/newsroom/entities/{_id(ORG, 'Stake')}"
    seeded.patch(url, json={"status": "active"})
    assert seeded.get(f"{url}/audit").json() == []


# --- Aliases -----------------------------------------------------------------

def test_alias_add_and_delete(seeded):
    url = f"/newsroom/entities/{_id(ORG, 'Wasl Group')}"
    assert seeded.post(f"{url}/aliases", json={"alias": "Wasl"}).status_code == 409  # ambiguous elsewhere
    resp = seeded.post(f"{url}/aliases", json={"alias": "Wasl", "ambiguous": True})  # ambiguous may repeat
    assert resp.status_code == 201
    alias = next(a for a in resp.json()["aliases"] if a["alias"] == "Wasl")
    assert seeded.post(f"{url}/aliases", json={"alias": "Wasl", "ambiguous": True}).status_code == 409  # duplicate

    resp = seeded.delete(f"{url}/aliases/{alias['id']}")
    assert resp.status_code == 200
    assert "Wasl" not in [a["alias"] for a in resp.json()["aliases"]]
    assert seeded.delete(f"{url}/aliases/{alias['id']}").status_code == 404


# --- Ignore list -------------------------------------------------------------

def test_ignore_list(seeded):
    assert len(seeded.get("/newsroom/ignore").json()) == 19
    assert seeded.post("/newsroom/ignore", json={"string": "Stake"}).status_code == 409  # an alias
    assert seeded.post("/newsroom/ignore", json={"string": "WASLGROUP"}).status_code == 409  # already there
    created = seeded.post("/newsroom/ignore", json={"string": "propertynews.ae", "reason": "website"})
    assert created.status_code == 201
    assert seeded.delete(f"/newsroom/ignore/{created.json()['id']}").status_code == 204
    assert seeded.delete(f"/newsroom/ignore/{created.json()['id']}").status_code == 404
