"""
A tenant must be able to rotate their credential and take their data back.

Neither was possible. The workspace key is the only credential, it is stored hashed and
shown once, so a leaked key was permanent: whoever held it could spend the tenant's
provider key indefinitely, and the tenant could not rotate it, revoke it, or delete the
workspace. "Create a new one" is not a remedy while the old one still works.

Deletion existed at the store level and left the data directory on disk -- the encrypted
provider keys, the ledger, the audit trail, the trust store. A delete that tells the
tenant their workspace is gone while their credentials stay on our volume.
"""

import tempfile

import pytest

from sovereign_os.saas.api import create_saas_app
from sovereign_os.saas.tenancy import TenantStore


@pytest.fixture
def store():
    return TenantStore(tempfile.mkdtemp())


@pytest.fixture
def client(store):
    try:
        from fastapi.testclient import TestClient
        return TestClient(create_saas_app(store))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {e}")


def _signup(client, name="Acme"):
    d = client.post("/saas/tenants", json={"name": name, "plan": "free"}).json()
    return d["api_key"], d["tenant"]["id"]


def _me(client, key):
    return client.get("/saas/tenants/me", headers={"X-Tenant-Key": key})


# ------------------------------------------------------------------- rotation

def test_rotating_returns_a_new_key_that_works(client):
    key, _ = _signup(client)
    r = client.post("/saas/tenants/me/rotate-key", headers={"X-Tenant-Key": key})
    assert r.status_code == 200
    new = r.json()["api_key"]
    assert new != key
    assert _me(client, new).status_code == 200


def test_the_old_key_stops_working_immediately(client):
    """The whole point: a leaked key has to become useless, not merely be superseded."""
    key, _ = _signup(client)
    client.post("/saas/tenants/me/rotate-key", headers={"X-Tenant-Key": key})
    assert _me(client, key).status_code == 401


def test_rotation_keeps_the_workspace_and_its_settings(client):
    """Rotating a credential must not look like starting over."""
    key, tid = _signup(client)
    client.put("/saas/tenants/me/config",
               json={"charter_yaml": "mission: Keep this.\n"},
               headers={"X-Tenant-Key": key})
    new = client.post("/saas/tenants/me/rotate-key",
                      headers={"X-Tenant-Key": key}).json()["api_key"]
    me = _me(client, new).json()
    assert me["tenant"]["id"] == tid
    assert "Keep this." in me["tenant"]["config"]["charter_yaml"]


def test_rotation_needs_a_valid_key(client):
    assert client.post("/saas/tenants/me/rotate-key",
                       headers={"X-Tenant-Key": "sk_ten_nope"}).status_code == 401


def test_the_new_key_is_not_recoverable_afterwards(client):
    """Stored hashed, like the first one. Shown once means once."""
    key, _ = _signup(client)
    new = client.post("/saas/tenants/me/rotate-key",
                      headers={"X-Tenant-Key": key}).json()["api_key"]
    body = _me(client, new).text
    assert new not in body


# ------------------------------------------------------------------- deletion

def test_deletion_requires_the_workspace_id_as_confirmation(client):
    key, tid = _signup(client)
    r = client.request("DELETE", "/saas/tenants/me", json={},
                       headers={"X-Tenant-Key": key})
    assert r.status_code == 400
    assert tid in r.json()["detail"]
    assert _me(client, key).status_code == 200, "a refused delete must change nothing"


def test_a_wrong_confirmation_does_not_delete(client):
    key, _ = _signup(client)
    r = client.request("DELETE", "/saas/tenants/me", json={"confirm": "ten_whatever"},
                       headers={"X-Tenant-Key": key})
    assert r.status_code == 400
    assert _me(client, key).status_code == 200


def test_a_confirmed_delete_removes_the_workspace(client):
    key, tid = _signup(client)
    r = client.request("DELETE", "/saas/tenants/me", json={"confirm": tid},
                       headers={"X-Tenant-Key": key})
    assert r.status_code == 200 and r.json()["deleted"] == tid
    assert _me(client, key).status_code == 401


def test_a_confirmed_delete_removes_the_stored_credentials_from_disk(client, store):
    """
    The load-bearing one. The record going away is not the same as the data going away,
    and what is on disk is the tenant's encrypted provider keys.
    """
    key, tid = _signup(client)
    client.put("/saas/tenants/me/config",
               json={"anthropic_api_key": "sk-ant-api03-" + "q" * 32},
               headers={"X-Tenant-Key": key})
    data_dir = store.data_dir(tid)
    assert data_dir.is_dir(), "nothing was written, so this test proves nothing"

    client.request("DELETE", "/saas/tenants/me", json={"confirm": tid},
                   headers={"X-Tenant-Key": key})
    assert not data_dir.exists(), "the workspace was deleted but its data was left behind"


def test_deleting_one_workspace_leaves_the_others_alone(client, store):
    k1, t1 = _signup(client, "One")
    k2, t2 = _signup(client, "Two")
    client.request("DELETE", "/saas/tenants/me", json={"confirm": t1},
                   headers={"X-Tenant-Key": k1})
    assert _me(client, k2).status_code == 200
    assert store.data_dir(t2).is_dir()


def test_the_store_refuses_to_walk_out_of_its_own_root(store):
    """
    Deletion removes a directory, so the path has to be checked rather than trusted.
    Tenant ids are generated here, but a guard that only holds for well-formed input is
    not a guard.
    """
    assert store.delete("../../etc") is False
    assert store.delete("") is False


# --------------------------------------------------------------------- export

def test_a_tenant_can_export_everything_before_deleting_it(client):
    """
    The counterpart to deletion. Being able to destroy your audit trail without being
    able to keep a copy is not control over your data.
    """
    key, tid = _signup(client)
    client.put("/saas/tenants/me/config",
               json={"charter_yaml": "mission: Export me.\n"},
               headers={"X-Tenant-Key": key})
    j = client.get("/saas/tenants/me/export", headers={"X-Tenant-Key": key}).json()
    assert j["tenant"]["id"] == tid
    assert "Export me." in j["charter_yaml"]
    assert isinstance(j["ledger"], list) and isinstance(j["audit_trail"], list)
    assert j["exported_at"].endswith("Z")


def test_the_export_does_not_hand_back_a_provider_key(client):
    """
    It is the tenant's record of what happened, not a way to read back a credential we
    promised to keep encrypted. A leak here would undo the masking everywhere else.
    """
    key, _ = _signup(client)
    secret = "sk-ant-api03-" + "q" * 32
    client.put("/saas/tenants/me/config", json={"anthropic_api_key": secret},
               headers={"X-Tenant-Key": key})
    body = client.get("/saas/tenants/me/export", headers={"X-Tenant-Key": key}).text
    assert "q" * 32 not in body
    assert secret not in body


def test_export_needs_a_valid_key(client):
    assert client.get("/saas/tenants/me/export",
                      headers={"X-Tenant-Key": "sk_ten_nope"}).status_code == 401


def test_export_survives_a_corrupt_line_in_the_trail(client, store):
    """Append-only files get truncated writes; an export that 500s is no export."""
    key, tid = _signup(client)
    (store.data_dir(tid) / "audit.jsonl").write_text('{"ok":true}\n{"truncated"\n')
    j = client.get("/saas/tenants/me/export", headers={"X-Tenant-Key": key}).json()
    assert len(j["audit_trail"]) == 2
    assert j["audit_trail"][1]["unparsed"].startswith('{"truncated"')
