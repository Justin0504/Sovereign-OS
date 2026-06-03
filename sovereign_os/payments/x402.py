"""
x402 / USDC settlement service for Sovereign-OS.

x402 is the HTTP-402-based agentic payment rail (Coinbase + Cloudflare). For
Sovereign-OS the relevant direction is *receiving* stablecoin settlement for a
completed job, so this implements the same `PaymentService.charge()` contract as
Stripe/Dummy: it settles `amount_cents` worth of USDC to a configured payout
address and returns a settlement reference (an on-chain tx hash in live mode, a
deterministic pseudo-hash in sandbox mode).

Two modes:
- Sandbox (default): no network calls. Produces a deterministic, idempotent
  settlement id derived from the charge inputs. Safe for demos, CI, and testnet
  rehearsal. Never moves real funds.
- Live: POSTs a settle request to an x402 facilitator (`X402_FACILITATOR_URL`)
  and returns the facilitator's tx hash. Requires `requests`.

Configuration (all optional; sensible testnet defaults):
- X402_SANDBOX           "true"/"false"  (default "true")
- X402_NETWORK           chain id/name   (default "base-sepolia")
- X402_ASSET             token symbol    (default "USDC")
- X402_PAY_TO            payout address  (receiving wallet)
- X402_FACILITATOR_URL   live settle endpoint
- X402_API_KEY           optional bearer for the facilitator
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# USDC has 6 decimals: 1 USD = 1_000_000 atomic units, so 1 cent = 10_000.
USDC_ATOMIC_PER_CENT = 10_000

DEFAULT_NETWORK = "base-sepolia"  # testnet by default — never mainnet unless explicitly set
DEFAULT_ASSET = "USDC"


def cents_to_usdc_atomic(amount_cents: int) -> int:
    """Convert USD cents to USDC atomic units (6 decimals)."""
    return int(amount_cents) * USDC_ATOMIC_PER_CENT


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class X402PaymentService:
    """
    Settle job revenue over the x402 / USDC rail.

    Implements the `PaymentService` protocol: `charge()` returns a settlement
    reference string that callers store in the ledger `ref` field.
    """

    pay_to: str = ""
    network: str = DEFAULT_NETWORK
    asset: str = DEFAULT_ASSET
    sandbox: bool = True
    facilitator_url: str = ""
    api_key: str = ""
    name: str = "x402"
    _timeout_s: float = field(default=15.0, repr=False)

    @classmethod
    def from_env(cls) -> "X402PaymentService":
        """Build from X402_* environment variables."""
        return cls(
            pay_to=os.getenv("X402_PAY_TO", ""),
            network=os.getenv("X402_NETWORK", DEFAULT_NETWORK),
            asset=os.getenv("X402_ASSET", DEFAULT_ASSET),
            sandbox=_env_bool("X402_SANDBOX", True),
            facilitator_url=os.getenv("X402_FACILITATOR_URL", ""),
            api_key=os.getenv("X402_API_KEY", ""),
        )

    @property
    def is_live(self) -> bool:
        """Live settlement requires sandbox disabled AND a facilitator endpoint."""
        return not self.sandbox and bool(self.facilitator_url)

    def _settlement_ref(self, amount_cents: int, currency: str, metadata: dict) -> str:
        """
        Deterministic sandbox settlement reference.

        Derived from (network, asset, pay_to, amount, idempotency key) so repeated
        charges for the same job collapse to the same id — mirrors on-chain
        idempotency without a network call.
        """
        idem = str(metadata.get("idempotency_key") or metadata.get("job_id") or "")
        seed = "|".join(
            [self.network, self.asset, self.pay_to, currency, str(amount_cents), idem]
        )
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]
        return f"x402_{self.network}_sbx_0x{digest}"

    async def charge(  # type: ignore[override]
        self, amount_cents: int, currency: str, *, metadata: dict | None = None
    ) -> str:
        currency = (currency or "usd").lower()
        metadata = dict(metadata or {})
        atomic = cents_to_usdc_atomic(amount_cents)

        if not self.is_live:
            ref = self._settlement_ref(amount_cents, currency, metadata)
            logger.info(
                "PAYMENTS: x402 SANDBOX settle %s %.2f -> %d %s atomic (network=%s, pay_to=%s, ref=%s)",
                currency,
                amount_cents / 100.0,
                atomic,
                self.asset,
                self.network,
                self.pay_to or "(unset)",
                ref,
            )
            return ref

        return await self._settle_live(amount_cents, atomic, currency, metadata)

    async def _settle_live(
        self, amount_cents: int, atomic: int, currency: str, metadata: dict
    ) -> str:
        import asyncio

        try:
            import requests  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - optional dependency
            raise ImportError(
                "requests is required for live x402 settlement; install with: pip install requests"
            ) from e

        if not self.pay_to:
            raise ValueError("X402_PAY_TO must be set for live x402 settlement.")

        idempotency_key = str(
            metadata.get("idempotency_key")
            or (metadata.get("job_id") and f"job-{metadata['job_id']}")
            or ""
        )[:255]
        payload = {
            "network": self.network,
            "asset": self.asset,
            "payTo": self.pay_to,
            "amount": str(atomic),  # atomic USDC units, string to avoid float loss
            "currency": currency,
            "metadata": metadata,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        url = self.facilitator_url.rstrip("/") + "/settle"

        def _post() -> str:
            resp = requests.post(url, json=payload, headers=headers, timeout=self._timeout_s)
            resp.raise_for_status()
            data = resp.json()
            tx = data.get("txHash") or data.get("tx_hash") or data.get("id")
            if not tx:
                raise ValueError(f"x402 facilitator returned no tx hash: {data!r}")
            return str(tx)

        loop = asyncio.get_running_loop()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                tx = await loop.run_in_executor(None, _post)
                logger.info(
                    "PAYMENTS: x402 LIVE settle succeeded: %s %.2f USDC (tx=%s, network=%s)",
                    currency,
                    amount_cents / 100.0,
                    tx,
                    self.network,
                )
                return tx
            except Exception as e:  # noqa: BLE001 - retried/logged below
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        logger.exception("PAYMENTS: x402 LIVE settle failed after 3 attempts")
        raise last_error  # type: ignore[misc]
