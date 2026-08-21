"""record_llm_call() -- the actual new telemetry logic added 2026-08-21,
after a night of diagnosing "malformed JSON from model" / LLM-timeout
failures purely from a 200-char truncated snippet plus an exception's
line/column number, with zero way to see what the model actually
produced. Every LLM call now gets an OTel span with the full system/user
prompt and full response (or, on a timeout, whatever partial content had
actually been generated), exported via OTLP to Grafana Cloud -- so it
survives a pod restart instead of vanishing with whatever local
`kubectl logs` happened to still have.

kept deliberately decoupled from llm()'s actual httpx/streaming
mechanics (already covered by test_llm_stream_reassembly.py) -- this
tests only "did we stamp the right attributes onto the span," using a
real in-process TracerProvider + InMemorySpanExporter, no network."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.telemetry import record_llm_call


def _tracer():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_record_llm_call_success_captures_full_input_and_output():
    tracer, exporter = _tracer()
    with tracer.start_as_current_span("llm.call") as span:
        record_llm_call(span, agent_id="planner-1", packet_id="abc123",
                         system="SYS", user="USER GOAL", duration_seconds=1.5,
                         timed_out=False, response="the full response text")

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["agent.id"] == "planner-1"
    assert attrs["packet.id"] == "abc123"
    assert attrs["llm.system_prompt"] == "SYS"
    assert attrs["llm.user_prompt"] == "USER GOAL"
    assert attrs["llm.response"] == "the full response text"
    assert attrs["llm.duration_seconds"] == 1.5
    assert attrs["llm.timed_out"] is False
    assert "llm.partial_response" not in attrs


def test_record_llm_call_timeout_captures_partial_response_not_full_response():
    """The core fix this whole module exists for: a timeout must not lose
    whatever content had already been generated. Found live 2026-08-21 --
    a 900s executor timeout left literally zero record of what the model
    had produced up to that point."""
    tracer, exporter = _tracer()
    with tracer.start_as_current_span("llm.call") as span:
        record_llm_call(span, agent_id="executor-1", packet_id="def456",
                         system="SYS", user="USER GOAL", duration_seconds=900.0,
                         timed_out=True, response="partial content generated so far")

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["llm.timed_out"] is True
    assert attrs["llm.partial_response"] == "partial content generated so far"
    assert "llm.response" not in attrs


def test_record_llm_call_omits_packet_id_attribute_when_not_provided():
    """Not every llm() caller has a packet_id in scope (e.g. a future
    non-packet-triggered call) -- must not stamp a bogus empty value that
    would be indistinguishable from a genuine empty string."""
    tracer, exporter = _tracer()
    with tracer.start_as_current_span("llm.call") as span:
        record_llm_call(span, agent_id="planner-1", packet_id=None,
                         system="SYS", user="USER", duration_seconds=0.5,
                         timed_out=False, response="ok")

    attrs = exporter.get_finished_spans()[0].attributes
    assert "packet.id" not in attrs
