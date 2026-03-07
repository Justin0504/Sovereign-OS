"""
Normalize RawOrder to Sovereign-OS job payload: goal, amount_cents, currency, charter.
"""

from __future__ import annotations

from sovereign_os.ingest_bridge.sources.base import RawOrder


def to_job_payload(raw: RawOrder) -> dict:
    return {
        "goal": raw.goal[:20_000],
        "amount_cents": max(0, raw.amount_cents),
        "currency": raw.currency or "USD",
        "charter": raw.charter or "Default",
    }
