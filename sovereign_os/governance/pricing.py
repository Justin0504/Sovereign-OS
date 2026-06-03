"""
Per-model token pricing for accurate cost tracing.

The ledger previously costed every model at a single blended rate (~10 cents per
1k tokens), so a gpt-4o-mini call and an o1 call recorded the same cost despite a
~100x real price gap. This table prices input and output tokens separately, per
model, so cost traces and CFO budgets reflect reality.

Prices are USD per 1,000,000 tokens (input, output), accurate to early-2026
public list prices. Override or extend at runtime with the
SOVEREIGN_MODEL_PRICING_JSON env var:

    SOVEREIGN_MODEL_PRICING_JSON='{"my-model": [1.0, 3.0]}'

Matching is exact first, then longest-prefix (so "gpt-4o-2024-11-20" matches
"gpt-4o"), then a conservative fallback.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# model_id -> (usd_per_1m_input, usd_per_1m_output)
DEFAULT_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o1-mini": (1.10, 4.40),
    "o1-preview": (15.00, 60.00),
    "o1": (15.00, 60.00),
    # Anthropic
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-opus": (15.00, 75.00),
    "claude-haiku-4": (1.00, 5.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
}

# Conservative fallback when a model is unknown (prevents silent under-costing).
FALLBACK_PRICING: tuple[float, float] = (2.00, 8.00)


def _load_overrides() -> dict[str, tuple[float, float]]:
    raw = os.getenv("SOVEREIGN_MODEL_PRICING_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out: dict[str, tuple[float, float]] = {}
        for k, v in data.items():
            out[str(k)] = (float(v[0]), float(v[1]))
        return out
    except Exception as e:  # pragma: no cover - bad env config
        logger.warning("PRICING: ignoring invalid SOVEREIGN_MODEL_PRICING_JSON: %s", e)
        return {}


def get_model_pricing(model_id: str) -> tuple[float, float]:
    """Resolve (input, output) USD-per-1M price for a model id, with overrides + prefix match."""
    table = {**DEFAULT_MODEL_PRICING, **_load_overrides()}
    mid = (model_id or "").strip().lower()
    if not mid:
        return FALLBACK_PRICING
    if mid in table:
        return table[mid]
    # Longest-prefix match: "gpt-4o-2024-11-20" -> "gpt-4o" (prefer "gpt-4o-mini" if it matches).
    best_key = ""
    for key in table:
        if mid.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return table[best_key]
    # Substring fallback: model_id contained in a known key or vice versa.
    for key, price in table.items():
        if key in mid or mid in key:
            return price
    logger.debug("PRICING: no match for model '%s'; using fallback %s.", model_id, FALLBACK_PRICING)
    return FALLBACK_PRICING


def estimate_cost_cents(model_id: str, input_tokens: int, output_tokens: int) -> int:
    """
    Estimated USD cost (rounded to nearest cent) for a model call.

    cost = input_tokens/1e6 * price_in + output_tokens/1e6 * price_out
    Sub-cent calls round to 0 — use `estimate_cost_usd` for sub-cent rollups.
    """
    return round(estimate_cost_usd(model_id, input_tokens, output_tokens) * 100)


def estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Precise estimated USD cost (no rounding) for sub-cent aggregation."""
    price_in, price_out = get_model_pricing(model_id)
    return (max(0, input_tokens) / 1_000_000.0) * price_in + (max(0, output_tokens) / 1_000_000.0) * price_out


def estimate_budget_cost_cents(
    model_id: str, token_budget: int, *, output_ratio: float = 0.5, floor_cents: int = 1
) -> int:
    """
    Pre-flight cost estimate (cents) for a task with only a total token budget.

    Splits the budget into input/output by `output_ratio` and prices it with the
    model's real rates, so the CFO budgets on the same basis the ledger later
    records actuals — keeping the estimate→actual overrun loop meaningful.
    `floor_cents` keeps a minimum so a task is never costed as entirely free.
    """
    ratio = min(1.0, max(0.0, output_ratio))
    out_tokens = int(max(0, token_budget) * ratio)
    in_tokens = max(0, token_budget) - out_tokens
    cents = round(estimate_cost_usd(model_id, in_tokens, out_tokens) * 100)
    return max(floor_cents, cents)
