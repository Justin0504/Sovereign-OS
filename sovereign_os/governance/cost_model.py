"""
Cost calibration: replace guessed compute costs with what jobs actually cost.

`economics.estimate_task_cost_cents` starts from a hand-set token budget per category.
That's a fine cold-start prior, but real cost drifts — a category may use a pricier
model, longer contexts, or more tool rounds than assumed. Every mispriced estimate
poisons the whole money stack: EV selection, bid floors, and profit attribution.

So we close the loop with evidence. As jobs settle, the ledger knows their real token
cost; we compare it to the raw heuristic estimate and learn a per-category correction
factor (smoothed actual ÷ estimated, pulled toward 1.0 until there's evidence, bounded
so a couple of outliers can't blow it up). `estimate_task_cost_cents` then multiplies
the heuristic by that factor, so estimates converge on reality per category.

Pure and deterministic; the calibrator is a process-global so the web layer records
actuals on completion and the estimator reads factors. With no history the factor is
1.0 — a no-op that preserves the cold-start behavior.

Beyond the point correction, the calibrator retains the individual (estimate, actual)
pairs so the *shape* of the estimator's error can be measured, not just its mean. Two
properties of the original design motivate this:

**The pooled factor is value-weighted.** `sum(actual) / sum(estimated)` weights every
job by its size, so one expensive overrun outweighs many accurate cheap jobs. That is
the right number for attributing a portfolio's total spend, and the wrong one for
budgeting the *next single task*, where the typical job matters. Job cost is heavy
tailed, so the two diverge; `CalibrationStats.divergence` reports by how much.

**A mean correction cannot price risk.** If estimates are off by a factor of three in
either direction, centering them still leaves half the tasks over their ceiling. Sizing
a budget needs a quantile, which needs the distribution — hence `safety_multiplier`.

Errors here are multiplicative, so the statistics are computed on log ratios: a 2x
under-estimate and a 2x over-estimate are then symmetric (+0.69 / -0.69), where in raw
ratio space they would be (+1.0 / -0.5) and would bias every average toward overrun.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list. q in [0, 1]."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = max(0.0, min(1.0, q)) * (len(sorted_values) - 1)
    lo_i = int(math.floor(pos))
    hi_i = int(math.ceil(pos))
    if lo_i == hi_i:
        return sorted_values[lo_i]
    frac = pos - lo_i
    return sorted_values[lo_i] * (1 - frac) + sorted_values[hi_i] * frac


@dataclass(frozen=True)
class CalibrationSample:
    """One settled job's estimate against what it actually cost."""

    category: str
    estimated_cents: float
    actual_cents: float
    model: str = ""
    complexity: float = 1.0
    ts: float = 0.0

    @property
    def ratio(self) -> float:
        """actual ÷ estimated. >1 means the estimate was too low."""
        return self.actual_cents / self.estimated_cents

    @property
    def log_ratio(self) -> float:
        return math.log(self.ratio)


@dataclass(frozen=True)
class CalibrationStats:
    """
    The shape of the estimator's error for one stratum, in log space.

    `bias_log` is the systematic component (0.0 = unbiased); `spread_log` is the
    irreducible-looking scatter that a point correction cannot remove. The pair is what
    decides whether to fix the estimator or to budget a margin around it.
    """

    n: int
    bias_log: float = 0.0
    spread_log: float = 0.0
    geometric_mean_ratio: float = 1.0
    """exp(bias_log): the typical multiplicative error, unweighted by job size."""

    pooled_ratio: float = 1.0
    """sum(actual)/sum(estimated): the value-weighted error the deployed factor uses."""

    median_ratio: float = 1.0
    p80_ratio: float = 1.0
    p95_ratio: float = 1.0
    under_rate: float = 0.0
    """Fraction of jobs that cost more than estimated — the overrun rate."""

    within_2x: float = 1.0
    """Fraction landing within a factor of two of the estimate. A crude but honest
    accuracy headline."""

    median_abs_log_error: float = 0.0

    @property
    def divergence(self) -> float:
        """
        How far the value-weighted correction sits from the typical-job correction,
        as a ratio. 1.0 means they agree; far from 1.0 means the deployed factor is
        being set by a few large jobs and misprices the median task.
        """
        if self.geometric_mean_ratio <= 0:
            return 1.0
        return self.pooled_ratio / self.geometric_mean_ratio

    def safety_multiplier(self, quantile: float = 0.8) -> float:
        """
        Multiplier that would have covered `quantile` of observed jobs — what to size a
        ceiling by when the cost of an overrun exceeds the cost of reserving headroom.
        Falls back to the central correction when there is no distribution to speak of.
        """
        if self.n < 3:
            return self.geometric_mean_ratio
        if quantile >= 0.95:
            return self.p95_ratio
        if quantile >= 0.8:
            return self.p80_ratio
        return self.median_ratio

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "bias_log": round(self.bias_log, 4),
            "spread_log": round(self.spread_log, 4),
            "geometric_mean_ratio": round(self.geometric_mean_ratio, 4),
            "pooled_ratio": round(self.pooled_ratio, 4),
            "divergence": round(self.divergence, 4),
            "median_ratio": round(self.median_ratio, 4),
            "p80_ratio": round(self.p80_ratio, 4),
            "p95_ratio": round(self.p95_ratio, 4),
            "under_rate": round(self.under_rate, 4),
            "within_2x": round(self.within_2x, 4),
            "median_abs_log_error": round(self.median_abs_log_error, 4),
        }


def compute_stats(samples: list[CalibrationSample]) -> CalibrationStats:
    """Summarize a stratum's estimate-vs-actual error. Pure; no history required."""
    usable = [s for s in samples if s.estimated_cents > 0 and s.actual_cents > 0]
    n = len(usable)
    if n == 0:
        return CalibrationStats(n=0)

    logs = sorted(s.log_ratio for s in usable)
    ratios = sorted(s.ratio for s in usable)
    mean_log = sum(logs) / n
    variance = sum((x - mean_log) ** 2 for x in logs) / n if n > 1 else 0.0
    abs_logs = sorted(abs(x) for x in logs)

    est_total = sum(s.estimated_cents for s in usable)
    act_total = sum(s.actual_cents for s in usable)

    return CalibrationStats(
        n=n,
        bias_log=mean_log,
        spread_log=math.sqrt(variance),
        geometric_mean_ratio=math.exp(mean_log),
        pooled_ratio=(act_total / est_total) if est_total > 0 else 1.0,
        median_ratio=_percentile(ratios, 0.5),
        p80_ratio=_percentile(ratios, 0.8),
        p95_ratio=_percentile(ratios, 0.95),
        under_rate=sum(1 for s in usable if s.actual_cents > s.estimated_cents) / n,
        within_2x=sum(1 for x in logs if abs(x) <= math.log(2)) / n,
        median_abs_log_error=_percentile(abs_logs, 0.5),
    )


@dataclass
class CostCalibrator:
    """Learns a per-category (actual ÷ estimated) compute-cost correction."""

    prior_samples: float = 3.0     # pseudo-jobs at ratio 1.0 pulling the factor toward 1.0
    lo: float = 0.25               # clamp: never trust the correction beyond 4x either way
    hi: float = 4.0
    max_samples: int = 500         # per-category ring buffer for distribution statistics
    _est: dict[str, float] = field(default_factory=dict)
    _act: dict[str, float] = field(default_factory=dict)
    _n: dict[str, int] = field(default_factory=dict)
    _samples: dict[str, list[CalibrationSample]] = field(default_factory=dict)

    def record(
        self,
        category: str,
        estimated_cents: float,
        actual_cents: float,
        *,
        model: str = "",
        complexity: float = 1.0,
        ts: float | None = None,
    ) -> None:
        c = (category or "general").lower()
        self._est[c] = self._est.get(c, 0.0) + max(0.0, float(estimated_cents))
        self._act[c] = self._act.get(c, 0.0) + max(0.0, float(actual_cents))
        self._n[c] = self._n.get(c, 0) + 1

        # Retain the pair itself so the error distribution can be measured. Only
        # positive-on-both-sides pairs carry a usable ratio; the running sums above
        # still see everything, so `factor()` is unchanged.
        if estimated_cents > 0 and actual_cents > 0:
            bucket = self._samples.setdefault(c, [])
            bucket.append(CalibrationSample(
                category=c,
                estimated_cents=float(estimated_cents),
                actual_cents=float(actual_cents),
                model=model,
                complexity=float(complexity),
                ts=time.time() if ts is None else float(ts),
            ))
            if len(bucket) > self.max_samples:
                del bucket[: len(bucket) - self.max_samples]

    def stats(self, category: str | None = None) -> CalibrationStats:
        """
        Error-distribution statistics for one category, or pooled across all when
        `category` is None.
        """
        if category is None:
            everything: list[CalibrationSample] = []
            for bucket in self._samples.values():
                everything.extend(bucket)
            return compute_stats(everything)
        return compute_stats(self._samples.get((category or "general").lower(), []))

    def samples_for(self, category: str) -> list[CalibrationSample]:
        """The retained pairs for a category (most recent last)."""
        return list(self._samples.get((category or "general").lower(), []))

    def calibration_report(self) -> dict:
        """
        Per-category error shape plus the deployed factor, so the two can be compared
        directly. `divergence` far from 1.0 flags a category whose correction is being
        set by a few large jobs.
        """
        cats = sorted(self._samples)
        return {
            "overall": self.stats(None).as_dict(),
            "by_category": {
                c: {**self.stats(c).as_dict(), "deployed_factor": self.factor(c)}
                for c in cats
            },
        }

    def factor(self, category: str) -> float:
        """
        Correction multiplier for a category's heuristic estimate: a pseudo-count-
        smoothed actual÷estimated ratio. `prior_samples` pseudo-jobs at ratio 1.0 pull
        it toward no-op, so it moves at a rate set by evidence volume and is scale-
        independent (works the same for $0.02 and $2 categories). Bounded to [lo, hi];
        no history -> exactly 1.0.
        """
        c = (category or "general").lower()
        est = self._est.get(c, 0.0)
        n = self._n.get(c, 0)
        if est <= 0 or n == 0:
            return 1.0
        ratio = self._act.get(c, 0.0) / est
        f = (self.prior_samples * 1.0 + n * ratio) / (self.prior_samples + n)
        return round(min(self.hi, max(self.lo, f)), 4)

    def samples(self, category: str) -> int:
        return self._n.get((category or "general").lower(), 0)

    def snapshot(self) -> dict:
        cats = sorted(set(self._est) | set(self._act))
        return {
            c: {
                "estimated_cents": round(self._est.get(c, 0.0), 2),
                "actual_cents": round(self._act.get(c, 0.0), 2),
                "samples": self._n.get(c, 0),
                "factor": self.factor(c),
            }
            for c in cats
        }

    def reset(self) -> None:
        self._est.clear()
        self._act.clear()
        self._n.clear()
        self._samples.clear()


# Process-global calibrator: the web layer records (estimate, actual) on job
# completion; the estimator reads factors during selection/bidding.
_CAL = CostCalibrator()


def record_cost(
    category: str,
    estimated_cents: float,
    actual_cents: float,
    *,
    model: str = "",
    complexity: float = 1.0,
) -> None:
    _CAL.record(category, estimated_cents, actual_cents, model=model, complexity=complexity)


def cost_factor(category: str) -> float:
    return _CAL.factor(category)


def cost_snapshot() -> dict:
    return _CAL.snapshot()


def cost_stats(category: str | None = None) -> CalibrationStats:
    """Error-distribution statistics for a category, or pooled when None."""
    return _CAL.stats(category)


def calibration_report() -> dict:
    """Per-category error shape alongside the factor actually deployed."""
    return _CAL.calibration_report()


def safety_multiplier(category: str, quantile: float = 0.8) -> float:
    """
    Multiplier that would have covered `quantile` of this category's jobs. Use where an
    overrun costs more than reserved headroom — sizing a ceiling rather than pricing a bid.
    """
    return _CAL.stats(category).safety_multiplier(quantile)


def reset_cost() -> None:
    _CAL.reset()
