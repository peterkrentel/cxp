"""#86: verifier passed an artifact (score >= DEPLOY_THRESHOLD) that then
failed at actual sandbox execution -- confirmed live 2026-08-30 twice
(a circle-area function and a doubling function, both with a wrong
expected value in their own generated test). Verifier's own opinion is
the only signal reflect ever saw; a real execution failure at this point
is much stronger, deterministic evidence that something is actually
wrong -- this pins that it now triggers its own reflect packet instead
of being silently absorbed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.agents.deployer import DeployerAgent
from src.packet import CXPPacket, PacketType, Payload


def _packet(**overrides) -> CXPPacket:
    defaults = dict(
        type=PacketType.REFLECT,
        capability="deploy",
        task_id="task-123",
        payload=Payload(goal="write a thing", instructions="0.9", context="def f(): pass"),
    )
    defaults.update(overrides)
    return CXPPacket(**defaults)


async def test_deploy_failure_above_threshold_triggers_reflect(monkeypatch):
    d = DeployerAgent()
    monkeypatch.setattr(d._memory, "save", AsyncMock())
    monkeypatch.setattr(d, "_try_deploy", AsyncMock(return_value={
        "deployed": False, "outcome": "error", "stdout": "", "stderr": "AssertionError: ...",
    }))
    emitted = []
    monkeypatch.setattr(d, "emit_packet", AsyncMock(side_effect=lambda p: emitted.append(p)))

    packet = _packet()
    await d._execute(packet)

    assert len(emitted) == 1
    reflect = emitted[0]
    assert reflect.capability == "reflect"
    assert reflect.task_id == "task-123"
    assert reflect.payload.inputs["evidence_class"] == "deterministic-validator"
    assert reflect.payload.inputs["target_role"] == "executor"
    assert "AssertionError" in reflect.payload.instructions


async def test_deploy_below_threshold_does_not_trigger_reflect(monkeypatch):
    d = DeployerAgent()
    monkeypatch.setattr(d._memory, "save", AsyncMock())
    emitted = []
    monkeypatch.setattr(d, "emit_packet", AsyncMock(side_effect=lambda p: emitted.append(p)))

    # Below DEPLOY_THRESHOLD -- returns early, never actually attempts to deploy.
    packet = _packet(payload=Payload(goal="write a thing", instructions="0.5", context="def f(): pass"))
    await d._execute(packet)

    assert emitted == []


async def test_deploy_success_does_not_trigger_reflect(monkeypatch):
    d = DeployerAgent()
    monkeypatch.setattr(d._memory, "save", AsyncMock())
    monkeypatch.setattr(d, "_try_deploy", AsyncMock(return_value={"deployed": True, "outcome": "ran"}))
    emitted = []
    monkeypatch.setattr(d, "emit_packet", AsyncMock(side_effect=lambda p: emitted.append(p)))

    packet = _packet()
    await d._execute(packet)

    assert emitted == []
