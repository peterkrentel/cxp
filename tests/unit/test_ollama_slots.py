"""acquire_ollama_slot()/release_ollama_slot() -- the JetStream KV semaphore
added to stop multiple agents racing the same Ollama instance's real
concurrency limit. These are the CAS/blocking/stale-claim-pruning
behaviors that were only ever verified by hand, live, in the cluster."""

from __future__ import annotations

import asyncio
import time

import pytest

from src import agent_shell
from src.agent_shell import KV_OLLAMA_SLOTS, OLLAMA_SLOT_STALE_SECONDS

URL = "http://ollama:11434"


async def _with_kv(agent, fake_kv):
    agent._kv_cache[KV_OLLAMA_SLOTS] = fake_kv
    return agent


async def test_acquire_succeeds_immediately_under_capacity(agent, fake_kv):
    await _with_kv(agent, fake_kv)
    claim_id = await agent.acquire_ollama_slot(URL, max_slots=2)
    assert isinstance(claim_id, str) and claim_id


async def test_acquire_blocks_when_at_capacity_then_unblocks_on_release(agent, fake_kv, monkeypatch):
    monkeypatch.setattr(agent_shell, "OLLAMA_SLOT_POLL_SECONDS", 0.01)
    await _with_kv(agent, fake_kv)

    first = await agent.acquire_ollama_slot(URL, max_slots=1)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agent.acquire_ollama_slot(URL, max_slots=1), timeout=0.1)

    await agent.release_ollama_slot(URL, first)

    second = await asyncio.wait_for(agent.acquire_ollama_slot(URL, max_slots=1), timeout=0.5)
    assert second != first


async def test_stale_claim_is_pruned_and_slot_reclaimed(agent, fake_kv):
    await _with_kv(agent, fake_kv)
    key = agent._ollama_slot_key(URL)
    stale_ts = time.time() - OLLAMA_SLOT_STALE_SECONDS - 10
    import json
    await fake_kv.create(key, json.dumps([{"id": "crashed-holder", "ts": stale_ts}]).encode())

    # max_slots=1: the only existing claim is stale, so this must succeed
    # immediately rather than treat the slot as permanently occupied by a
    # holder that crashed without releasing it.
    claim_id = await asyncio.wait_for(agent.acquire_ollama_slot(URL, max_slots=1), timeout=0.5)
    assert claim_id != "crashed-holder"


async def test_release_removes_only_its_own_claim(agent, fake_kv):
    await _with_kv(agent, fake_kv)
    first = await agent.acquire_ollama_slot(URL, max_slots=2)
    second = await agent.acquire_ollama_slot(URL, max_slots=2)

    await agent.release_ollama_slot(URL, first)

    import json
    key = agent._ollama_slot_key(URL)
    entry = await fake_kv.get(key)
    remaining_ids = [c["id"] for c in json.loads(entry.value.decode())]
    assert remaining_ids == [second]
