"""
Secret handling for hosted multi-tenant deployments.

Two different problems live here, and they need opposite treatments.

**Tenant provider keys** (Anthropic, OpenAI, Stripe) must be *recoverable*: missions run
on them, so the server has to be able to present the original string. They are therefore
encrypted, not hashed — AES-GCM under a key derived from a deployment secret, with the
authentication tag catching tampering as well as corruption.

**Tenant API keys** must *not* be recoverable. Nothing ever needs the original after it
is handed to the tenant once at signup; authentication only needs to decide whether a
presented string matches. So they are hashed, like passwords, and the plaintext is never
written down. A stolen database then yields no working credentials.

Getting these the wrong way round is the common failure: hashing the provider keys makes
the product stop working, and storing the API keys recoverably hands an attacker every
tenant's account.

The deployment secret comes from `SOVEREIGN_SAAS_SECRET`. Without it a hosted deployment
refuses to encrypt rather than silently falling back to plaintext — a fallback that
"works" is exactly how plaintext credentials reach production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _secrets

SECRET_ENV = "SOVEREIGN_SAAS_SECRET"

# Marks a value this module produced, so a store can tell encrypted values apart from
# legacy plaintext and migrate without guessing.
_PREFIX = "enc:v1:"
_HASH_PREFIX = "sha256:v1:"

_KDF_ROUNDS = 200_000
_KDF_SALT = b"sovereign-os/saas/secret-box/v1"


class SecretsNotConfigured(RuntimeError):
    """Raised when encryption is required but no deployment secret is set."""


def _derive_key(secret: str) -> bytes:
    """Stretch the deployment secret into a 32-byte AES key."""
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), _KDF_SALT, _KDF_ROUNDS, 32)


class SecretBox:
    """
    Authenticated encryption for values that must be read back.

    A fresh 96-bit nonce per encryption — reusing a nonce under the same key breaks
    GCM badly — stored alongside the ciphertext.
    """

    def __init__(self, secret: str | None = None) -> None:
        resolved = (secret if secret is not None else os.getenv(SECRET_ENV, "")).strip()
        if not resolved:
            raise SecretsNotConfigured(
                f"{SECRET_ENV} is not set. A hosted deployment stores tenant provider "
                f"keys, which must be encrypted at rest; refusing to run without a "
                f"deployment secret. Generate one with: "
                f"python -c \"import secrets;print(secrets.token_urlsafe(48))\""
            )
        self._key = _derive_key(resolved)

    @staticmethod
    def is_available(secret: str | None = None) -> bool:
        """Whether encryption can be performed in this process."""
        return bool((secret if secret is not None else os.getenv(SECRET_ENV, "")).strip())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a value. Empty input stays empty — there is nothing to protect."""
        if not plaintext:
            return ""
        if is_encrypted(plaintext):
            return plaintext          # idempotent: never double-wrap
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        nonce = os.urandom(12)
        blob = nonce + AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return _PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")

    def decrypt(self, value: str) -> str:
        """
        Decrypt a value produced by `encrypt`.

        A value without the marker is returned unchanged: stores written before
        encryption existed hold plaintext, and refusing to read them would lock an
        operator out of their own data. `needs_migration` is how that state is surfaced
        rather than hidden.
        """
        if not value or not is_encrypted(value):
            return value
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        raw = base64.urlsafe_b64decode(value[len(_PREFIX):].encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(self._key).decrypt(nonce, ct, None).decode("utf-8")


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def needs_migration(value: str) -> bool:
    """True for a non-empty secret still sitting in plaintext."""
    return bool(value) and not is_encrypted(value)


# ------------------------------------------------------------------ API key hashing

def new_api_key() -> str:
    """A fresh tenant API key. Returned to the tenant once and never stored as-is."""
    return "sk_ten_" + _secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """
    Irreversible digest of an API key, for storage.

    Plain SHA-256 rather than a password KDF on purpose: an API key is 32 bytes of
    machine-generated entropy, so brute force is already infeasible and the digest is on
    the hot path of every authenticated request. Passwords — low entropy, human-chosen —
    would need argon2 instead.
    """
    if not api_key:
        return ""
    return _HASH_PREFIX + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, stored: str) -> bool:
    """
    Constant-time check of a presented key against what the store holds.

    Accepts a legacy plaintext record too, so a store written before hashing still
    authenticates while it is migrated.
    """
    if not api_key or not stored:
        return False
    if stored.startswith(_HASH_PREFIX):
        return hmac.compare_digest(hash_api_key(api_key), stored)
    return hmac.compare_digest(api_key, stored)


def is_hashed(stored: str) -> bool:
    return isinstance(stored, str) and stored.startswith(_HASH_PREFIX)
