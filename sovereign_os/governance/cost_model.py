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
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CostCalibrator:
    """Learns a per-category (actual ÷ estimated) compute-cost correction."""

    prior_samples: float = 3.0     # pseudo-jobs at ratio 1.0 pulling the factor toward 1.0
    lo: float = 0.25               # clamp: never trust the correction beyond 4x either way
    hi: float = 4.0
    _est: dict[str, float] = field(default_factory=dict)
    _act: dict[str, float] = field(default_factory=dict)
    _n: dict[str, int] = field(default_factory=dict)

    def record(self, category: str, estimated_cents: float, actual_cents: float) -> None:
        c = (category or "general").lower()
        self._est[c] = self._est.get(c, 0.0) + max(0.0, float(estimated_cents))
        self._act[c] = self._act.get(c, 0.0) + max(0.0, float(actual_cents))
        self._n[c] = self._n.get(c, 0) + 1

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


# Process-global calibrator: the web layer records (estimate, actual) on job
# completion; the estimator reads factors during selection/bidding.
_CAL = CostCalibrator()


def record_cost(category: str, estimated_cents: float, actual_cents: float) -> None:
    _CAL.record(category, estimated_cents, actual_cents)


def cost_factor(category: str) -> float:
    return _CAL.factor(category)


def cost_snapshot() -> dict:
    return _CAL.snapshot()


def reset_cost() -> None:
    _CAL.reset()
