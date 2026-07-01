# Payments runbook

Sovereign-OS charges for a job **only after its audits pass**. The flow is already
wired end-to-end in the web job worker: complete → `PaymentService.charge()` →
record `job_income` in the ledger → store `payment_id`.

## Turn it on (Stripe — genuinely live)

```bash
pip install -e ".[llm,payments]"
export STRIPE_API_KEY=sk_test_xxx          # test key -> charges appear in Stripe test dashboard
export ANTHROPIC_API_KEY=sk-ant-xxx        # (or OPENAI_API_KEY) so workers actually run
export SOVEREIGN_JOB_WORKER_ENABLED=true   # start the 24/7 job processor
python -m sovereign_os.web.app             # http://localhost:8000
```

`create_payment_service()` auto-selects `StripePaymentService` when `STRIPE_API_KEY`
is set (test-mode uses the Charge API with `tok_visa`; live-mode uses PaymentIntent).
No key ⇒ `DummyPaymentService` (balance moves, Stripe does not).

**Verify one charge:** submit a job with `amount_cents` > 0 (dashboard or
`POST /api/jobs`), approve it (or `SOVEREIGN_AUTO_APPROVE_JOBS=true`); on audit
pass it charges — check the Stripe dashboard and `GET /api/status` balance.

## Other rails

| Rail | Status |
|---|---|
| **Stripe** | Live-capable now (set the key). |
| **x402 / USDC** | Sandbox-verified; set `PAYMENT_PROVIDER=x402` + `X402_*`. Live settlement needs a real facilitator (`X402_FACILITATOR_URL`, `X402_SANDBOX=false`) — validate before trusting. See [X402.md](X402.md). |
| **Outbound escrow (RentAHuman)** | Dry-run complete; run `python -m sovereign_os.oversight.rentahuman_preflight` and set `RENTAHUMAN_LIVE=true` only with a funded account. See [OVERSIGHT.md](OVERSIGHT.md). |
| **Dummy** | Demos/tests (`PAYMENT_PROVIDER=dummy`). |

## Reality check
Stripe is production-ready once you set the key. x402 and RentAHuman escrow are
sandbox/dry-run verified but have **not** been run against real funds — do a small
live test before relying on them.
