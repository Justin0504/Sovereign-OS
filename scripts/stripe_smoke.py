"""
Stripe test-key smoke check for Sovereign-OS.

Charges $1.00 through the SAME StripePaymentService the governance engine uses,
so a green run proves the key + code path + Dashboard wiring all work — without
starting the web app.

Safe by design:
- Requires an `sk_test_` key. Refuses to run against a live `sk_live_` key.
- $1.00 on Stripe's built-in test card (tok_visa); no real money moves.

Usage:
    export STRIPE_API_KEY=sk_test_xxxxxxxx
    python scripts/stripe_smoke.py            # $1.00 default
    python scripts/stripe_smoke.py 250        # $2.50
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path

from sovereign_os.payments.service import StripePaymentService


def main() -> int:
    key = os.getenv("STRIPE_API_KEY", "").strip()
    if not key:
        print("✗ STRIPE_API_KEY not set. Run: export STRIPE_API_KEY=sk_test_...")
        return 2
    if not key.startswith("sk_test_"):
        print("✗ Refusing to run: this smoke test only accepts an sk_test_ key "
              "(you gave one starting with %r...). Use your Sandbox test key." % key[:8])
        return 2

    amount_cents = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    print(f"→ Charging ${amount_cents / 100:.2f} via StripePaymentService (test mode)…")

    try:
        svc = StripePaymentService(api_key=key)
    except ImportError:
        print("✗ stripe SDK missing. Run: pip install stripe")
        return 2

    try:
        charge_id = asyncio.run(
            svc.charge(amount_cents, "usd", metadata={"job_id": "smoke", "goal": "Stripe smoke test"})
        )
    except Exception as e:  # noqa: BLE001 - surface the raw Stripe error to the user
        print(f"✗ Charge failed: {e}")
        return 1

    print(f"✓ Charge succeeded — id={charge_id}")
    print("  View it at: https://dashboard.stripe.com/test/payments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
