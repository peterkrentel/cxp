"""#85: assess/deploy packets were tagged type=PacketType.REFLECT -- the same
value as a real reflect packet -- relying entirely on the separate
`capability` field to tell them apart. Confirmed live 2026-08-30 (task
f8437c9d, packet aa045b96): a real assess packet showed
{"type": "reflect", "capability": "assess"} in /api/state, genuinely
confusing when reading raw packet data. Nothing routes on `packet.type`
(routing is by capability subject, cxp.cap.{capability}) -- the only
consumers are cosmetic (web dashboard display) or already keyed off
capability (run_tests.py) -- so giving assess/deploy their own real
PacketType values is a safe, non-functional-behavior change.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.contracts import VerificationResult
from src.packet import CXPPacket, PacketType, Payload


async def test_verifier_spawns_assess_packet_with_its_own_type(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="raw verdict"))
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    monkeypatch.setattr(agent, "record_attempt", AsyncMock())
    monkeypatch.setattr(
        verifier_module, "parse_contract",
        lambda *_: VerificationResult(score=0.5, passed=False, issues=[], suggestion="none"),
    )
    emitted = []
    monkeypatch.setattr(agent, "emit_packet", AsyncMock(side_effect=lambda p: emitted.append(p)))

    packet = CXPPacket(type=PacketType.VERIFY, capability="verify",
                        payload=Payload(goal="verify code", context="artifact"))
    await agent._execute(packet)

    assess_packets = [p for p in emitted if p.capability == "assess"]
    assert len(assess_packets) == 1
    assert assess_packets[0].type == PacketType.ASSESS


async def test_verifier_spawns_deploy_packet_with_its_own_type(monkeypatch):
    from src.agents import verifier as verifier_module
    from src.agents.verifier import VerifierAgent

    agent = VerifierAgent()
    monkeypatch.setattr(agent, "get_skill", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "llm", AsyncMock(return_value="raw verdict"))
    monkeypatch.setattr(agent._memory, "save", AsyncMock())
    monkeypatch.setattr(agent, "record_attempt", AsyncMock())
    monkeypatch.setattr(
        verifier_module, "parse_contract",
        lambda *_: VerificationResult(score=0.9, passed=True, issues=[], suggestion="none"),
    )
    emitted = []
    monkeypatch.setattr(agent, "emit_packet", AsyncMock(side_effect=lambda p: emitted.append(p)))

    packet = CXPPacket(type=PacketType.VERIFY, capability="verify",
                        payload=Payload(goal="verify code", context="artifact"))
    await agent._execute(packet)

    deploy_packets = [p for p in emitted if p.capability == "deploy"]
    assert len(deploy_packets) == 1
    assert deploy_packets[0].type == PacketType.DEPLOY


def test_build_submission_packet_maps_assess_and_deploy_to_their_own_type():
    from src.web_dashboard import build_submission_packet

    assess = build_submission_packet({"goal": "x", "capability": "assess"})
    deploy = build_submission_packet({"goal": "x", "capability": "deploy"})

    assert assess.type == PacketType.ASSESS
    assert deploy.type == PacketType.DEPLOY
