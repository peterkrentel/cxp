"""Candidate skill revisions must be isolated from active skills."""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.agent_shell import KV_CANDIDATE_EVALUATIONS, KV_SKILL_CANDIDATES
from src.agents.reflect import ReflectAgent
from src.packet import CXPPacket, PacketType, Payload


async def test_skill_candidate_is_stored_in_separate_kv_bucket(agent, fake_kv):
    agent._kv_cache[KV_SKILL_CANDIDATES] = fake_kv

    revision = await agent.put_skill_candidate("candidate-1", {
        "target_role": "planner",
        "content": "Return valid JSON.",
        "source_attempt_id": "attempt-1",
        "rationale": "Malformed plan output.",
    })

    entry = await fake_kv.get("candidate-1")
    assert revision == entry.revision
    assert b'"target_role": "planner"' in entry.value


async def test_staged_skill_candidate_can_be_read_without_active_skill_lookup(agent, fake_kv):
    agent._kv_cache[KV_SKILL_CANDIDATES] = fake_kv
    await agent.put_skill_candidate("candidate-1", {
        "target_role": "executor",
        "content": "Return raw YAML only.",
        "source_attempt_id": "attempt-1",
        "rationale": "Remove fences.",
    })

    candidate = await agent.get_skill_candidate("candidate-1")

    assert candidate is not None
    assert candidate["target_role"] == "executor"
    assert candidate["content"] == "Return raw YAML only."


async def test_candidate_evaluation_report_is_stored_separately(agent, fake_kv):
    agent._kv_cache[KV_CANDIDATE_EVALUATIONS] = fake_kv

    revision = await agent.put_candidate_evaluation("candidate-1", {
        "candidate_id": "candidate-1",
        "recommendation": "recommend_promotion",
    })

    entry = await fake_kv.get("candidate-1")
    assert revision == entry.revision
    assert b'"recommendation": "recommend_promotion"' in entry.value


async def test_reflect_creates_candidate_without_overwriting_active_skill(monkeypatch):
    agent = ReflectAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value="Current planner skill"))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="Return a JSON array only."))
    monkeypatch.setattr(agent, "put_skill", AsyncMock())
    put_candidate = AsyncMock(return_value=9)
    monkeypatch.setattr(agent, "put_skill_candidate", put_candidate)
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    packet = CXPPacket(
        type=PacketType.REFLECT,
        capability="reflect",
        parent_packet_id="attempt-1",
        payload=Payload(
            goal="Improve planner output",
            instructions="Planner returned malformed JSON.",
            inputs={"target_role": "planner", "source_attempt_id": "attempt-1"},
        ),
    )

    result = await agent._execute(packet)

    agent.put_skill.assert_not_awaited()
    put_candidate.assert_awaited_once()
    key, candidate = put_candidate.await_args.args
    assert key == packet.id
    assert candidate["target_role"] == "planner"
    assert candidate["source_attempt_id"] == "attempt-1"
    assert "candidate" in result.lower()


async def test_reflect_rejects_platform_unhealthy_source_attempt(monkeypatch):
    agent = ReflectAgent()
    agent._memory.attempts = [{"attempt_id": "attempt-1", "environment_healthy": False}]
    monkeypatch.setattr(agent, "get_skill", AsyncMock())
    monkeypatch.setattr(agent, "llm", AsyncMock())
    put_candidate = AsyncMock()
    monkeypatch.setattr(agent, "put_skill_candidate", put_candidate)
    packet = CXPPacket(
        type=PacketType.REFLECT,
        capability="reflect",
        payload=Payload(
            goal="Improve executor output",
            inputs={"target_role": "executor", "source_attempt_id": "attempt-1"},
        ),
    )

    result = await agent._execute(packet)

    agent.get_skill.assert_not_awaited()
    agent.llm.assert_not_awaited()
    put_candidate.assert_not_awaited()
    assert "platform-unhealthy" in result