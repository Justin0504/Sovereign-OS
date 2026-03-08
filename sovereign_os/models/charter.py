"""
Charter schema: the single source of truth for what the autonomous entity is and does.

The system is entirely generic; it does not "know" the business until it parses Charter.yaml.
"""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Nested models (Charter schema components)
# ---------------------------------------------------------------------------


class FiscalBoundaries(BaseModel):
    """
    Daily burn limit, max budget, currency, and profitability guardrails.

    min_job_margin_ratio: Minimum gross margin (0–1) per paid job. CFO rejects the job if
    estimated cost > job_revenue * (1 - min_job_margin_ratio). E.g. 0.2 = 20% margin floor
    (cost must be ≤ 80% of revenue). Set to 0 to disable (accept any cost within balance).
    """

    daily_burn_max_usd: Annotated[
        float,
        Field(ge=0, description="Maximum USD allowed to spend per calendar day"),
    ] = 0.0
    max_budget_usd: Annotated[
        float,
        Field(ge=0, description="Total budget cap in USD (runway ceiling)"),
    ] = 0.0
    currency: Annotated[str, Field(min_length=1)] = "USD"
    min_job_margin_ratio: Annotated[
        float,
        Field(ge=0, le=1, description="Minimum gross margin per job (0=disabled, 0.35=35%% margin floor)"),
    ] = 0.35

    @field_validator("daily_burn_max_usd", "max_budget_usd", "min_job_margin_ratio", mode="before")
    @classmethod
    def coerce_float(cls, v: object) -> float:
        if isinstance(v, (int, str)):
            return float(v)
        return v  # type: ignore[return-value]


class CoreCompetency(BaseModel):
    """A single capability the entity can hire workers for."""

    name: Annotated[str, Field(min_length=1)]
    description: str = ""
    priority: Annotated[int, Field(ge=1, le=10)] = 5


class SuccessKPI(BaseModel):
    """A measurable success criterion from the Charter."""

    name: Annotated[str, Field(min_length=1)]
    metric: Annotated[str, Field(min_length=1)]  # e.g. "revenue_usd", "tasks_done"
    target_value: float = 0.0
    unit: str = ""
    verification_prompt: str = ""  # For AuditorAgent to verify against


# ---------------------------------------------------------------------------
# Root Charter model
# ---------------------------------------------------------------------------


class Charter(BaseModel):
    """
    Corporate Charter: Mission, competencies, fiscal bounds, and success KPIs.

    The CEO Agent interprets this to dynamically hire (instantiate) Worker Agents via MCP.
    """

    mission: Annotated[str, Field(min_length=1)]
    core_competencies: list[CoreCompetency] = Field(default_factory=list)
    fiscal_boundaries: FiscalBoundaries = Field(default_factory=FiscalBoundaries)
    success_kpis: list[SuccessKPI] = Field(default_factory=list)
    entity_id: str | None = None  # Optional stable identifier for this charter instance

    model_config = {"extra": "forbid", "strict": True}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_charter(path: str | Path) -> Charter:
    """Load and validate a Charter from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Charter file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("Charter YAML must resolve to a mapping (dict).")
    return Charter.model_validate(data)
