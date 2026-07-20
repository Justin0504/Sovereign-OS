# Platform Integration Status (honest)

Grounded in a live probe of each endpoint (2026-07) plus a code audit. "Ingest" =
discovering open bounties; "Delivery" = claiming/submitting completed work. All
money/reputation-bearing delivery is **dry-run** until the platform's `*_LIVE` flag is
set.

| Platform | Endpoint live now | Ingest | Delivery | Notes / first-live risk |
|---|---|---|---|---|
| **TaskBounty** | ✅ 200 | ✅ real (fields confirmed: `id`, `title`, `short_summary`, `bounty_cents`, `currency`, `status`, `tags`) | ⚠️ submit path **undocumented** (`TASKBOUNTY_SUBMIT_PATH`, default `/tasks/{id}/submit`) | Ingest verified against live API. Confirm the submit path on your account before `TASKBOUNTY_LIVE=true`. |
| **StacksTasker** | ✅ 200 | ✅ real (`id`, `title`, `description`, `bounty` STX, `bountyMicroStx`, `status`, `network`) | ⚠️ submit path undocumented (`STACKSTASKER_SUBMIT_PATH`); dynamic bid needs `reward_cents`+`est_cost_cents` in contact | Testnet (STX). Bid pricing wired; submit path guessed. |
| **BotBounty** | ✅ 200 (`{count, bounties, tip}`) | ✅ real (`id`, `title`, `description`, `amount`, per-record `currency`, `status`) | ✅ **now implemented** (`delivery/botbounty.py`: claim via `claimEndpoint` → submit) | Was discovery-only (orphaned work); delivery added. Submit body inferred (`BOTBOUNTY_SUBMIT_PATH`), dry-run until `BOTBOUNTY_LIVE`. |
| **RentAHuman** (outbound) | ✅ 200 | N/A (we post) | ✅ real, full escrow lifecycle + preflight | `rah_live_*` key; fund/release move real money. Run `rentahuman_preflight`. |
| **APB / x402** | depends on publisher | ✅ tolerant parser (`/.well-known/bounties.json`) | ✅ if bounty carries a claim URL (else logged + skipped, not silent) | Reward settles via x402/USDC. Run `x402_preflight` before `X402_SANDBOX=false`. |
| **Reddit** | ✅ (PRAW) | ✅ real | ✅ real (public comment reply) | Needs Reddit OAuth with post permission. |
| **ClawTasks** | ❌ **HTTP 500** | ✅ code real, but platform is **down** ("free-tasks-only hardening") | ✅ code real (claim stakes 10% USDC on Base) | Cannot transact until the platform recovers. |

## Hardening added this pass

- **BotBounty delivery** — the one integration that was discovery-only. Now claims via
  the bounty's `claimEndpoint` (carried through from ingest) and submits the solution.
- **Field-drift guard** — the shared bounty-board client now logs a WARNING when a
  bounty is missing its amount/title field (a renamed field would otherwise silently
  price the job at $0). Earliest signal that a field map needs updating.
- **Claim-endpoint passthrough** — ingest carries `claimEndpoint`/`submitEndpoint` from
  the bounty into the delivery contact, so delivery uses the platform's own URL rather
  than a guessed path when one is provided.

## What is genuinely NOT production-ready without per-platform testing

- **TaskBounty / StacksTasker submit paths** are undocumented guesses — they will 404
  if wrong. Verify against a real account/awarded task before going live.
- **BotBounty submit body** is inferred (bounties list was empty at audit time). Confirm
  the claim/submit contract on a live bounty.
- **ClawTasks** is down; nothing transacts until it recovers.

Ingest is solid across live platforms; the remaining risk is entirely in the
submit/claim contracts, which can only be confirmed with a funded account on each
platform.
