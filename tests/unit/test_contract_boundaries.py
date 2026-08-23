"""Verifier and assessor must consume shared output contracts."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from src.contracts import ArtifactResult, AssessmentResult, ContractParseError, VerificationResult
from src.packet import CXPPacket, PacketType, Payload


async def test_verifier_delegates_raw_output_to_verification_contract(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="raw verdict"))
    monkeypatch.setattr(agent, "emit_packet", AsyncMock())
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    recorded = AsyncMock()
    monkeypatch.setattr(agent, "record_attempt", recorded)
    calls = []

    def fake_parse_contract(capability, raw_text):
        calls.append((capability, raw_text))
        return VerificationResult(score=0.8, passed=True, issues=[], suggestion="none")

    monkeypatch.setattr(verifier_module, "parse_contract", fake_parse_contract)
    packet = CXPPacket(
        type=PacketType.VERIFY,
        capability="verify",
        payload=Payload(goal="verify code", context="artifact"),
    )

    output = await agent._execute(packet)

    assert calls == [("verify", "raw verdict")]
    assert json.loads(output)["score"] == 0.8
    evidence = recorded.await_args.kwargs
    assert evidence["validation_status"] == "valid"
    assert evidence["raw_response"] == "raw verdict"
    assert '"score":0.8' in evidence["normalized_response"]


async def test_assessor_delegates_raw_output_to_assessment_contract(monkeypatch):
    from src.agents import assessor as assessor_module
    from src.agents.assessor import AssessorAgent

    agent = AssessorAgent()
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="raw assessment"))
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    recorded = AsyncMock()
    monkeypatch.setattr(agent, "record_attempt", recorded)
    calls = []

    def fake_parse_contract(capability, raw_text):
        calls.append((capability, raw_text))
        return AssessmentResult(
            labels=["CODE_GENERATION"],
            verdict="valid artifact",
            strengths=["structured"],
            gaps=[],
        )

    monkeypatch.setattr(assessor_module, "parse_contract", fake_parse_contract)
    packet = CXPPacket(
        type=PacketType.REFLECT,
        capability="assess",
        payload=Payload(goal="assess code", context="artifact"),
    )

    output = await agent._execute(packet)

    assert calls == [("assess", "raw assessment")]
    assert json.loads(output)["labels"] == ["CODE_GENERATION"]
    evidence = recorded.await_args.kwargs
    assert evidence["validation_status"] == "valid"
    assert evidence["raw_response"] == "raw assessment"
    assert '"labels":["CODE_GENERATION"]' in evidence["normalized_response"]


async def test_verifier_marks_malformed_contract_evidence_as_error(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="broken verdict"))
    monkeypatch.setattr(agent, "emit_packet", AsyncMock())
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    recorded = AsyncMock()
    monkeypatch.setattr(agent, "record_attempt", recorded)
    monkeypatch.setattr(
        verifier_module,
        "parse_contract",
        lambda *_args: (_ for _ in ()).throw(ContractParseError("verify contract validation failed")),
    )
    packet = CXPPacket(
        type=PacketType.VERIFY,
        capability="verify",
        payload=Payload(goal="verify code", context="artifact"),
    )

    await agent._execute(packet)

    evidence = recorded.await_args.kwargs
    assert evidence["validation_status"] == "contract_error"
    assert evidence["validation_issues"] == ["verify contract validation failed"]


async def test_assessor_marks_malformed_contract_evidence_as_error(monkeypatch):
    from src.agents import assessor as assessor_module
    from src.agents.assessor import AssessorAgent

    agent = AssessorAgent()
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="broken assessment"))
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    recorded = AsyncMock()
    monkeypatch.setattr(agent, "record_attempt", recorded)
    monkeypatch.setattr(
        assessor_module,
        "parse_contract",
        lambda *_args: (_ for _ in ()).throw(ContractParseError("assess contract validation failed")),
    )
    packet = CXPPacket(
        type=PacketType.REFLECT,
        capability="assess",
        payload=Payload(goal="assess code", context="artifact"),
    )

    await agent._execute(packet)

    evidence = recorded.await_args.kwargs
    assert evidence["validation_status"] == "contract_error"
    assert evidence["validation_issues"] == ["assess contract validation failed"]


async def test_executor_normalizes_artifact_before_verification(monkeypatch):
    from src.agents import executor as executor_module
    from src.agents.executor import ExecutorAgent

    agent = ExecutorAgent()
    monkeypatch.setattr(agent, "get_skill_with_revision", AsyncMock(return_value=("", 7)))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="```yaml\nkind: ConfigMap\n```"))
    emitted = AsyncMock()
    monkeypatch.setattr(agent, "emit_packet", emitted)
    recorded = AsyncMock()
    monkeypatch.setattr(agent, "record_attempt", recorded)
    calls = []

    def fake_parse_contract(capability, raw_text):
        calls.append((capability, raw_text))
        return ArtifactResult(content="kind: ConfigMap", format="yaml")

    monkeypatch.setattr(executor_module, "parse_contract", fake_parse_contract)
    packet = CXPPacket(
        type=PacketType.CODE,
        capability="code",
        payload=Payload(goal="write YAML", instructions="return a ConfigMap"),
    )

    output = await agent._execute(packet)

    assert calls == [("code", "```yaml\nkind: ConfigMap\n```")]
    assert output == "kind: ConfigMap"
    verify_packet = emitted.await_args.args[0]
    assert verify_packet.payload.context == "kind: ConfigMap"
    evidence = recorded.await_args.kwargs
    assert evidence["raw_response"].startswith("```yaml")
    assert evidence["normalized_response"] == "kind: ConfigMap"
    assert evidence["skill_revision"] == 7


async def test_verifier_defaults_score_to_neutral_when_missing_from_a_valid_response(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value='{"passed": true}'))
    monkeypatch.setattr(agent, "emit_packet", AsyncMock())
    monkeypatch.setattr(agent, "record_attempt", AsyncMock())
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    monkeypatch.setattr(
        verifier_module,
        "parse_contract",
        lambda *_args: VerificationResult(passed=True, issues=[], suggestion=""),
    )
    packet = CXPPacket(
        type=PacketType.VERIFY,
        capability="verify",
        payload=Payload(goal="verify code", context="artifact"),
    )

    await agent._execute(packet)

    # A genuinely absent score must land on the neutral 0.5 fallback, not a
    # hard 0.0 fail -- a valid response missing one optional field is not
    # the same signal as a response that failed to parse at all.
    assert packet.quality_score == 0.5


async def test_verifier_skips_episodic_write_during_a_candidate_evaluation_run(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="raw verdict"))
    monkeypatch.setattr(agent, "emit_packet", AsyncMock())
    monkeypatch.setattr(agent, "record_attempt", AsyncMock())
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    add_episodic = AsyncMock()
    monkeypatch.setattr(agent._memory, "add_episodic", add_episodic)
    monkeypatch.setattr(
        verifier_module,
        "parse_contract",
        lambda *_args: VerificationResult(score=0.9, passed=True, issues=[], suggestion=""),
    )
    packet = CXPPacket(
        type=PacketType.VERIFY,
        capability="verify",
        payload=Payload(goal="verify code", context="artifact", inputs={"evaluation_run": True}),
    )

    await agent._execute(packet)

    # A candidate-comparison run must never write into the same episodic
    # history the regression detector reads -- a deliberately bad candidate
    # would otherwise corrupt the next real regression check.
    add_episodic.assert_not_called()


async def test_failed_verification_targets_executor_candidate(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="failed verdict"))
    emitted = AsyncMock()
    monkeypatch.setattr(agent, "emit_packet", emitted)
    monkeypatch.setattr(agent, "record_attempt", AsyncMock())
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    monkeypatch.setattr(
        verifier_module,
        "parse_contract",
        lambda *_args: VerificationResult(score=0.2, passed=False, issues=["missing test"], suggestion="add a test"),
    )
    packet = CXPPacket(
        type=PacketType.VERIFY,
        capability="verify",
        parent_packet_id="executor-attempt",
        payload=Payload(goal="verify code", context="artifact"),
    )

    await agent._execute(packet)

    reflect_packet = emitted.await_args_list[0].args[0]
    assert reflect_packet.capability == "reflect"
    assert reflect_packet.payload.inputs == {
        "target_role": "executor",
        "source_attempt_id": packet.id,
    }


async def test_executor_uses_staged_candidate_without_mutating_or_reading_active_skill(monkeypatch):
    from src.agents import executor as executor_module
    from src.agents.executor import ExecutorAgent

    agent = ExecutorAgent()
    active_skill = AsyncMock(return_value=("active skill", 7))
    monkeypatch.setattr(agent, "get_skill_with_revision", active_skill)
    monkeypatch.setattr(agent, "get_skill_candidate", AsyncMock(return_value={
        "target_role": "executor",
        "content": "candidate skill",
    }))
    llm = AsyncMock(return_value="candidate artifact")
    monkeypatch.setattr(agent, "llm", llm)
    monkeypatch.setattr(agent, "emit_packet", AsyncMock())
    monkeypatch.setattr(agent, "record_attempt", AsyncMock())
    monkeypatch.setattr(
        executor_module,
        "parse_contract",
        lambda *_args: ArtifactResult(content="candidate artifact", format="text"),
    )
    packet = CXPPacket(
        type=PacketType.CODE,
        capability="code",
        payload=Payload(
            goal="write code",
            instructions="return an artifact",
            inputs={"candidate_id": "candidate-1"},
        ),
    )

    await agent._execute(packet)

    active_skill.assert_not_awaited()
    assert "candidate skill" in llm.await_args.args[0]
    assert agent.record_attempt.await_args.kwargs["skill_revision"] == "candidate-1"