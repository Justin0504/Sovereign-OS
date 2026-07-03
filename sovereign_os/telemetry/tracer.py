"""
OpenTelemetry tracing and Prometheus-compatible metrics.

Wraps LLM calls and Governance decisions with spans; exports tokens per mission
and success rate per model to a local Prometheus endpoint (e.g. :9464/metrics).
"""

import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_TRACER = None
_PROMETHEUS_STARTED = False

# Optional OpenTelemetry
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

# Prometheus metrics (prometheus_client)
_tokens_counter = None
_success_counter = None
_fail_counter = None

try:
    from prometheus_client import Counter, Histogram, Gauge, start_http_server
    _tokens_counter = Counter(
        "sovereign_tokens_total",
        "Total tokens used (input+output) per model",
        ["model"],
    )
    _success_counter = Counter(
        "sovereign_audit_success_total",
        "Audit pass count per model",
        ["model"],
    )
    _fail_counter = Counter(
        "sovereign_audit_fail_total",
        "Audit fail count per model",
        ["model"],
    )
    _jobs_completed_total = Counter(
        "sovereign_jobs_completed_total",
        "Total jobs finished by status",
        ["status"],
    )
    _job_duration_seconds = Histogram(
        "sovereign_job_duration_seconds",
        "Job execution duration in seconds",
        buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0),
    )
    _jobs_queue_pending = Gauge("sovereign_jobs_pending", "Number of jobs pending approval")
    _jobs_queue_running = Gauge("sovereign_jobs_running", "Number of jobs currently running")
    # --- Governance guardrails ---
    # CFO circuit breaker (session state; refreshed on scrape / after each audit).
    _breaker_spend_cents = Gauge("sovereign_breaker_session_spend_cents", "CFO breaker: cumulative session spend (cents)")
    _breaker_revenue_cents = Gauge("sovereign_breaker_session_revenue_cents", "CFO breaker: cumulative session revenue (cents)")
    _breaker_ceiling_cents = Gauge("sovereign_breaker_session_ceiling_cents", "CFO breaker: session spend ceiling (cents; 0=off)")
    _breaker_consecutive_failures = Gauge("sovereign_breaker_consecutive_failures", "CFO breaker: consecutive audit failures")
    _breaker_roi = Gauge("sovereign_breaker_roi", "CFO breaker: realized ROI (revenue/spend; -1 when undefined)")
    _breaker_tripped = Gauge("sovereign_breaker_tripped", "CFO breaker: 1 if tripped else 0")
    _breaker_trips_total = Counter("sovereign_breaker_trips_total", "CFO breaker: cumulative trip count", ["reason"])
    # JIT capability leases + per-agent trust.
    _active_leases = Gauge("sovereign_active_leases", "Active JIT capability leases (total)")
    _agent_trust = Gauge("sovereign_agent_trust_score", "TrustScore (0-100) per agent", ["agent"])
    # Audit quality (per-category rubric).
    _AUDIT_BUCKETS = (0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0)
    _audit_score = Histogram("sovereign_audit_score", "Overall audit score per category", ["category"], buckets=_AUDIT_BUCKETS)
    _audit_criterion_score = Histogram(
        "sovereign_audit_criterion_score", "Rubric criterion score", ["category", "criterion"], buckets=_AUDIT_BUCKETS,
    )
    # Autonomous profitability + self-repair.
    _tasks_screened_total = Counter("sovereign_tasks_screened_total", "Ingest profitability screen decisions", ["decision"])
    _task_repairs_total = Counter("sovereign_task_repairs_total", "Reactive self-repair outcomes", ["outcome"])
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    _jobs_completed_total = None
    _job_duration_seconds = None
    _jobs_queue_pending = None
    _jobs_queue_running = None
    _breaker_spend_cents = None
    _breaker_revenue_cents = None
    _breaker_ceiling_cents = None
    _breaker_consecutive_failures = None
    _breaker_roi = None
    _breaker_tripped = None
    _breaker_trips_total = None
    _active_leases = None
    _agent_trust = None
    _audit_score = None
    _audit_criterion_score = None
    _tasks_screened_total = None
    _task_repairs_total = None


def init_telemetry(
    service_name: str = "sovereign-os",
    prometheus_port: int = 9464,
    trace_to_console: bool = False,
) -> None:
    """Initialize tracer and start Prometheus HTTP server for metrics."""
    global _TRACER, _PROMETHEUS_STARTED
    if _OTEL_AVAILABLE and trace_to_console:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer(service_name, "0.1.0")
    elif _OTEL_AVAILABLE:
        trace.set_tracer_provider(TracerProvider())
        _TRACER = trace.get_tracer(service_name, "0.1.0")
    if _PROMETHEUS_AVAILABLE and prometheus_port > 0 and not _PROMETHEUS_STARTED:
        try:
            start_http_server(port=prometheus_port, addr="0.0.0.0")
            _PROMETHEUS_STARTED = True
            logger.info("Prometheus metrics on http://0.0.0.0:%s/metrics", prometheus_port)
        except OSError as e:
            logger.warning("Prometheus server failed to start: %s", e)


def get_tracer():
    """Return the global OpenTelemetry tracer or a no-op."""
    if _OTEL_AVAILABLE and _TRACER is not None:
        return _TRACER
    class _NoopTracer:
        def start_span(self, name: str, **kwargs: Any) -> Any:
            return _NoopSpan()
    return _NoopTracer()


class _NoopSpan:
    def __enter__(self): return self
    def __exit__(self, *a: Any): return False
    def set_attribute(self, k: str, v: Any) -> None: pass
    def set_status(self, status: Any) -> None: pass
    def end(self) -> None: pass
    def record_exception(self, e: Exception) -> None: pass


def get_meter():
    """Return a no-op meter (metrics use prometheus_client directly)."""
    class _NoopMeter:
        def create_counter(self, *a: Any, **kw: Any): return _NoopCounter()
    return _NoopMeter()


class _NoopCounter:
    def add(self, x: float, attributes: Any = None) -> None: pass


def record_llm_tokens(model: str, input_tokens: int, output_tokens: int) -> None:
    """Record token usage for cost analytics (Prometheus)."""
    if _tokens_counter is not None:
        try:
            _tokens_counter.labels(model=model or "unknown").inc(input_tokens + output_tokens)
        except Exception:
            pass


def record_job_completed(status: str, duration_seconds: float) -> None:
    """Record job completion for Prometheus (sovereign_jobs_completed_total, sovereign_job_duration_seconds)."""
    if _jobs_completed_total is not None:
        try:
            _jobs_completed_total.labels(status=status).inc()
        except Exception:
            pass
    if _job_duration_seconds is not None and duration_seconds >= 0:
        try:
            _job_duration_seconds.observe(duration_seconds)
        except Exception:
            pass


def set_job_queue_gauges(pending: int, running: int) -> None:
    """Set sovereign_jobs_pending and sovereign_jobs_running gauges (e.g. when serving /metrics)."""
    if _jobs_queue_pending is not None:
        try:
            _jobs_queue_pending.set(pending)
        except Exception:
            pass
    if _jobs_queue_running is not None:
        try:
            _jobs_queue_running.set(running)
        except Exception:
            pass


def record_audit_rubric(category: str, score: float, sub_scores: dict | None = None) -> None:
    """
    Record one audit outcome for Prometheus: overall score per category plus each
    rubric criterion score (correctness/robustness/…). Enables Grafana panels for
    quality distribution and per-criterion weak spots. No-op if client missing.
    """
    cat = (category or "unknown")
    if _audit_score is not None:
        try:
            _audit_score.labels(category=cat).observe(max(0.0, min(1.0, float(score))))
        except Exception:
            pass
    if _audit_criterion_score is not None and sub_scores:
        for crit, val in sub_scores.items():
            try:
                _audit_criterion_score.labels(category=cat, criterion=str(crit)).observe(
                    max(0.0, min(1.0, float(val)))
                )
            except Exception:
                pass


def record_breaker_trip(reason: str = "") -> None:
    """Increment the cumulative CFO circuit-breaker trip counter (labeled by reason kind)."""
    if _breaker_trips_total is not None:
        try:
            _breaker_trips_total.labels(reason=_trip_reason_kind(reason)).inc()
        except Exception:
            pass


def record_task_screened(taken: bool) -> None:
    """Count an ingest profitability-screen decision (take vs skip)."""
    if _tasks_screened_total is not None:
        try:
            _tasks_screened_total.labels(decision="take" if taken else "skip").inc()
        except Exception:
            pass


def record_task_repair(recovered: bool) -> None:
    """Count a reactive self-repair outcome (recovered to passing vs exhausted)."""
    if _task_repairs_total is not None:
        try:
            _task_repairs_total.labels(outcome="recovered" if recovered else "exhausted").inc()
        except Exception:
            pass


def _trip_reason_kind(reason: str) -> str:
    """Bucket a free-text trip reason into a low-cardinality label."""
    r = (reason or "").lower()
    if "ceiling" in r:
        return "ceiling"
    if "consecutive" in r or "failure" in r:
        return "failure_streak"
    if "roi" in r:
        return "roi"
    return "other"


def set_governance_gauges(breaker_status: dict | None = None, active_leases: int | None = None,
                          agent_trust: dict | None = None) -> None:
    """
    Refresh point-in-time governance gauges (breaker session state, active JIT lease
    count, per-agent trust). Call on /metrics scrape and/or after each audit so the
    exported values are current. Any argument left None is skipped.
    """
    if breaker_status is not None:
        _set(_breaker_spend_cents, breaker_status.get("spent_cents", 0))
        _set(_breaker_revenue_cents, breaker_status.get("revenue_cents", 0))
        _set(_breaker_ceiling_cents, breaker_status.get("session_ceiling_cents", 0))
        _set(_breaker_consecutive_failures, breaker_status.get("consecutive_failures", 0))
        roi = breaker_status.get("roi")
        _set(_breaker_roi, roi if roi is not None else -1)
        _set(_breaker_tripped, 1 if breaker_status.get("tripped") else 0)
    if active_leases is not None:
        _set(_active_leases, active_leases)
    if agent_trust:
        for agent_id, info in agent_trust.items():
            score = info.get("trust_score") if isinstance(info, dict) else info
            if score is not None and _agent_trust is not None:
                try:
                    _agent_trust.labels(agent=str(agent_id)).set(score)
                except Exception:
                    pass


def _set(gauge: Any, value: Any) -> None:
    if gauge is not None:
        try:
            gauge.set(value)
        except Exception:
            pass


def get_prometheus_metrics_output(pending: int = 0, running: int = 0) -> bytes:
    """Update queue gauges and return Prometheus text format. Use from FastAPI /metrics endpoint."""
    set_job_queue_gauges(pending, running)
    if not _PROMETHEUS_AVAILABLE:
        return b"# Prometheus client not installed\n"
    try:
        from prometheus_client import REGISTRY, generate_latest
        return generate_latest(REGISTRY)
    except Exception:
        return b"# Metrics export failed\n"


def record_mission_success(model: str, success: bool) -> None:
    """Record audit outcome for success rate per model."""
    if success and _success_counter is not None:
        try:
            _success_counter.labels(model=model or "unknown").inc()
        except Exception:
            pass
    elif not success and _fail_counter is not None:
        try:
            _fail_counter.labels(model=model or "unknown").inc()
        except Exception:
            pass


@contextmanager
def span_governance(operation: str, **attributes: str | int | float | bool) -> Iterator[Any]:
    """Context manager for Governance spans (run_mission, dispatch, approve_task)."""
    tracer = get_tracer()
    span = tracer.start_span(f"governance.{operation}")
    for k, v in attributes.items():
        try:
            span.set_attribute(str(k), v)
        except Exception:
            pass
    try:
        yield span
        if _OTEL_AVAILABLE:
            span.set_status(trace.Status(trace.StatusCode.OK))
    except Exception as e:
        if _OTEL_AVAILABLE:
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            span.record_exception(e)
        raise
    finally:
        span.end()


@contextmanager
def span_llm(operation: str, model: str = "", **attributes: str | int | float | bool) -> Iterator[Any]:
    """Context manager for LLM call spans (strategist, judge)."""
    tracer = get_tracer()
    span = tracer.start_span(f"llm.{operation}")
    span.set_attribute("llm.model", model or "unknown")
    for k, v in attributes.items():
        try:
            span.set_attribute(str(k), v)
        except Exception:
            pass
    try:
        yield span
        if _OTEL_AVAILABLE:
            span.set_status(trace.Status(trace.StatusCode.OK))
    except Exception as e:
        if _OTEL_AVAILABLE:
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            span.record_exception(e)
        raise
    finally:
        span.end()
