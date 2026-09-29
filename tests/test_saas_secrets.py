"""
Tests for credential handling in a hosted deployment.

The measured starting point was that a tenant's Anthropic, OpenAI and Stripe keys, plus
their API key, all sat in plaintext JSON on disk. A host compromise then hands an
attacker working credentials that bill the customer's own account, so these are the tests
that have to hold before anyone else's keys are accepted.

The two halves pull in opposite directions and are easy to swap by mistake: provider keys
must be recoverable (missions run on them) so they are encrypted; API keys must not be
(nothing needs the original) so they are hashed.
"""

import json
import tempfile
from pathlib import Path

import pytest

from sovereign_os.saas.secrets import (
    SECRET_ENV,
    SecretBox,
    SecretsNotConfigured,
    hash_api_key,
    is_encrypted,
    is_hashed,
    needs_migration,
    new_api_key,
    verify_api_key,
)
from sovereign_os.saas.tenancy import TenantConfig, TenantStore

SECRET = "test-deployment-secret-do-not-use-in-production"


@pytest.fixture
def store(tmp_path):
    return TenantStore(tmp_path / "tenants", secret=SECRET)


def _raw(store) -> str:
    return (store.root / "tenants.json").read_text("utf-8")


# ------------------------------------------------------------------ the secret box

def test_round_trip():
    box = SecretBox(SECRET)
    assert box.decrypt(box.encrypt("sk-ant-secret")) == "sk-ant-secret"


def test_ciphertext_does_not_contain_the_plaintext():
    assert "sk-ant-secret" not in SecretBox(SECRET).encrypt("sk-ant-secret")


def test_each_encryption_is_distinct():
    """A fresh nonce per encryption — reuse under one key breaks GCM."""
    box = SecretBox(SECRET)
    assert box.encrypt("same") != box.encrypt("same")


def test_encryption_is_idempotent():
    """Re-saving a record must not wrap an already-encrypted value twice."""
    box = SecretBox(SECRET)
    once = box.encrypt("sk-ant-secret")
    assert box.encrypt(once) == once
    assert box.decrypt(once) == "sk-ant-secret"


def test_empty_stays_empty():
    box = SecretBox(SECRET)
    assert box.encrypt("") == "" and box.decrypt("") == ""


def test_tampering_is_detected():
    """AES-GCM authenticates: a flipped byte must fail, not decrypt to garbage."""
    box = SecretBox(SECRET)
    blob = box.encrypt("sk-ant-secret")
    tampered = blob[:-2] + ("AA" if blob[-2:] != "AA" else "BB")
    with pytest.raises(Exception):
        box.decrypt(tampered)


def test_a_different_secret_cannot_read_it():
    blob = SecretBox(SECRET).encrypt("sk-ant-secret")
    with pytest.raises(Exception):
        SecretBox("some-other-deployment-secret").decrypt(blob)


def test_missing_deployment_secret_refuses_rather_than_falling_back(monkeypatch):
    """A fallback that 'works' is how plaintext credentials reach production."""
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(SecretsNotConfigured):
        SecretBox()


def test_plaintext_passes_through_and_is_flagged():
    """Legacy records stay readable, but say so."""
    box = SecretBox(SECRET)
    assert box.decrypt("sk-ant-legacy") == "sk-ant-legacy"
    assert needs_migration("sk-ant-legacy") is True
    assert needs_migration(box.encrypt("x")) is False
    assert needs_migration("") is False


# ------------------------------------------------------------------ API key hashing

def test_hash_is_not_reversible_and_verifies():
    key = new_api_key()
    stored = hash_api_key(key)
    assert key not in stored
    assert is_hashed(stored)
    assert verify_api_key(key, stored) is True
    assert verify_api_key(new_api_key(), stored) is False


def test_legacy_plaintext_key_still_authenticates():
    """A store written before hashing must keep working while it is migrated."""
    assert verify_api_key("sk_ten_legacy", "sk_ten_legacy") is True
    assert verify_api_key("wrong", "sk_ten_legacy") is False


def test_empty_never_authenticates():
    assert verify_api_key("", hash_api_key("k")) is False
    assert verify_api_key("k", "") is False


# ---------------------------------------------------------------- the store, on disk

def test_provider_keys_are_not_on_disk_in_the_clear(store):
    store.create("Acme", config=TenantConfig(
        anthropic_api_key="sk-ant-SUPERSECRET",
        openai_api_key="sk-oai-SUPERSECRET",
        stripe_api_key="sk_live_SUPERSECRET",
    ))
    raw = _raw(store)
    assert "SUPERSECRET" not in raw
    assert raw.count("enc:v1:") >= 3


def test_the_api_key_is_never_written_down(store):
    tenant = store.create("Acme")
    assert tenant.api_key.startswith("sk_ten_")      # shown once to the caller
    assert tenant.api_key not in _raw(store)
    assert is_hashed(json.loads(_raw(store))[tenant.id]["api_key_hash"])


def test_keys_are_usable_after_a_reload(store):
    """Encrypted at rest, decrypted in memory — missions still need the real string."""
    tenant = store.create("Acme", config=TenantConfig(anthropic_api_key="sk-ant-REAL"))
    reopened = TenantStore(store.root, secret=SECRET)
    assert reopened.get(tenant.id).config.anthropic_api_key == "sk-ant-REAL"


def test_authentication_survives_a_reload(store):
    tenant = store.create("Acme")
    reopened = TenantStore(store.root, secret=SECRET)
    found = reopened.by_api_key(tenant.api_key)
    assert found is not None and found.id == tenant.id
    assert reopened.by_api_key("sk_ten_wrong") is None


def test_updates_stay_encrypted(store):
    tenant = store.create("Acme")
    tenant.config.anthropic_api_key = "sk-ant-ROTATED"
    store.update(tenant)
    assert "sk-ant-ROTATED" not in _raw(store)
    assert TenantStore(store.root, secret=SECRET).get(
        tenant.id).config.anthropic_api_key == "sk-ant-ROTATED"


# ------------------------------------------------------------------- migration path

def test_a_plaintext_store_is_detected_and_upgraded(tmp_path):
    """The realistic case: a store that predates this module."""
    root = tmp_path / "legacy"
    root.mkdir(parents=True)
    legacy_key = "sk_ten_legacyplaintextkey"
    (root / "tenants.json").write_text(json.dumps({"ten_1": {
        "id": "ten_1", "name": "Old", "api_key": legacy_key, "plan": "pro",
        "created_ts": 1.0,
        "config": {"anthropic_api_key": "sk-ant-PLAINTEXT", "openai_api_key": "",
                   "stripe_api_key": "", "llm_provider": "", "x402_pay_to": "",
                   "charter_yaml": "", "earning_enabled": False},
    }}), encoding="utf-8")

    store = TenantStore(root, secret=SECRET)
    pending = store.pending_migrations()
    assert "ten_1" in pending
    assert set(pending["ten_1"]) == {"anthropic_api_key", "api_key"}

    # The legacy key still works before migration, or the operator is locked out.
    assert store.by_api_key(legacy_key) is not None

    assert store.migrate_secrets() == 1
    raw = (root / "tenants.json").read_text("utf-8")
    assert "sk-ant-PLAINTEXT" not in raw
    assert legacy_key not in raw
    assert store.pending_migrations() == {}

    # ...and the same key authenticates afterwards, since the hash derives from it.
    assert TenantStore(root, secret=SECRET).by_api_key(legacy_key) is not None


def test_require_encryption_fails_loudly_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    store = TenantStore(tmp_path / "t")
    with pytest.raises(SecretsNotConfigured):
        store.require_encryption()


def test_self_host_without_a_secret_still_works(tmp_path, monkeypatch):
    """A single operator on their own machine is not required to configure encryption."""
    monkeypatch.delenv(SECRET_ENV, raising=False)
    store = TenantStore(tmp_path / "solo")
    tenant = store.create("Solo", config=TenantConfig(anthropic_api_key="sk-ant-MINE"))
    assert store.by_api_key(tenant.api_key) is not None
    # The API key is hashed regardless — that costs nothing and is never recoverable.
    assert tenant.api_key not in (store.root / "tenants.json").read_text("utf-8")
