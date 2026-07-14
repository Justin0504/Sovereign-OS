"""
The CEO's task-selection brain: decide *which* jobs to take, on expected value.

`economics.evaluate_opportunity` answers "does this task's payout beat its cost?".
That's necessary but not how a top-tier operator chooses work. Two tasks with the
same nominal margin are not equal: one is in a category we deliver reliably, on a
cheap fast rail; the other is work we often fail, on a chain with high gas. A real
CEO ranks by **expected value** — the payout weighted by our probability of actually
delivering it, net of that platform's real settlement economics.

This module composes three signals into one decision:

  1. Platform economics — each rail's true settlement fee, gas, and currency, so the
     net payout is computed correctly per platform (x402/Base, Stacks, Stripe, ...).
  2. Success probability — our track record in the task's category (audit pass/fail
     counts), Beta-smoothed so a new category starts from a sensible prior and moves
     with evidence. This is where *delivery quality* feeds *task selection*.
  3. Cost — the fully-loaded LLM estimate from `economics`.

Expected value = P(success) · (payout − fee − gas) − LLM cost. Take the task only
when EV is positive AND the success-case margin clears the floor. Pure and
deterministic — safe to run in the ingest hot path and fully unit-testable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from sovereign_os.governance.economics import complexity_from_goal, estimate_task_cost_cents


@dataclass(frozen=True)
class PlatformEconomics:
    """Settlement economics for one platform/rail."""

    fee_ratio: float      # proportional settlement fee (e.g. 0.029 for a 2.9% rail)
    gas_cents: int        # fixed on-chain/network cost paid to settle a reward
    currency: str         # payout currency
    network: str          # settlement network


# Best-effort 2026 defaults. Crypto rails on Base/USDC are cheap; fiat rails carry a
# percentage + fixed fee; Stacks gas runs a touch higher. Tune per deployment via
# SOVEREIGN_PLATFORM_ECON_JSON (see platform_economics()).
_PLATFORM_ECONOMICS: dict[str, PlatformEconomics] = {
    "apb":          PlatformEconomics(0.00, 5, "USDC", "base"),      # x402 on Base
    "x402":         PlatformEconomics(0.00, 5, "USDC", "base"),
    "clawtasks":    PlatformEconomics(0.01, 5, "USDC", "base"),
    "taskbounty":   PlatformEconomics(0.00, 5, "USDC", "base"),
    "stackstasker": PlatformEconomics(0.00, 10, "STX", "stacks"),
    "rentahuman":   PlatformEconomics(0.029, 30, "USD", "stripe"),   # fiat escrow
    "stripe":       PlatformEconomics(0.029, 30, "USD", "stripe"),
    "reddit":       PlatformEconomics(0.00, 0, "USD", "offchain"),
    "default":      PlatformEconomics(0.00, 0, "USD", "default"),
}


def platform_economics(platform: str | None) -> PlatformEconomics:
    """
    Economics for a platform, with env override. `SOVEREIGN_PLATFORM_ECON_JSON` may
    map platform -> {fee_ratio, gas_cents, currency, network} to correct/extend the
    built-in table without a code change. Unknown platforms fall back to `default`.
    """
    key = (platform or "default").strip().lower()
    table = dict(_PLATFORM_ECONOMICS)
    raw = os.getenv("SOVEREIGN_PLATFORM_ECON_JSON")
    if raw:
        try:
            for k, v in (json.loads(raw) or {}).items():
                base = table.get(k.lower()) or table["default"]
                table[k.lower()] = PlatformEconomics(
                    fee_ratio=float(v.get("fee_ratio", base.fee_ratio)),
                    gas_cents=int(v.get("gas_cents", base.gas_cents)),
                    currency=str(v.get("currency", base.currency)),
                    network=str(v.get("network", base.network)),
                )
        except (ValueError, AttributeError, TypeError):
            pass
    return table.get(key, table["default"])


def success_probability(
    successes: int, failures: int, *, prior_mean: float = 0.7, prior_strength: float = 4.0
) -> float:
    """
    Beta-smoothed probability of delivering a task well, from audit pass/fail counts.

    Posterior mean of Beta(α0+s, β0+f) with the prior expressed as (mean, strength):
    α0 = mean·strength, β0 = (1−mean)·strength. With no history the estimate is the
    prior; each pass/fail nudges it. `prior_mean` is a modest optimism (we assume we
    can usually deliver); `prior_strength` is how much evidence it takes to move.
    """
    s = max(0, int(successes))
    f = max(0, int(failures))
    m = min(1.0, max(0.0, prior_mean))
    k = max(0.0, prior_strength)
    alpha = m * k + s
    beta = (1.0 - m) * k + f
    denom = alpha + beta
    return alpha / denom if denom > 0 else m


@dataclass
class OpportunityScore:
    """CEO verdict for one candidate job."""

    take: bool
    expected_value_cents: float   # P(success)·(payout−fee−gas) − LLM cost
    success_prob: float
    net_margin_cents: int         # success-case margin (payout − fee − gas − LLM cost)
    est_cost_cents: int
    fee_cents: int
    gas_cents: int
    platform: str
    currency: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "take": self.take,
            "expected_value_cents": round(self.expected_value_cents, 2),
            "success_prob": round(self.success_prob, 4),
            "net_margin_cents": self.net_margin_cents,
            "est_cost_cents": self.est_cost_cents,
            "fee_cents": self.fee_cents,
            "gas_cents": self.gas_cents,
            "platform": self.platform,
            "currency": self.currency,
            "reason": self.reason,
        }


def score_opportunity(
    revenue_cents: int,
    est_cost_cents: int,
    success_prob: float,
    *,
    fee_ratio: float = 0.0,
    gas_cents: int = 0,
    margin_floor: float = 0.0,
    platform: str = "default",
    currency: str = "USD",
    ev_multiplier: float = 1.0,
) -> OpportunityScore:
    """
    Expected-value verdict. Take iff EV > 0 AND the success-case net margin clears the
    floor (so we never chase a lottery whose *best* case is still a thin/negative deal).
    `ev_multiplier` (from the reward system's realized lane yield) scales the reported
    EV so proven-profitable lanes rank higher in the portfolio; it never flips a job's
    take/skip sign, only its priority.
    """
    revenue_cents = max(0, int(revenue_cents))
    est_cost_cents = max(0, int(est_cost_cents))
    p = min(1.0, max(0.0, float(success_prob)))
    fee_cents = int(round(revenue_cents * min(1.0, max(0.0, fee_ratio))))
    gas_cents = max(0, int(gas_cents))
    payout_if_success = revenue_cents - fee_cents - gas_cents
    net_margin = payout_if_success - est_cost_cents            # success-case margin
    ev = (p * payout_if_success - est_cost_cents) * max(0.0, float(ev_multiplier))  # LLM cost paid either way

    if revenue_cents <= 0:
        take = margin_floor <= 0
        reason = "unpaid task (EV n/a)" + ("" if take else " skipped (floor set)")
        return OpportunityScore(take, ev, p, net_margin, est_cost_cents, fee_cents,
                                gas_cents, platform, currency, reason)

    required = revenue_cents * margin_floor
    if net_margin < required or net_margin <= 0:
        take, reason = False, (
            f"success-case margin {net_margin}¢ below floor {required:.0f}¢ "
            f"({margin_floor*100:.0f}%)"
        )
    elif ev <= 0:
        take, reason = False, (
            f"negative EV {ev:.0f}¢: p={p:.2f}·(payout {payout_if_success}¢) "
            f"< LLM cost {est_cost_cents}¢ — success too unlikely"
        )
    else:
        take, reason = True, (
            f"take: EV {ev:.0f}¢ (p={p:.2f}, net {net_margin}¢ after fee {fee_cents}¢ "
            f"+ gas {gas_cents}¢ + LLM {est_cost_cents}¢ on {platform})"
        )
    return OpportunityScore(take, ev, p, net_margin, est_cost_cents, fee_cents,
                            gas_cents, platform, currency, reason)


def evaluate_job(
    revenue_cents: int,
    goal: str,
    category: str,
    *,
    platform: str | None = None,
    successes: int = 0,
    failures: int = 0,
    model: str = "gpt-4o",
    margin_floor: float | None = None,
    ev_multiplier: float | None = None,
) -> OpportunityScore:
    """
    Full CEO decision for a candidate job: platform economics + cost estimate +
    track-record success probability -> expected-value verdict. `margin_floor`
    defaults from SOVEREIGN_MIN_MARGIN_RATIO. `ev_multiplier` defaults to the reward
    system's realized yield for this category×platform lane (proven lanes rank higher).
    """
    econ = platform_economics(platform)
    if margin_floor is None:
        try:
            margin_floor = float((os.getenv("SOVEREIGN_MIN_MARGIN_RATIO") or "").strip() or 0.0)
        except ValueError:
            margin_floor = 0.0
    if ev_multiplier is None:
        try:
            from sovereign_os.governance.portfolio import lane_multiplier

            ev_multiplier = lane_multiplier(category, platform)
        except Exception:  # noqa: BLE001 - reward loop is best-effort
            ev_multiplier = 1.0
    est = estimate_task_cost_cents(category, model, complexity=complexity_from_goal(goal))
    p = success_probability(successes, failures)
    return score_opportunity(
        revenue_cents, est, p,
        fee_ratio=econ.fee_ratio, gas_cents=econ.gas_cents, margin_floor=margin_floor,
        platform=(platform or "default"), currency=econ.currency, ev_multiplier=ev_multiplier,
    )
