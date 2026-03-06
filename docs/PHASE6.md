# Phase 6b: On-chain settlements & sovereign identity (design)

Future work; this document describes the intended interfaces and extension points.

## On-chain financial settlements

**Goal:** Record mission income/payouts on a public or permissioned chain so that revenue and spending are verifiable off the existing ledger.

**Extension points:**

- **Ledger** — Today `UnifiedLedger` is file-backed. A future `OnChainLedgerAdapter` could:
  - Map `record_usd(amount_cents)` / `record_income_cents` to a stablecoin transfer or memo on a chosen chain.
  - Expose a minimal interface: `submit_settlement(tx_type, amount_cents, reference_id) -> tx_hash`.
- **Payments** — After a job is completed and audited, the existing `PaymentService` (e.g. Stripe) could be complemented by a `CryptoPaymentService` that:
  - Accepts a destination address and amount.
  - Returns a tx hash; the ledger records the outflow and the job’s `payment_id` could store the tx hash.
- **Config** — Charter or env could specify `settlement_chain`, `settlement_asset`, and credentials (e.g. wallet key or RPC). No implementation yet; this is the intended direction.

## Sovereign identity and compliance hooks

**Goal:** Let the entity (or each agent) have a stable, verifiable identity and attach compliance hooks (e.g. human approval, logging) for sensitive actions.

**Extension points:**

- **Identity** — A `SovereignIdentity` abstraction could:
  - Hold a stable ID (e.g. DID, or an internal UUID plus optional on-chain anchor).
  - Be attached to the Charter or to each Worker; the Auditor and Ledger could record which identity performed or approved an action.
- **Compliance hooks** — Before certain operations (e.g. `SPEND_USD` above a threshold, or “publish to external API”), the engine could:
  - Call a `ComplianceHook` interface: `check(action_type, context) -> Allow | Deny | RequestHumanApproval`.
  - Integrate with the existing Human-in-the-Loop (e.g. job approval) so that “request human approval” enqueues a task for the dashboard or an external system.
- **Audit** — The verifiable audit trail (Phase 6a) already gives a tamper-evident log; identity and compliance would add “who” and “whether allowed” to that story.

## Code stubs

The package `sovereign_os.compliance` provides concrete interfaces and stub implementations:

- **`SovereignIdentity`** / **`StubIdentity`** — `id`, optional `on_chain_anchor`; `to_dict()` for audit.
- **`ComplianceHook`** / **`StubComplianceHook`** — `check(action_type, context) -> ComplianceResult` (Allow / Deny / RequestHumanApproval).
- **`OnChainSettlement`** / **`StubOnChainSettlement`** — `submit_settlement(tx_type, amount_cents, reference_id, destination?) -> tx_hash` (stub returns a placeholder hash).

Wire these into the engine and ledger when implementing Phase 6b for real.

## Status

- **Phase 6a (Verifiable audit trail):** Implemented. See [AUDIT_PROOF.md](AUDIT_PROOF.md).
- **Phase 6b:** Design + stubs in `sovereign_os.compliance`. Full implementations (real chain, real compliance rules) are left for future contributions.
