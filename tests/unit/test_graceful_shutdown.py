"""AgentShell's response to a graceful shutdown (SIGTERM) -- e.g. a routine
rolling restart during `make deploy`.

Found live 2026-08-20: a rolling restart killed a planner replica mid-
_execute() on a genuinely healthy decomposition. Recovery had to wait out
the full INFLIGHT_STALE_SECONDS (960s) before another replica would treat
the claim as abandoned and retry -- a timer deliberately sized for a hard
crash with *zero* warning. But a rolling restart isn't that: Kubernetes
sends SIGTERM and waits out a termination grace period before SIGKILL,
which is enough time to hand the claim back cleanly instead of making the
next claimant wait out a timer built for the no-warning case. The test
harness's own per-test timeout (900s) is shorter than the 960s stale
window, so this gap reliably surfaces as a false TIMEOUT/PLANNER_FAILED
on the test that happened to be in flight during any deploy.

_release_for_shutdown() closes this: on SIGTERM, nak the in-flight
message (JetStream redelivers immediately, not after ack_wait) and delete
its KV_INFLIGHT claim (so the redelivered copy is claimable right away,
not read as still-in-progress) -- for every packet this replica is
currently, genuinely still working on. Safe to do even if this process's
own _execute() finishes anyway before SIGKILL: the idempotency fence
already tolerates two replicas briefly processing the same packet (see
_claim_packet's and _mark_packet_done's own docstrings)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from src.agent_shell import KV_INFLIGHT, KV_STATE
from src.packet import CXPPacket, PacketType, Payload


class FakeMsg:
    def __init__(self, packet: CXPPacket) -> None:
        self.data = packet.model_dump_json().encode()
        self.ack = AsyncMock()
        self.nak = AsyncMock()
        self.in_progress = AsyncMock()


def _wire(agent, fake_kv, monkeypatch):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    agent._kv_cache[KV_STATE] = fake_kv
    monkeypatch.setattr(agent, "is_halted", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_emit_status", AsyncMock())
    monkeypatch.setattr(agent, "_think", AsyncMock())
    monkeypatch.setattr(agent, "_publish", AsyncMock())


async def test_release_for_shutdown_frees_an_in_progress_claim_immediately(agent, fake_kv, monkeypatch):
    """The core fix: mid-_execute(), a graceful shutdown must free the
    claim right away -- not leave the next claimant waiting out
    INFLIGHT_STALE_SECONDS the way an unannounced crash would."""
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)

    still_processing = asyncio.Event()
    release_now = asyncio.Event()

    async def fake_execute(p):
        still_processing.set()
        await release_now.wait()
        return "done"

    monkeypatch.setattr(agent, "_execute", fake_execute)

    handle_task = asyncio.create_task(agent._handle_message(msg))
    await still_processing.wait()

    # Mid-execute: a second delivery must still be rejected -- the claim
    # is genuinely live right now.
    assert await agent._claim_packet(packet.id) is False

    await agent._release_for_shutdown()

    # Released immediately by the shutdown handler -- a fresh claim
    # succeeds right away, no staleness wait required.
    assert await agent._claim_packet(packet.id) is True
    msg.nak.assert_awaited_once()

    release_now.set()
    await handle_task


async def test_release_for_shutdown_does_not_touch_an_already_completed_packet(agent, fake_kv, monkeypatch):
    """Once a packet has already reached _mark_packet_done, a later
    _release_for_shutdown() call (e.g. triggered by a second, unrelated
    packet still in flight on the same replica) must leave it alone --
    nak'ing or deleting an already-done claim would strip the exact
    protection _mark_packet_done exists to provide."""
    _wire(agent, fake_kv, monkeypatch)
    packet = CXPPacket(type=PacketType.CODE, capability="code", payload=Payload(goal="do a thing"))
    msg = FakeMsg(packet)
    monkeypatch.setattr(agent, "_execute", AsyncMock(return_value="done"))

    await agent._handle_message(msg)
    msg.nak.reset_mock()

    await agent._release_for_shutdown()

    msg.nak.assert_not_called()
    assert await agent._claim_packet(packet.id) is False


async def test_release_for_shutdown_with_nothing_in_flight_is_a_no_op(agent, fake_kv, monkeypatch):
    """No active claims at all (idle replica) -- must not error."""
    _wire(agent, fake_kv, monkeypatch)
    await agent._release_for_shutdown()
