"""OpenTelemetry setup for the swarm.

Every LLM call gets a span with the full system/user prompt and full
response (or, on a timeout, whatever partial content had actually been
generated), exported via OTLP to Grafana Cloud. Added 2026-08-21 after a
night of diagnosing "malformed JSON from model" / LLM-timeout failures
purely from a 200-char truncated snippet plus an exception's line/column
number -- with no way to see what the model actually produced, or
whether a timed-out call had generated anything coherent at all before
its budget ran out. Because it ships off-cluster the moment a span
finishes, this data survives a pod restart -- unlike `kubectl logs`,
which has repeatedly been the wrong tool for this: several times this
project has needed to inspect what a since-recycled pod logged, and the
answer was always "gone."

No in-cluster collector -- Grafana Cloud ingests OTLP directly, so
OTLPSpanExporter() just needs OTEL_EXPORTER_OTLP_ENDPOINT and
OTEL_EXPORTER_OTLP_HEADERS set (standard OTel SDK env vars, read
automatically). If OTEL_EXPORTER_OTLP_ENDPOINT is unset, init_tracing()
is a no-op and every span becomes a no-op too -- safe to call
unconditionally from every agent's startup rather than gating it behind
a config flag at every call site.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

_initialized = False


def init_tracing(service_name: str) -> None:
    """Idempotent -- safe to call every time a process starts, even
    across multiple agent subclasses in the same run (e.g. tests
    importing several agent modules)."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return  # telemetry not configured -- get_tracer() spans are no-ops

    # Imported lazily so importing this module never requires the OTLP
    # exporter package to be installed unless tracing is actually enabled.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def get_tracer(name: str):
    """Always safe to call, configured or not -- returns a real tracer
    once init_tracing() has set a real provider, or OTel's default no-op
    provider otherwise (start_as_current_span() still works, it just
    produces a non-recording span)."""
    return trace.get_tracer(name)


def record_llm_call(
    span,
    *,
    agent_id: str,
    packet_id: str | None,
    task_id: str | None = None,
    parent_packet_id: str | None = None,
    system: str,
    user: str,
    duration_seconds: float,
    timed_out: bool,
    response: str,
) -> None:
    """Stamps one LLM call's full input/output onto its span.

    Call this on EVERY outcome, not just success -- on a timeout,
    `response` is whatever partial content had actually been generated
    before the budget ran out, stored under a distinctly-named attribute
    so it's never confused with a genuinely complete response. Without
    this, a timed-out call left zero record of what the model had
    produced so far -- a real incident live 2026-08-21 (executor, 900s
    timeout) where there was no way to tell "stuck looping / producing
    garbage" apart from "generating something reasonable that just
    needed more time."
    """
    span.set_attribute("agent.id", agent_id)
    if packet_id:
        span.set_attribute("packet.id", packet_id)
    # Without these two, a span can be looked up by its own packet.id but
    # a Tempo/Grafana dashboard can't group by task lineage or reconstruct
    # a parent-child chain -- confirmed live 2026-08-23 building the OTel
    # dashboard: only packet.id was ever being stamped here.
    if task_id:
        span.set_attribute("task.id", task_id)
    if parent_packet_id:
        span.set_attribute("parent.packet.id", parent_packet_id)
    span.set_attribute("llm.system_prompt", system)
    span.set_attribute("llm.user_prompt", user)
    span.set_attribute("llm.duration_seconds", duration_seconds)
    span.set_attribute("llm.timed_out", timed_out)
    span.set_attribute("llm.partial_response" if timed_out else "llm.response", response)
