"""Planner's decomposition step -- three real crashes found live and fixed
2026-08-17, none of which had a regression test until now:

1. capability defaulting to "any", a subject nobody subscribes to, silently
   losing sub-tasks with no error at all.
2. a malformed JSON decomposition crashing _execute() uncaught, halting the
   whole swarm over one goal's LLM hiccup.
3. a list-valued goal/instructions field (small models occasionally emit
   several bullet points instead of one string) raising a pydantic
   ValidationError that killed the entire decomposition, not just that one
   sub-task.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.contracts import PlanResult, PlannedTask
from src.agents.planner import PlannerAgent, _coerce_str
from src.packet import CXPPacket, PacketType, Payload


@pytest.mark.parametrize("value,expected", [
    ("already a string", "already a string"),
    (["point one", "point two"], "point one point two"),
    (None, ""),
    (42, "42"),
])
def test_coerce_str(value, expected):
    assert _coerce_str(value) == expected


def _goal_packet() -> CXPPacket:
    return CXPPacket(type=PacketType.PLAN, capability="plan", payload=Payload(goal="build a thing"))


async def _planner(monkeypatch, llm_response: str) -> tuple[PlannerAgent, AsyncMock]:
    p = PlannerAgent()
    monkeypatch.setattr(p._memory, "save", AsyncMock())
    monkeypatch.setattr(p, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(p, "llm", AsyncMock(return_value=llm_response))
    emitted = AsyncMock()
    monkeypatch.setattr(p, "emit_packet", emitted)
    return p, emitted


async def test_malformed_json_degrades_gracefully_instead_of_crashing(monkeypatch):
    p, emitted = await _planner(monkeypatch, "not valid json at all {{{")

    result = await p._execute(_goal_packet())

    assert "malformed JSON" in result
    emitted.assert_awaited_once()
    assert emitted.await_args.args[0].capability == "reflect"


async def test_planner_delegates_decomposition_parsing_to_contract(monkeypatch):
    p, emitted = await _planner(monkeypatch, "raw model output")
    parsed = PlanResult(subtasks=[PlannedTask(
        type="code",
        capability="code",
        goal="write a function",
        instructions="return code",
    )])
    calls = []

    def fake_parse_contract(capability, raw_text):
        calls.append((capability, raw_text))
        return parsed

    import src.agents.planner as planner_module
    monkeypatch.setattr(planner_module, "parse_contract", fake_parse_contract)

    await p._execute(_goal_packet())

    assert calls == [("plan", "raw model output")]
    emitted.assert_awaited_once()


async def test_planner_records_contract_failure_as_learnable_evidence(monkeypatch):
    p, _ = await _planner(monkeypatch, "not valid json")
    recorded = AsyncMock()
    monkeypatch.setattr(p, "record_attempt", recorded)

    await p._execute(_goal_packet())

    recorded.assert_awaited_once()
    evidence = recorded.await_args.kwargs
    assert evidence["capability"] == "plan"
    assert evidence["validation_status"] == "contract_error"
    assert evidence["environment_healthy"] is True
    assert evidence["raw_response"] == "not valid json"


async def test_planner_contract_failure_requests_planner_targeted_candidate(monkeypatch):
    p, emitted = await _planner(monkeypatch, "not valid json")
    monkeypatch.setattr(p, "record_attempt", AsyncMock())
    packet = _goal_packet()

    await p._execute(packet)

    reflect_packet: CXPPacket = emitted.await_args.args[0]
    assert reflect_packet.capability == "reflect"
    assert reflect_packet.payload.inputs == {
        "target_role": "planner",
        "source_attempt_id": packet.id,
        "evidence_class": "contract",
    }


async def test_planner_records_normalized_contract_evidence_on_success(monkeypatch):
    raw = """[
        {
            "type": "code",
            "goal": "write a function",
            "instructions": "return code"
        }
    ]"""
    p, _ = await _planner(monkeypatch, raw)
    recorded = AsyncMock()
    monkeypatch.setattr(p, "record_attempt", recorded)

    await p._execute(_goal_packet())

    evidence = recorded.await_args.kwargs
    assert evidence["capability"] == "plan"
    assert evidence["validation_status"] == "valid"
    assert evidence["raw_response"] == raw
    assert '"capability":"code"' in evidence["normalized_response"]


async def test_planner_propagates_candidate_evaluation_inputs_to_child(monkeypatch):
    sub_tasks = [{"type": "code", "capability": "code", "goal": "write code", "instructions": "return it"}]
    p, emitted = await _planner(monkeypatch, json.dumps(sub_tasks))
    monkeypatch.setattr(p, "record_attempt", AsyncMock())
    packet = _goal_packet()
    packet.payload.inputs = {"candidate_id": "candidate-1"}

    await p._execute(packet)

    child: CXPPacket = emitted.await_args.args[0]
    assert child.payload.inputs == {"candidate_id": "candidate-1"}


async def test_planner_requests_planner_candidate_when_every_subtask_is_invalid(monkeypatch):
    sub_tasks = [
        {"type": "code", "capability": "code", "priority": "not-a-number", "goal": "a", "instructions": "x"},
        {"type": "code", "capability": "code", "priority": "also-bad", "goal": "b", "instructions": "y"},
    ]
    p, emitted = await _planner(monkeypatch, json.dumps(sub_tasks))
    monkeypatch.setattr(p, "record_attempt", AsyncMock())

    result = await p._execute(_goal_packet())

    # A silent zero-subtask outcome after a structurally valid parse is the
    # same missed-learning-signal shape as a hard contract error -- it must
    # still request planner-targeted feedback, not just log locally.
    emitted.assert_awaited_once()
    reflect_packet: CXPPacket = emitted.await_args.args[0]
    assert reflect_packet.capability == "reflect"
    assert reflect_packet.payload.inputs["target_role"] == "planner"
    assert "Spawned 0 sub-packets" in result


async def test_missing_capability_falls_back_to_type_not_to_dead_any_subject(monkeypatch):
    sub_tasks = [{"type": "code", "goal": "write a function", "instructions": "do it"}]
    p, emitted = await _planner(monkeypatch, json.dumps(sub_tasks))

    await p._execute(_goal_packet())

    emitted.assert_awaited_once()
    child: CXPPacket = emitted.await_args.args[0]
    assert child.capability == "code"  # not "any" -- "any" has no consumer


async def test_list_valued_goal_field_is_coerced_not_crashed_on(monkeypatch):
    sub_tasks = [{
        "type": "code",
        "capability": "code",
        "goal": ["first point", "second point"],
        "instructions": "do it",
    }]
    p, emitted = await _planner(monkeypatch, json.dumps(sub_tasks))

    result = await p._execute(_goal_packet())

    emitted.assert_awaited_once()
    child: CXPPacket = emitted.await_args.args[0]
    assert child.payload.goal == "first point second point"
    assert "Spawned 1 sub-packets" in result


async def test_trailing_comma_in_an_otherwise_valid_decomposition_no_longer_fails(monkeypatch):
    """Found live 2026-08-21 via a full OTel span capture (packet
    dcb5043e): a genuinely complete, well-formed decomposition failed to
    parse purely because of a trailing comma after each object's last
    property. Before _strip_trailing_commas() existed, this was
    indistinguishable from real malformation/truncation."""
    raw = """[
    {
        "type": "code",
        "capability": "code",
        "goal": "write a function",
        "instructions": "do it",
        "priority": 3,
    },
]"""
    p, emitted = await _planner(monkeypatch, raw)

    result = await p._execute(_goal_packet())

    emitted.assert_awaited_once()
    assert "Spawned 1 sub-packets" in result


async def test_type_is_derived_from_the_validated_capability_not_trusted_raw(monkeypatch):
    # Found live 2026-08-20: the model emitted capability="code" but
    # type="verify" for a sub-task that WAS the real code-writing step (its
    # own goal/instructions were unambiguously about writing code, and the
    # executor it routed to -- via capability -- produced real code output,
    # verified at a real 0.9 score). Only `capability` is validated against
    # the known-safe set (code/verify/reflect); `type` was trusted as-is
    # from the model's own output, so it silently disagreed with capability.
    # Downstream, wait_for_results() keys off packet *type* == "code" to
    # decide a task produced an artifact -- a type="verify" packet is
    # invisible to that check forever, so the task times out even though
    # the swarm already finished it successfully.
    sub_tasks = [{"type": "verify", "capability": "code", "goal": "write a function", "instructions": "do it"}]
    p, emitted = await _planner(monkeypatch, json.dumps(sub_tasks))

    await p._execute(_goal_packet())

    emitted.assert_awaited_once()
    child: CXPPacket = emitted.await_args.args[0]
    assert child.capability == "code"
    assert child.type == PacketType.CODE  # derived from capability, not the model's raw "type"


async def test_one_malformed_subtask_does_not_kill_the_rest(monkeypatch):
    # Non-numeric priority is still a genuine construction failure (raised
    # by int(task.get("priority", ...)) inside the try) -- an invalid
    # *type* string is no longer one, now that type is derived from the
    # already-validated capability rather than trusted from the model
    # directly (see test_type_is_derived_from_the_validated_capability_
    # not_trusted_raw above).
    sub_tasks = [
        {"type": "code", "capability": "code", "priority": "not-a-number", "goal": "bad", "instructions": "x"},
        {"type": "code", "capability": "code", "goal": "good one", "instructions": "y"},
    ]
    p, emitted = await _planner(monkeypatch, json.dumps(sub_tasks))
    validation_failure = AsyncMock()
    monkeypatch.setattr(p, "record_validation_failure", validation_failure)

    result = await p._execute(_goal_packet())

    emitted.assert_awaited_once()
    child: CXPPacket = emitted.await_args.args[0]
    assert child.payload.goal == "good one"
    assert "Spawned 1 sub-packets" in result
    validation_failure.assert_awaited_once()
