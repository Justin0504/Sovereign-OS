"""
Ruin analysis: does the measured cost miscalibration bankrupt an EV-maximizing agent?

An agent that takes paid work is not an optimizer with a utility function; it is a
gambler with a finite bankroll, an absorbing barrier at zero, and beliefs that are
measurably wrong. Expected-value maximization is the right objective for a player with
unlimited capital and unlimited repetitions. It says nothing about ruin, and an agent
that hits zero does not get to collect its expectation.

The specific danger is not that the cost estimate is noisy but that its error has a
*direction*, measured in `bench.calibration_run`:

    small tasks   realized/predicted = 0.55   cost over-estimated  ~1.8x
    large tasks   realized/predicted = 1.66   cost under-estimated ~1.7x

Put that through `EV = p*payout - cost_estimate` and the screen inverts: large jobs look
cheaper than they are, so their EV is inflated and they are accepted; small jobs look
dearer, so their EV is deflated and they are skipped. The agent systematically takes the
losers and declines the winners. Losses concentrate in exactly the tasks where the most
money is at stake, which is not an error that averages away.

What is empirical here and what is assumed
------------------------------------------
**Empirical**: the cost model. Per-tier multiplicative bias and log-space spread are the
values measured over 49 governed runs on a live model, three repeats.

**Assumed**: payouts and success probability. The system has never earned on a live
marketplace, so there is no payout trace to replay; these are stated parameters, and
`sweep_assumptions()` exists so a conclusion that depends on a particular guess can be
identified as such. The question asked here — does the *measured* miscalibration cause
ruin — is answerable while holding the payout model fixed, and the sweep shows over what
range the answer holds.

Nothing here is a claim about live earnings.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# Measured in bench/calibration_run.py: geometric-mean realized/predicted per tier, and
# the log-space spread of the raw estimator across the suite.
MEASURED_BIAS = {"small": 0.48, "medium": 0.90, "large": 1.66}
MEASURED_SPREAD_LOG = 0.700
"""Residual sd of log(realized/predicted) with no correction applied. This is the number
that should set how far below full Kelly a policy sizes: fractional Kelly exists because
edge estimates are uncertain, and this is that uncertainty, measured rather than guessed.

Pooled over all 49 usable samples in `data/calibration_*.jsonl`. The earlier values here
(0.601, and medium 0.97) were taken from one run of 17 while the paper described them as
coming from 49 -- and 0.97 matched no run at all, the three giving 0.84, 0.85 and 1.06.
`tests/test_ruin_constants.py` recomputes all of these from the committed data, so the
constants and the sample they claim to come from cannot drift apart again."""

CALIBRATED_SPREAD_LOG = 0.465
"""Residual after the best per-tier correction — the spread a well-calibrated version of
this system would still face."""


@dataclass
class Job:
    tier: str
    payout_cents: float
    true_cost_cents: float
    estimated_cost_cents: float
    success_prob: float


@dataclass
class MarketAssumptions:
    """The stated, non-empirical half of the model."""

    payout_mean_cents: float = 500.0
    payout_spread_log: float = 0.8
    success_prob: float = 0.85
    tier_mix: tuple[float, float, float] = (0.4, 0.35, 0.25)   # small, medium, large
    base_cost_cents: dict[str, float] = field(default_factory=lambda: {
        "small": 120.0, "medium": 260.0, "large": 620.0})


PERFECT_CALIBRATION = {"small": 1.0, "medium": 1.0, "large": 1.0}
"""Ablation: an estimator with the same noise but no directional bias. Comparing against
this is what separates "the measured miscalibration causes ruin" from "this market is
hard" — without it, a high ruin rate says nothing about calibration at all."""


def draw_job(rng: random.Random, m: MarketAssumptions, spread_log: float,
             bias: dict | None = None) -> Job:
    """
    One offered job. The estimate is what the system would predict; the true cost is drawn
    around it using the measured per-tier bias and spread, so the gap between the two is
    the empirical finding rather than an invention.
    """
    bias = MEASURED_BIAS if bias is None else bias
    tier = rng.choices(["small", "medium", "large"], weights=m.tier_mix)[0]
    estimated = m.base_cost_cents[tier]
    # realized = estimated * bias * lognormal(0, spread)
    true_cost = estimated * bias[tier] * math.exp(rng.gauss(0.0, spread_log))
    # Payout scales with task size — a large task pays more — times market noise.
    payout = (m.payout_mean_cents * (estimated / m.base_cost_cents["medium"])
              * math.exp(rng.gauss(0.0, m.payout_spread_log)))
    return Job(tier, max(1.0, payout), max(1.0, true_cost), estimated, m.success_prob)


# ------------------------------------------------------------------- policies

def policy_ev(job: Job, wealth: float, *, margin_floor: float = 0.0, **_) -> bool:
    """
    What the system deploys: accept iff expected profit clears a margin floor, computed
    from the POINT estimate. Risk-neutral, and blind to how much of the bankroll is at
    stake.
    """
    ev = job.success_prob * job.payout_cents - job.estimated_cost_cents
    return ev > margin_floor * job.payout_cents


def policy_ev_exposure_cap(job: Job, wealth: float, *, margin_floor: float = 0.0,
                           max_fraction: float = 0.1, **_) -> bool:
    """EV, plus the crudest survival rule: never stake more than a fixed share of bankroll."""
    if job.estimated_cost_cents > max_fraction * wealth:
        return False
    return policy_ev(job, wealth, margin_floor=margin_floor)


def policy_risk_adjusted(job: Job, wealth: float, *, spread_log: float = MEASURED_SPREAD_LOG,
                         quantile_z: float = 0.84, kelly_fraction: float | None = None,
                         **_) -> bool:
    """
    Size on what the job might actually cost, and cap exposure at a fraction derived from
    the measured spread rather than chosen.

    Two changes from `policy_ev`, and they are separable:

    1. The cost used for the decision is an upper quantile of the estimate's error
       distribution, not its point value. With a log-normal error of width `spread_log`,
       the q-quantile is `estimate * exp(z_q * spread)`. This is what stops a 1.66x
       under-estimate on large jobs from reading as profit.

    2. Exposure is capped at `kelly_fraction` of bankroll. Full Kelly is already the
       upper bound of sane leverage, and it assumes the edge is known; since the edge
       here is estimated with measured width `spread_log`, the fraction is shaded by
       `exp(-spread_log)` — more uncertainty, smaller bet. At the measured 0.601 this is
       roughly half-Kelly, which practitioners reach for by instinct; here it is derived.
    """
    worst_case_cost = job.estimated_cost_cents * math.exp(quantile_z * spread_log)
    ev = job.success_prob * job.payout_cents - worst_case_cost
    if ev <= 0:
        return False

    if kelly_fraction is None:
        b = max(1e-9, (job.payout_cents - worst_case_cost) / worst_case_cost)
        full_kelly = job.success_prob - (1 - job.success_prob) / b
        kelly_fraction = max(0.0, full_kelly) * math.exp(-spread_log)
    return worst_case_cost <= kelly_fraction * wealth


POLICIES = {
    "ev": policy_ev,
    "ev_margin_35": lambda j, w, **k: policy_ev(j, w, margin_floor=0.35),
    "ev_exposure_cap": policy_ev_exposure_cap,
    "risk_adjusted": policy_risk_adjusted,
}


# ------------------------------------------------------------------ simulation

@dataclass
class LifetimeResult:
    ruined: bool
    final_wealth: float
    tasks_taken: int
    tasks_offered: int
    steps_survived: int


def simulate_lifetime(policy, *, rng: random.Random, m: MarketAssumptions,
                      start_wealth: float = 10_000.0, horizon: int = 1000,
                      spread_log: float = MEASURED_SPREAD_LOG,
                      bias: dict | None = None, **kw) -> LifetimeResult:
    """
    One agent's life. Ruin is absorbing: once the bankroll cannot fund the work, the
    agent is done, and no later expectation redeems it. That asymmetry is the entire
    reason expected value is the wrong objective here.
    """
    wealth = start_wealth
    taken = offered = 0

    for step in range(horizon):
        job = draw_job(rng, m, spread_log, bias)
        offered += 1
        if not policy(job, wealth, spread_log=spread_log, **kw):
            continue
        if job.true_cost_cents > wealth:
            return LifetimeResult(True, 0.0, taken, offered, step)

        taken += 1
        wealth -= job.true_cost_cents
        if rng.random() < job.success_prob:
            wealth += job.payout_cents
        if wealth <= 0:
            return LifetimeResult(True, 0.0, taken, offered, step)

    return LifetimeResult(False, wealth, taken, offered, horizon)


def evaluate(policy_name: str, *, trials: int = 2000, seed: int = 20261012,
             m: MarketAssumptions | None = None, spread_log: float = MEASURED_SPREAD_LOG,
             start_wealth: float = 10_000.0, horizon: int = 1000,
             bias: dict | None = None, **kw) -> dict:
    """Run many lifetimes and report survival, not just average wealth."""
    policy = POLICIES[policy_name]
    m = m or MarketAssumptions()
    rng = random.Random(seed)

    results = [simulate_lifetime(policy, rng=rng, m=m, start_wealth=start_wealth,
                                 horizon=horizon, spread_log=spread_log, bias=bias, **kw)
               for _ in range(trials)]

    ruined = [r for r in results if r.ruined]
    survivors = [r for r in results if not r.ruined]
    finals = sorted(r.final_wealth for r in results)

    # Reported as the median rather than the mean: with an absorbing barrier the mean is
    # dominated by the few runs that compounded, and describes an outcome almost nobody
    # in the cohort experienced.
    median_final = finals[len(finals) // 2] if finals else 0.0
    growth = (math.log(median_final / start_wealth) / horizon) if median_final > 0 else float("-inf")

    return {
        "policy": policy_name,
        "ruin_rate": len(ruined) / trials,
        "median_final_wealth": round(median_final, 1),
        "median_growth_per_task": round(growth, 6),
        "acceptance_rate": round(
            sum(r.tasks_taken for r in results) / max(1, sum(r.tasks_offered for r in results)), 4),
        "median_steps_to_ruin": (
            sorted(r.steps_survived for r in ruined)[len(ruined) // 2] if ruined else None),
        "survivors": len(survivors),
        "trials": trials,
    }


def compare(**kw) -> list[dict]:
    return [evaluate(name, **kw) for name in POLICIES]


def sweep_assumptions(policy_name: str = "ev", *, trials: int = 600, **kw) -> list[dict]:
    """
    Vary the assumed half of the model. A conclusion that survives the sweep is about the
    measured miscalibration; one that does not is about the guess.
    """
    out = []
    for payout in (300.0, 500.0, 800.0, 1200.0):
        for p_success in (0.7, 0.85, 0.95):
            m = MarketAssumptions(payout_mean_cents=payout, success_prob=p_success)
            r = evaluate(policy_name, trials=trials, m=m, **kw)
            out.append({"payout_mean_cents": payout, "success_prob": p_success,
                        "ruin_rate": r["ruin_rate"],
                        "median_final_wealth": r["median_final_wealth"]})
    return out


def find_margin_frontier(*, trials: int = 500, horizon: int = 400,
                         policy_name: str = "ev") -> list[dict]:
    """
    Sweep the market from generous to thin and report where ruin appears, with and
    without the measured bias.

    The first run of this analysis used a single generous payout assumption and every
    policy survived — it was asking whether a profitable market is profitable. The
    informative regime is the thin-margin one the system is actually designed for
    ("earn the most at the thinnest margin"), and the honest way to find it is to sweep
    rather than to pick a number that produces the expected answer.
    """
    rows = []
    for payout in (150.0, 200.0, 250.0, 300.0, 400.0, 600.0):
        m = MarketAssumptions(payout_mean_cents=payout)
        biased = evaluate(policy_name, trials=trials, horizon=horizon, m=m)
        clean = evaluate(policy_name, trials=trials, horizon=horizon, m=m,
                         bias=PERFECT_CALIBRATION)
        rows.append({
            "payout_mean_cents": payout,
            "ruin_measured_bias": biased["ruin_rate"],
            "ruin_perfect_calibration": clean["ruin_rate"],
            "median_final_biased": biased["median_final_wealth"],
            "median_final_clean": clean["median_final_wealth"],
        })
    return rows


def _paper_tables(trials: int = 4000) -> int:
    """
    Reproduce section 7.5 exactly.

    This exists because there was no command that did. The CLI default is the generous
    regime (500c payouts against ~260c costs), where nothing can lose and every policy
    reports ~0.1% -- which is the uninformative run the paper describes rejecting. A
    reader following the paper got that, and would have concluded the reported 9.3% was
    unsupported.

    4000 trials rather than the CLI default because the safe policy's ruin rate sits near
    0.1%: at 2000 trials the estimator's resolution is 0.05% and the figure moves with the
    seed, which is not a number to quote to one decimal place in a paper. `evaluate` is
    seeded, so this output is byte-stable.
    """
    thin = MarketAssumptions(payout_mean_cents=150.0)
    print(f"section 7.5, thin margins (payout 150c vs 260c medium cost), {trials} trials\n")

    print("Table: ruin rate by horizon")
    print(f"  {'horizon':>8}{'EV (measured bias)':>21}{'EV (ablated)':>15}{'risk-adjusted':>16}")
    for h in (250, 500, 1000, 2000, 4000):
        biased = evaluate("ev", trials=trials, horizon=h, m=thin)["ruin_rate"]
        clean = evaluate("ev", trials=trials, horizon=h, m=thin,
                         bias=PERFECT_CALIBRATION)["ruin_rate"]
        safe = evaluate("risk_adjusted", trials=trials, horizon=h, m=thin)["ruin_rate"]
        print(f"  {h:>8}{biased:>20.1%}{clean:>15.1%}{safe:>16.1%}")

    print("\nTable: policy comparison at horizon 1000")
    print(f"  {'policy':16}{'accepted':>10}{'median final wealth':>22}{'ruin':>8}")
    for name in ("ev", "risk_adjusted"):
        r = evaluate(name, trials=trials, horizon=1000, m=thin)
        print(f"  {name:16}{r['acceptance_rate']:>10.1%}"
              f"{r['median_final_wealth']:>22,.0f}{r['ruin_rate']:>8.1%}")
    return 0


def main() -> int:  # pragma: no cover - CLI
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--horizon", type=int, default=1000)
    # The paper's §7.5 figures are the thin-margin regime (--payout 150), which is what
    # the system is designed for. Without this flag there was no documented way to
    # reproduce them: the default 500c against ~260c costs is the generous regime where
    # nothing can lose, every policy reports ~0.1%, and the experiment is asking whether
    # a profitable market is profitable. That is the uninformative run the paper
    # describes rejecting, and it was the only one the CLI could produce.
    parser.add_argument("--payout", type=float, default=MarketAssumptions.payout_mean_cents,
                        help="mean payout in cents; 150 is the thin-margin regime the "
                             "paper reports, 500 (default) is the generous one")
    parser.add_argument("--paper", action="store_true",
                        help="reproduce the two tables in the paper's section 7.5 "
                             "(thin margins, 4000 trials) and exit")
    parser.add_argument("--ablate-bias", action="store_true",
                        help="run with the measured directional bias removed -- the "
                             "control that makes a ruin rate a finding about the "
                             "estimator rather than about the assumed market")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--frontier", action="store_true",
                        help="sweep market thinness, with and without the measured bias")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if args.paper:
        return _paper_tables()

    market = MarketAssumptions(payout_mean_cents=args.payout)
    kw = {"trials": args.trials, "horizon": args.horizon, "m": market}
    if args.ablate_bias:
        kw["bias"] = PERFECT_CALIBRATION
    rows = compare(**kw)

    margin = args.payout / market.base_cost_cents["medium"]
    regime = "thin" if margin < 1.5 else "generous"
    print(f"market: payout {args.payout:.0f}c vs medium-task cost "
          f"{market.base_cost_cents['medium']:.0f}c  ({margin:.2f}x, {regime})"
          f"{'   [bias ABLATED]' if args.ablate_bias else ''}")
    if regime == "generous" and not args.ablate_bias:
        print("note: at this margin almost nothing can lose. The paper reports "
              "--payout 150; see §7.5.")
    print(f"{'policy':18}{'ruin':>8}{'median final':>14}{'accept':>9}{'to ruin':>9}")
    for r in rows:
        print(f"{r['policy']:18}{r['ruin_rate']:>7.1%}{r['median_final_wealth']:>14,.0f}"
              f"{r['acceptance_rate']:>9.1%}"
              f"{(str(r['median_steps_to_ruin']) if r['median_steps_to_ruin'] else '—'):>9}")

    payload = {"comparison": rows}
    if args.sweep:
        payload["sweep"] = sweep_assumptions(trials=400, horizon=args.horizon)
        print("\nassumption sweep (policy=ev): ruin rate by payout x success")
        for s in payload["sweep"]:
            print(f"  payout {s['payout_mean_cents']:>6.0f}c  p={s['success_prob']:.2f}"
                  f"  ruin {s['ruin_rate']:.1%}")

    if args.frontier:
        payload["frontier"] = find_margin_frontier(horizon=args.horizon)
        print("\n  market thinness: ruin rate under EV, measured bias vs perfect calibration")
        print(f"  {'payout':>8}{'measured':>11}{'perfect':>10}{'wealth (biased)':>18}{'(clean)':>12}")
        for f in payload["frontier"]:
            print(f"  {f['payout_mean_cents']:>8.0f}{f['ruin_measured_bias']:>10.1%}"
                  f"{f['ruin_perfect_calibration']:>10.1%}"
                  f"{f['median_final_biased']:>18,.0f}{f['median_final_clean']:>12,.0f}")

    if args.out:
        from pathlib import Path

        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
