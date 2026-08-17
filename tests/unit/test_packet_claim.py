"""_claim_packet()/_mark_packet_done() -- the idempotency fence added
2026-08-17 after a heartbeat-timing bug let JetStream redeliver a packet
to a second replica while the first was still (or had already) run it to
completion, producing duplicate "done" results and duplicate side effects
(confirmed live: the same packet id completed 4-5 times, ~5-6 minutes
apart). The fence is what actually has to hold regardless of whether that
exact timing bug ever gets fully root-caused -- JetStream redelivery is
always possible by design.

A second incident (also 2026-08-17) showed marking a claim done isn't
enough on its own -- deleting/releasing it made a finished packet
indistinguishable from "never claimed", so a redelivery arriving well
after the original completed (triggered by an ack() that apparently
didn't register) was treated as brand new work. The "done" status +
DONE_CLAIM_RETENTION_SECONDS tests below cover that specifically."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

from src.agent_shell import KV_INFLIGHT, INFLIGHT_STALE_SECONDS, DONE_CLAIM_RETENTION_SECONDS

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


async def test_done_claim_is_protected_from_immediate_reclaim(agent, fake_kv):
    """This is the opposite of the old (buggy) expectation: marking a
    packet done must NOT free it up for immediate re-execution -- a
    redelivery of the same message arriving right after completion is
    exactly the scenario that produced real duplicate work live."""
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True
    await agent._mark_packet_done(PACKET_ID)
    assert await agent._claim_packet(PACKET_ID) is False


async def test_done_claim_still_protected_past_the_in_progress_stale_window(agent, fake_kv):
    """Regression test for the exact live incident: duplicates landed
    ~960-1080s after the original completion -- right past
    INFLIGHT_STALE_SECONDS (960s). A "done" marker must use its own, much
    longer retention window, not the short in-progress one, or a
    redelivery landing in that gap reclaims it as if abandoned."""
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True
    await agent._mark_packet_done(PACKET_ID)

    entry = await fake_kv.get(PACKET_ID)
    claim = json.loads(entry.value.decode())
    claim["ts"] = time.time() - INFLIGHT_STALE_SECONDS - 120
    await fake_kv.update(PACKET_ID, json.dumps(claim).encode(), last=entry.revision)

    assert await agent._claim_packet(PACKET_ID) is False


async def test_very_old_done_claim_is_eventually_pruned(agent, fake_kv):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True
    await agent._mark_packet_done(PACKET_ID)

    entry = await fake_kv.get(PACKET_ID)
    claim = json.loads(entry.value.decode())
    claim["ts"] = time.time() - DONE_CLAIM_RETENTION_SECONDS - 10
    await fake_kv.update(PACKET_ID, json.dumps(claim).encode(), last=entry.revision)

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


async def test_unreadable_existing_claim_fails_closed_not_open(agent, fake_kv, monkeypatch):
    """Regression test for a real bug found live 2026-08-17: a claim
    already exists (create() failed), but reading it to check staleness
    then hits a transient error -- this used to fail OPEN (return True,
    "safe to execute"), which is backwards, since the failed create()
    already proved someone else holds this claim. Disproportionately hit
    the assess capability (highest-throughput agent, most likely to see a
    transient JetStream read blip under load), producing real duplicate
    "done" completions despite the fence supposedly being in place."""
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True  # someone else holds it

    async def always_fails(key):
        raise ConnectionError("simulated transient JetStream read blip")

    monkeypatch.setattr(fake_kv, "get", always_fails)
    monkeypatch.setattr("src.agent_shell.asyncio.sleep", AsyncMock())

    assert await agent._claim_packet(PACKET_ID) is False


async def test_transient_read_error_recovers_on_retry(agent, fake_kv, monkeypatch):
    agent._kv_cache[KV_INFLIGHT] = fake_kv
    assert await agent._claim_packet(PACKET_ID) is True

    real_get = fake_kv.get
    calls = {"n": 0}

    async def flaky_get(key):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("simulated transient blip")
        return await real_get(key)

    monkeypatch.setattr(fake_kv, "get", flaky_get)
    monkeypatch.setattr("src.agent_shell.asyncio.sleep", AsyncMock())

    # The existing claim is still live (not stale), so once the read
    # actually succeeds on retry, this must correctly reject -- the retry
    # exists to rescue a transient blip, not to change the outcome.
    assert await agent._claim_packet(PACKET_ID) is False
    assert calls["n"] >= 2
