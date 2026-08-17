"""_claim_packet()/_release_packet_claim() -- the idempotency fence added
2026-08-17 after a heartbeat-timing bug let JetStream redeliver a packet
to a second replica while the first was still (or had already) run it to
completion, producing duplicate "done" results and duplicate side effects
(confirmed live: the same packet id completed 4-5 times, ~5-6 minutes
apart). The fence is what actually has to hold regardless of whether that
exact timing bug ever gets fully root-caused -- JetStream redelivery is
always possible by design."""

from __future__ import annotations

import time

from src.agent_shell import KV_INFLIGHT, INFLIGHT_STALE_SECONDS

PACKET_ID = "11111111-1111-1111-1111-111111111111"


async def test_first_claim_succeeds(agent, fake_kv):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True


async def test_second_claim_of_same_live_packet_fails(agent, fake_kv):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True
    # This is the exact scenario a JetStream redelivery creates: another
    # handle() invocation for the same packet id while the first is still
    # running. It must be told to back off, not execute a second time.
    assert await agent._claim_packet(PACKET_ID) is False


async def test_claim_succeeds_again_after_release(agent, fake_kv):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True
    await agent._release_packet_claim(PACKET_ID)
    assert await agent._claim_packet(PACKET_ID) is True


async def test_stale_claim_from_a_crashed_holder_is_reclaimed(agent, fake_kv):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    import json
    stale_ts = time.time() - INFLIGHT_STALE_SECONDS - 10
    await fake_kv.create(PACKET_ID, json.dumps({"agent": "some-dead-pod", "ts": stale_ts}).encode())

    # The original holder never released it (it crashed) -- a new delivery
    # a human resumes into must still be able to make progress instead of
    # being blocked on that claim forever.
    assert await agent._claim_packet(PACKET_ID) is True


async def test_a_still_running_claim_is_not_treated_as_stale(agent, fake_kv):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True
    # Simulate a redelivery arriving well within the legitimate processing
    # window (e.g. LLM_TOTAL_TIMEOUT can run up to 900s) -- must still be
    # rejected, not mistaken for an abandoned claim.
    assert await agent._claim_packet(PACKET_ID) is False
