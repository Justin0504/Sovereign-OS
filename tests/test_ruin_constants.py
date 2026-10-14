"""
The constants in `bench.ruin` must match the data they claim to come from.

They did not. `MEASURED_BIAS` and `MEASURED_SPREAD_LOG` were taken from a single run of
17 samples while the paper described them as measured "across 49 governed runs", and the
medium-tier value (0.97) matched no run at all -- the three runs give 0.84, 0.85 and 1.06,
and the pooled figure is 0.90. Nothing caught it because the constants were literals and
the data was a file nobody re-read.

The tolerance is the rounding in the literal, not a fudge factor: these are quoted to two
decimal places in the paper, so anything beyond that is a real disagreement.
"""

import json
import math
import statistics as st
from pathlib import Path

import pytest

from sovereign_os.bench.ruin import (
    CALIBRATED_SPREAD_LOG,
    MEASURED_BIAS,
    MEASURED_SPREAD_LOG,
)

DATA = Path(__file__).resolve().parent.parent / "data"
RUNS = ("calibration_full.jsonl", "calibration_run2.jsonl", "calibration_run3.jsonl")


def _samples():
    rows = []
    for name in RUNS:
        path = DATA / name
        if not path.exists():
            pytest.skip(f"calibration data not present: {name}")
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows
            if r.get("ok") and r.get("estimated_cents") and r.get("actual_cents")]


def _log_ratios(rows):
    return [math.log(r["actual_cents"] / r["estimated_cents"]) for r in rows]


def test_the_sample_is_the_one_the_paper_claims():
    assert len(_samples()) == 49, "the paper says 49 governed runs"


def test_measured_bias_matches_the_pooled_data():
    rows = _samples()
    for tier, stated in MEASURED_BIAS.items():
        tier_rows = [r for r in rows if r.get("tier") == tier]
        assert tier_rows, f"no samples for tier {tier}"
        logs = _log_ratios(tier_rows)
        actual = math.exp(sum(logs) / len(logs))
        assert actual == pytest.approx(stated, abs=0.005), (
            f"{tier}: constant says {stated}, the 49 samples say {actual:.3f}")


def test_the_direction_that_the_whole_ruin_argument_rests_on():
    """Small over-estimated, large under-estimated. If this flips, §7.5 is about
    something else entirely and the constants are not the place to discover it."""
    assert MEASURED_BIAS["small"] < 1.0 < MEASURED_BIAS["large"]
    assert MEASURED_BIAS["small"] < MEASURED_BIAS["medium"] < MEASURED_BIAS["large"]


def test_spread_matches_the_pooled_data():
    actual = st.pstdev(_log_ratios(_samples()))
    assert actual == pytest.approx(MEASURED_SPREAD_LOG, abs=0.005), (
        f"constant says {MEASURED_SPREAD_LOG}, the 49 samples say {actual:.3f}")


def test_calibrated_spread_is_the_residual_after_the_per_tier_correction():
    rows = _samples()
    means = {}
    for tier in set(r["tier"] for r in rows):
        logs = _log_ratios([r for r in rows if r["tier"] == tier])
        means[tier] = sum(logs) / len(logs)
    residual = [math.log(r["actual_cents"] / r["estimated_cents"]) - means[r["tier"]]
                for r in rows]
    assert st.pstdev(residual) == pytest.approx(CALIBRATED_SPREAD_LOG, abs=0.005)


def test_a_per_tier_correction_cannot_remove_the_spread():
    """The paper's claim that one multiplier per category shifts the line without
    changing its slope: the residual stays large after the best per-tier correction."""
    assert CALIBRATED_SPREAD_LOG > 0.3
