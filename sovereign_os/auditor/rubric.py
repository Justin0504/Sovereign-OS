"""
Per-category audit rubrics + positional-bias mitigation for the Judge LLM.

Two evidence-backed refinements over a single fixed four-criteria rubric:

1. Category-specific analytic rubrics. Analytic (criterion-by-criterion) rubrics
   are more reliable and diagnosable than one holistic score, and the *right*
   criteria differ by work type — a coding deliverable must compile and be correct,
   an article must be well-structured and on-voice. Each category therefore gets a
   tuned criteria set instead of the generic relevance/completeness/correctness/safety.

2. Rubric-order shuffling. LLM judges are sensitive to the order criteria are
   presented (positional bias). We deterministically permute the criteria order per
   task (seeded by task_id, so a given task always shuffles the same way and tests
   stay stable) — spreading any positional weight across criteria rather than always
   privileging the first-listed one.

Each criterion is (key, description). `safety` is appended to every rubric so a
harmful deliverable can always be failed regardless of category.
"""

from __future__ import annotations

import hashlib

# Category key -> ordered analytic criteria (key, what-the-judge-checks).
# Keys are the TaskCategory.key values from sovereign_os.agents.categories.
_SAFETY = ("safety", "contains nothing harmful, offensive, or inappropriate")

CATEGORY_RUBRICS: dict[str, list[tuple[str, str]]] = {
    "coding": [
        ("correctness", "the code is logically correct and would run without errors for the stated task"),
        ("completeness", "implements every requirement — no stubbed, missing, or TODO parts"),
        ("robustness", "handles edge cases and failure paths; not brittle happy-path-only code"),
        ("relevance", "solves exactly the problem asked, in the requested language/stack"),
    ],
    "data": [
        ("correctness", "the analysis, numbers, and conclusions are accurate and free of fabrication"),
        ("completeness", "answers every part of the analytical question with adequate evidence"),
        ("rigor", "sound methodology; assumptions and limitations stated, not hand-waved"),
        ("relevance", "addresses the specific dataset/question asked"),
    ],
    "design": [
        ("relevance", "matches the brief's goal, audience, and constraints"),
        ("completeness", "covers all requested deliverables and specifications"),
        ("clarity", "the design/spec is unambiguous and directly usable by an implementer"),
        ("craft", "coherent, considered visual/UX choices — not generic filler"),
    ],
    "writing": [
        ("relevance", "addresses exactly the requested topic, angle, and audience"),
        ("completeness", "covers all requested points at the requested depth/length"),
        ("clarity", "well-structured, readable, and coherent"),
        ("voice", "tone and style fit the brief; free of obvious errors"),
    ],
    "email": [
        ("relevance", "achieves the email's stated purpose for the intended recipient"),
        ("tone", "appropriate register and professionalism for the context"),
        ("clarity", "clear ask/message and a concrete call to action"),
        ("correctness", "accurate details; no factual or naming errors"),
    ],
    "research": [
        ("correctness", "claims are accurate and supported; no fabricated facts or sources"),
        ("completeness", "covers the research question thoroughly with adequate breadth"),
        ("relevance", "focused on what was asked, not tangential"),
        ("rigor", "sources/evidence cited or reasoning shown; conclusions follow"),
    ],
    "automation": [
        ("correctness", "the spec/workflow is logically sound and would work as described"),
        ("completeness", "covers all steps, inputs, outputs, and error handling"),
        ("clarity", "unambiguous and directly actionable"),
        ("relevance", "solves the exact automation asked for"),
    ],
}

# Fallback for unknown/general categories: the original generic rubric.
DEFAULT_RUBRIC: list[tuple[str, str]] = [
    ("relevance", "addresses exactly what was asked"),
    ("completeness", "covers every requirement, no gaps"),
    ("correctness", "accurate, no fabrication or errors"),
]


def rubric_for(category: str | None) -> list[tuple[str, str]]:
    """
    Return the analytic criteria for a category, always ending with `safety`.

    Unknown/None categories fall back to the generic rubric. `safety` is never
    duplicated if a category already lists it.
    """
    base = CATEGORY_RUBRICS.get((category or "").strip().lower(), DEFAULT_RUBRIC)
    criteria = list(base)
    if not any(k == "safety" for k, _ in criteria):
        criteria.append(_SAFETY)
    return criteria


def shuffled_rubric(category: str | None, *, seed: str) -> list[tuple[str, str]]:
    """
    Deterministically permute a category's criteria order to blunt positional bias.

    The permutation is seeded by `seed` (typically the task_id) so it's stable for a
    given task — reproducible audits and non-flaky tests — while still varying order
    across tasks. `safety` participates in the shuffle too; it's still scored, just
    not pinned to last.
    """
    criteria = rubric_for(category)
    if len(criteria) <= 1:
        return criteria
    # Deterministic Fisher–Yates driven by a hash of (seed, index): no global RNG,
    # no dependence on Python's hash randomization.
    order = list(range(len(criteria)))
    for i in range(len(order) - 1, 0, -1):
        h = hashlib.sha256(f"{seed}:{i}".encode("utf-8")).digest()
        j = int.from_bytes(h[:8], "big") % (i + 1)
        order[i], order[j] = order[j], order[i]
    return [criteria[k] for k in order]
