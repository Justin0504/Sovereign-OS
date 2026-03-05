"""
Telemetry: OpenTelemetry tracing and Prometheus-compatible metrics.
"""

from sovereign_os.telemetry.tracer import (
    get_meter,
    get_tracer,
    record_llm_tokens,
    record_mission_success,
    span_governance,
    span_llm,
)

__all__ = [
    "get_meter",
    "get_tracer",
    "record_llm_tokens",
    "record_mission_success",
    "span_governance",
    "span_llm",
]
