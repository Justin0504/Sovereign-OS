"""
The tenant config endpoint must refuse what it cannot honour.

Found by walking the real signup-to-mission journey against the deployed service. Three
inputs were accepted with 200 OK and then not applied:

    {"plan": "enterprise-unlimited"}      -> 200, tenant stays on "free"
    {"charter_yaml": "mission: [unclosed"} -> 200, has_charter=true
    {"anthropic_api_key": "not-a-key"}     -> 200, stored

The charter one is the governance failure. A stored charter that fails to parse was
caught at use and silently replaced with a DEFAULT charter carrying DEFAULT fiscal
boundaries, so a tenant who mistyped their daily spending cap was told it was in force
while every mission ran under limits they never chose. That is a control plane reporting
a constraint the effect site is not applying -- the exact thing this system exists to
prevent, happening inside it.
"""

import tempfile

import pytest

from sovereign_os.saas.api import create_saas_app
from sovereign_os.saas.tenancy import TenantStore

VALID_CHARTER = """
mission: Test mission for a governed workspace.
fiscal_boundaries:
  daily_burn_max_usd: 5.0
  max_budget_usd: 100.0
  currency: USD
"""


@pytest.fixture
def client():
    store = TenantStore(tempfile.mkdtemp())
    try:
        from fastapi.testclient import TestClient
        return TestClient(create_saas_app(store))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {e}")


@pytest.fixture
def key(client):
    r = client.post("/saas/tenants", json={"name": "Acme", "plan": "free"})
    return r.json()["api_key"]


def _put(client, key, body):
    return client.put("/saas/tenants/me/config", json=body, headers={"X-Tenant-Key": key})


def _me(client, key):
    return client.get("/saas/tenants/me", headers={"X-Tenant-Key": key}).json()


# ------------------------------------------------------------------ the charter

def test_a_charter_that_is_not_valid_yaml_is_refused(client, key):
    r = _put(client, key, {"charter_yaml": "mission: [unclosed"})
    assert r.status_code == 400
    assert "yaml" in r.json()["detail"].lower()


def test_a_refused_charter_is_not_stored(client, key):
    """A 400 that still wrote the value would be worse than the original bug."""
    _put(client, key, {"charter_yaml": "mission: [unclosed"})
    assert _me(client, key)["tenant"]["config"]["has_charter"] is False


def test_yaml_that_is_not_a_mapping_is_refused(client, key):
    r = _put(client, key, {"charter_yaml": "- just\n- a\n- list"})
    assert r.status_code == 400
    assert "mapping" in r.json()["detail"]


def test_well_formed_yaml_that_is_not_a_charter_is_refused(client, key):
    """Parsing is not the same as validating; the schema has to be checked too."""
    r = _put(client, key, {"charter_yaml": "fiscal_boundaries: {daily_burn_max_usd: -5}"})
    assert r.status_code == 400


def test_a_valid_charter_is_accepted_and_recorded(client, key):
    assert _put(client, key, {"charter_yaml": VALID_CHARTER}).status_code == 200
    assert _me(client, key)["tenant"]["config"]["has_charter"] is True


def test_clearing_the_charter_is_allowed(client, key):
    _put(client, key, {"charter_yaml": VALID_CHARTER})
    assert _put(client, key, {"charter_yaml": ""}).status_code == 200
    assert _me(client, key)["tenant"]["config"]["has_charter"] is False


def test_a_charter_that_stops_loading_refuses_missions_rather_than_defaulting(client, key):
    """
    The load-bearing one. Simulates a stored charter that no longer parses -- a schema
    change, a bad migration -- by writing one past the API, then checks the runtime
    refuses instead of substituting default fiscal boundaries.
    """
    from sovereign_os.saas.runtime import _charter_for

    store = TenantStore(tempfile.mkdtemp())
    t = store.create("Acme", plan="free")
    t.config.charter_yaml = "mission: [unclosed"
    store.update(t)

    with pytest.raises(PermissionError) as exc:
        _charter_for(t)
    assert "refused" in str(exc.value)


def test_no_charter_at_all_still_gets_a_default(client, key):
    """Failing closed applies to a broken charter, not to an absent one."""
    from sovereign_os.saas.runtime import _charter_for

    store = TenantStore(tempfile.mkdtemp())
    t = store.create("Acme", plan="free")
    charter = _charter_for(t)
    assert charter.mission


# --------------------------------------------------------------------- the plan

def test_an_unknown_plan_is_refused_with_the_valid_names(client, key):
    r = _put(client, key, {"plan": "enterprise-unlimited"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "unknown plan" in detail and "free" in detail


def test_a_known_plan_still_works(client, key):
    r = _put(client, key, {"plan": "pro"})
    assert r.status_code == 200
    assert r.json()["tenant"]["plan"] == "pro"


# ------------------------------------------------------------- the provider key

@pytest.mark.parametrize("bad", ["not-a-key-at-all", "$ANTHROPIC_API_KEY",
                                 "<your-key-here>", "sk-ant with a space", "sk-short"])
def test_input_that_cannot_be_a_key_is_refused(client, key, bad):
    assert _put(client, key, {"anthropic_api_key": bad}).status_code == 400


def test_a_plausible_key_is_accepted_and_masked(client, key):
    r = _put(client, key, {"anthropic_api_key": "sk-ant-api03-" + "x" * 32})
    assert r.status_code == 200
    stored = _me(client, key)["tenant"]["config"]["anthropic_api_key"]
    assert stored and "x" * 32 not in stored, "the key must come back masked"


def test_the_check_is_loose_enough_to_survive_a_format_change(client, key):
    """
    Deliberately not a strict pattern: provider key formats change, and a tight regex
    breaks the product the day one does. Anything with an sk- prefix and real length
    must pass, whatever follows it.
    """
    assert _put(client, key, {"openai_api_key": "sk-proj-" + "A1b2" * 12}).status_code == 200


# ------------------------------------------------------------------- atomicity

def test_a_rejected_field_does_not_half_apply_the_request(client, key):
    """
    One request, one outcome. Validation happens before any mutation, so a body with a
    good charter and a bad plan leaves neither applied.
    """
    r = _put(client, key, {"charter_yaml": VALID_CHARTER, "plan": "does-not-exist"})
    assert r.status_code == 400
    me = _me(client, key)
    assert me["tenant"]["config"]["has_charter"] is False, "the charter leaked through a 400"
    assert me["tenant"]["plan"] == "free"
