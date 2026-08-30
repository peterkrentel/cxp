"""TRANSIENT_EXCEPTIONS classification and the recurrence counter --
diagnostician's evidence-based (no-LLM) path for timeout-class halts
depends on both being right.

The "exceeded total budget" entry is a regression test for a real bug
found live 2026-08-17: str(TimeoutError("LLM call exceeded total budget
of 240.0s")) does NOT contain the substring "TimeoutError" (Python's
exception str() doesn't prefix the class name), so AgentShell's own
raised total-budget timeout silently failed to match and got routed to
the wrong (LLM-authored) diagnosis path instead of the evidence-based one."""

from __future__ import annotations

import time

import pytest
from src.agent_shell import KV_STATE
from src.agents.diagnostician import KV_RECURRENCE_KEY, TRANSIENT_EXCEPTIONS
from src.packet import CXPPacket, PacketType, Payload


def _is_timeout_class(exc_detail: str) -> bool:
    return any(name in exc_detail for name in TRANSIENT_EXCEPTIONS)


@pytest.mark.parametrize("detail", [
    "LLM call exceeded total budget of 240.0s",
    "LLM call exceeded total budget of 900.0s",
    "ReadTimeout",
    "httpx.ConnectTimeout: connection attempt timed out",
    "PoolTimeout",
])
def test_known_timeout_shapes_are_classified_as_timeout_class(detail):
    assert _is_timeout_class(detail) is True


@pytest.mark.parametrize("detail", [
    "ValueError: invalid literal for int() with base 10: 'high'",
    "KeyError: 'goal'",
    "malformed JSON from model",
])
def test_non_timeout_failures_are_not_misclassified(detail):
    assert _is_timeout_class(detail) is False


async def test_recent_timeout_count_only_counts_within_window(fake_kv):
    from src.agents.diagnostician import DiagnosticianAgent
    d = DiagnosticianAgent()
    d._kv_cache[KV_STATE] = fake_kv

    assert await d._recent_timeout_count() == 0
    await d._record_timeout()
    assert await d._recent_timeout_count() == 1

    await d._record_timeout()
    assert await d._recent_timeout_count() == 2

    # A timeout recorded well outside the 15-minute recurrence window
    # shouldn't inflate the "recurring pattern" count.
    import json
    entry = await fake_kv.get(KV_RECURRENCE_KEY)
    history = json.loads(entry.value.decode())
    history[0] = time.time() - 3600  # 1 hour ago
    await fake_kv.put(KV_RECURRENCE_KEY, json.dumps(history).encode())

    assert await d._recent_timeout_count() == 1


async def test_non_timeout_diagnosis_preserves_packet_lineage(monkeypatch):
    from src.agents.diagnostician import DiagnosticianAgent

    d = DiagnosticianAgent()
    packet = CXPPacket(
        type=PacketType.DIAGNOSE,
        capability="diagnose",
        task_id="task-123",
        parent_packet_id="parent-456",
        payload=Payload(goal="diagnose"),
    )
    observed = {}

    async def fake_llm(system, prompt, **kwargs):
        observed.update(kwargs)
        return '{"diagnosis": "ok", "suggested_action": "resume"}'

    monkeypatch.setattr(d, "llm", fake_llm)

    result = await d._diagnose("halt details", packet)

    assert result == {"diagnosis": "ok", "suggested_action": "resume"}
    assert observed == {
        "packet_id": packet.id,
        "task_id": packet.task_id,
        "parent_packet_id": packet.parent_packet_id,
        "json_mode": True,
    }
