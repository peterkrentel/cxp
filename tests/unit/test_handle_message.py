"""AgentShell._handle_message() -- the full per-packet pipeline (claim,
heartbeat, execute, mark-done, publish, ack). Confirmed live 2026-08-17,
twice:

1. A single-replica planner completed 4 packets twice each, ~10-15
   minutes apart. Root cause was the claim being released in the
   `finally` block, before _publish/_emit_status/msg.ack() ran -- if
   that post-execute work was ever slow enough to run past the
   remaining ack_wait budget (the heartbeat had already stopped by
   then), JetStream could redeliver, and the redelivered copy found no
   claim in its way and reran the whole task.
2. Even after moving the release to after ack, several packets still
   completed twice, ~960-1080s apart -- right around
   INFLIGHT_STALE_SECONDS. Root cause: releasing (deleting) the claim
   after ack() made a finished packet indistinguishable from "never
   claimed" -- if ack() itself ever threw or silently failed to
   register, the release step never ran, and a later redelivery found a
   stale-looking claim and reclaimed it as an abandoned crash.

The fix: mark the packet done immediately after _execute() returns
(before publish/emit_status/ack), and never actually delete the claim --
a "done" marker is protected from reclaim for DONE_CLAIM_RETENTION_SECONDS
regardless of how the rest of the pipeline behaves afterward. These tests
pin that down directly instead of relying on live observation again."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from src.agent_shell import KV_INFLIGHT, KV_STATE, INFLIGHT_STALE_SECONDS
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


async def test_claim_stays_protected_after_handle_message_fully_completes(agent, fake_kv, monkeypatch):
    """The opposite of what an earlier version of this test asserted: once
    a packet has genuinely finished (published, acked), it must NOT be
    immediately reclaimable. That's precisely the live incident -- a
    redelivery of the identical message landing after completion (ack()
    apparently not registering) was previously treated as brand new work
    and fully re-executed."""
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)
    monkeypatch.setattr(agent, "_execute", AsyncMock(return_value="done"))

    await agent._handle_message(msg)

    assert await agent._claim_packet(packet.id) is False


async def test_packet_stays_marked_done_even_if_ack_itself_fails(agent, fake_kv, monkeypatch):
    """The actual failure mode this fix protects against: asserting
    immediately after ack() fails isn't enough to distinguish this from
    the old (buggy) ordering -- a *fresh* in-progress claim also looks
    "not reclaimable yet" for the unrelated reason that it isn't stale
    yet. This simulates real time passing (matching the ~960-1080s gap
    observed live) so it only passes if the packet was durably marked
    done *before* the failing ack() call, not after."""
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)
    msg.ack = AsyncMock(side_effect=ConnectionError("simulated ack failure"))
    monkeypatch.setattr(agent, "_execute", AsyncMock(return_value="done"))

    with pytest.raises(ConnectionError):
        await agent._handle_message(msg)

    entry = await fake_kv.get(packet.id)
    claim = json.loads(entry.value.decode())
    claim["ts"] = time.time() - INFLIGHT_STALE_SECONDS - 120
    await fake_kv.update(packet.id, json.dumps(claim).encode(), last=entry.revision)

    # If mark_packet_done ran before the failing ack(), the claim's
    # status is "done" and this rewound timestamp doesn't matter (done
    # claims use DONE_CLAIM_RETENTION_SECONDS, not INFLIGHT_STALE_SECONDS)
    # -- still correctly rejected. If it only ran after (the bug), the
    # claim is still "in_progress" and this rewound timestamp makes it
    # look exactly like an abandoned crash, wrongly reclaimable.
    assert await agent._claim_packet(packet.id) is False


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
    # msg1 finishing already leaves the packet marked "done" and protected
    # (see test_claim_stays_protected_after_handle_message_fully_completes)
    # -- a redelivery (msg2) landing any time afterward must be rejected
    # by that same protection, not treated as new work.
    assert await agent._claim_packet(packet.id) is False
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
