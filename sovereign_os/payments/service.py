"""
Payment service abstractions for Sovereign-OS.

These are intentionally minimal: GovernanceEngine / Web UI can call a single
`charge` method without depending on a specific provider.

Out of the box we provide:
- DummyPaymentService: simulates a successful charge (for demos/tests)
- StripePaymentService: real integration via the official stripe Python SDK
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class PaymentService(Protocol):
    """Abstract payment interface used by higher-level orchestration."""

    async def charge(self, amount_cents: int, currency: str, *, metadata: dict | None = None) -> str:
        """
        Charge the customer.

        Returns:
            provider-specific payment identifier (e.g. Stripe payment_intent id)
        Raises:
            Exception on failure (caller should log + mark mission as payment_failed).
        """
        ...


@dataclass
class DummyPaymentService:
    """
    No-op payment service for local demos.

    Always "succeeds" and logs the charge; records nothing with any provider.
    """

    name: str = "dummy"

    async def charge(self, amount_cents: int, currency: str, *, metadata: dict | None = None) -> str:  # type: ignore[override]
        logger.info(
            "PAYMENTS: Dummy charge of %s %.2f (metadata=%s)",
            currency,
            amount_cents / 100.0,
            metadata or {},
        )
        # Fabricated id that still lets us track in UnifiedLedger `ref` fields.
        return f"dummy_{currency}_{amount_cents}"


@dataclass
class StripePaymentService:
    """
    Stripe-based implementation using the official `stripe` Python SDK.

    Requires:
        - pip install 'stripe'
        - STRIPE_API_KEY env var set
    """

    api_key: str
    default_currency: str = "usd"

    def __post_init__(self) -> None:
        try:
            import stripe as _stripe  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - optional dependency
            raise ImportError(
                "stripe package is required for StripePaymentService; "
                "install with: pip install 'stripe'"
            ) from e
        _stripe.api_key = self.api_key
        self._stripe = _stripe

    async def charge(self, amount_cents: int, currency: str, *, metadata: dict | None = None) -> str:  # type: ignore[override]
        import asyncio

        currency = (currency or self.default_currency).lower()
        metadata = dict(metadata or {})
        idempotency_key = metadata.pop("idempotency_key", None) or (metadata.get("job_id") and f"job-{metadata['job_id']}")

        def _create_charge() -> str:
            # Test mode: use Charge API with test token so the charge appears in Stripe Dashboard.
            # Production would use PaymentIntent + customer payment method.
            if self.api_key.startswith("sk_test_"):
                kwargs = {
                    "amount": amount_cents,
                    "currency": currency,
                    "source": "tok_visa",
                    "description": metadata.get("goal", "Sovereign-OS job")[:500],
                }
                if idempotency_key:
                    kwargs["idempotency_key"] = str(idempotency_key)[:255]
                charge = self._stripe.Charge.create(**kwargs)
                return charge.id
            # Live key: PaymentIntent (caller must attach payment_method separately in production).
            kwargs = {
                "amount": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "confirm": True,
            }
            if idempotency_key:
                kwargs["idempotency_key"] = str(idempotency_key)[:255]
            intent = self._stripe.PaymentIntent.create(**kwargs)
            return intent.id

        loop = asyncio.get_running_loop()
        last_error = None
        for attempt in range(3):
            try:
                pid = await loop.run_in_executor(None, _create_charge)
                logger.info(
                    "PAYMENTS: Stripe charge succeeded: %s %.2f (id=%s)",
                    currency, amount_cents / 100.0, pid,
                )
                return pid
            except Exception as e:
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        logger.exception("PAYMENTS: Stripe charge failed after 3 attempts")
        raise last_error  # type: ignore[misc]


def create_payment_service() -> PaymentService:
    """
    Best-effort payment service factory.

    If STRIPE_API_KEY is set and `stripe` is installed -> StripePaymentService.
    Otherwise -> DummyPaymentService (for demos and tests).
    """
    key = os.getenv("STRIPE_API_KEY")
    logger.warning("PAYMENTS: create_payment_service called. STRIPE_API_KEY set=%s", bool(key))
    if key:
        try:
            svc = StripePaymentService(api_key=key)
            mode = "test" if key.startswith("sk_test_") else "live"
            logger.warning(
                "PAYMENTS: Using StripePaymentService (%s mode). Charges will appear in Stripe Dashboard.", mode
            )
            return svc
        except Exception as e:  # pragma: no cover - optional
            logger.warning("PAYMENTS: StripePaymentService init FAILED: %s. Falling back to Dummy.", e)
    logger.warning(
        "PAYMENTS: Using DummyPaymentService (Balance will change but Stripe will not). "
        "Set STRIPE_API_KEY and run: pip install stripe"
    )
    return DummyPaymentService()

