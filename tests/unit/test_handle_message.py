"""AgentShell._handle_message() -- the full per-packet pipeline (claim,
heartbeat, execute, publish, ack, release). Confirmed live 2026-08-17: a
single-replica planner completed 4 packets twice each, ~10-15 minutes
apart. Root cause was the claim being released in the `finally` block,
before _publish/_emit_status/msg.ack() ran -- if that post-execute work
was ever slow enough to run past the remaining ack_wait budget (the
heartbeat had already stopped by then), JetStream could redeliver, and
the redelivered copy found no claim in its way and reran the whole task.
The fix moves the release to after msg.ack() -- these tests pin that
ordering down directly instead of relying on live observation again."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent_shell import KV_INFLIGHT, KV_STATE
from src.packet import CXPPacket, PacketType, Payload


class FakeMsg:
    def __init__(self, packet: CXPPacket) -> None:
        self.data = packet.model_dump_json().encode()
        self.ack = AsyncMock()
        self.in_progress = AsyncMock()


def _wire(agent, fake_kv, monkeypatch):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    agent._kv_cache[KV_STATE] = fake_kv
    monkeypatch.setattr(agent, "is_halted", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_emit_status", AsyncMock())
    monkeypatch.setattr(agent, "_think", AsyncMock())
    monkeypatch.setattr(agent, "_publish", AsyncMock())


async def test_claim_is_still_held_when_publish_and_ack_run(agent, fake_kv, monkeypatch):
    """The core regression: while _publish/ack are in flight (i.e. exactly
    the window the real bug lived in), a second delivery of the same
    packet must still be rejected by the fence -- not just during
    _execute()."""
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)

    claim_still_held_during_ack = {}

    async def fake_execute(p):
        return "done"

    async def spying_publish(subject, p):
        claim_still_held_during_ack["result"] = await agent._claim_packet(packet.id)

    monkeypatch.setattr(agent, "_execute", fake_execute)
    monkeypatch.setattr(agent, "_publish", spying_publish)

    await agent._handle_message(msg)

    assert claim_still_held_during_ack["result"] is False
    msg.ack.assert_awaited_once()


async def test_claim_is_released_after_ack_so_a_later_genuine_retry_can_proceed(agent, fake_kv, monkeypatch):
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)
    monkeypatch.setattr(agent, "_execute", AsyncMock(return_value="done"))

    await agent._handle_message(msg)

    # Once the message is actually acked, the claim must be gone -- a
    # brand new packet with this id (or a deliberate resubmission) should
    # be claimable again, rather than the fence leaking forever.
    assert await agent._claim_packet(packet.id) is True


async def test_duplicate_redelivery_of_a_still_running_packet_is_acked_without_reexecuting(agent, fake_kv, monkeypatch):
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg1 = FakeMsg(packet)
    msg2 = FakeMsg(packet)

    execute_calls = []

    async def fake_execute(p):
        execute_calls.append(p.id)
        return "done"

    monkeypatch.setattr(agent, "_execute", fake_execute)

    await agent._handle_message(msg1)
    # A redelivery of the identical packet id arriving after the first
    # already completed and released its claim (the legitimate, expected
    # case -- e.g. a deliberate resubmission) is a separate scenario from
    # a redelivery arriving *during* processing, covered by the first test
    # above. Here we simulate the "still claimed" case directly.
    await agent._claim_packet(packet.id)  # re-claim to simulate msg1 still in flight
    await agent._handle_message(msg2)

    assert execute_calls == [packet.id]  # msg2 was rejected by the fence, never executed
    msg2.ack.assert_awaited_once()


async def test_halted_swarm_drops_packet_without_ever_claiming_it(agent, fake_kv, monkeypatch):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    agent._kv_cache[KV_STATE] = fake_kv
    monkeypatch.setattr(agent, "is_halted", AsyncMock(return_value={"reason": "boom"}))
    monkeypatch.setattr(agent, "_execute", AsyncMock(return_value="done"))
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)

    await agent._handle_message(msg)

    msg.ack.assert_awaited_once()
    agent._execute.assert_not_called()
    # Never claimed -- a human clearing the halt later shouldn't find this
    # packet id already (falsely) marked in-flight.
    assert await agent._claim_packet(packet.id) is True
