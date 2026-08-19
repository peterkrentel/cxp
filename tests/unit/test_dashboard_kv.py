"""get_halt()/get_tier_status() -- web_dashboard.py's KV reads had zero test
coverage despite needing nothing new: FakeKV already reproduces the get/put
interface both functions call, the same fixture proven on agent_shell.py's
KV_OLLAMA_SLOTS/KV_INFLIGHT buckets. Seeding _kv_cache directly (same trick
test_ollama_slots.py/test_packet_claim.py use) skips the real NATS connect
entirely -- _nc only needs to be truthy to pass these functions' own
connectivity guard, it's never actually touched once the cache is warm."""

from __future__ import annotations

import json

from src import web_dashboard
from src.web_dashboard import KV_STATE, get_halt, get_tier_status


async def test_get_halt_returns_none_when_not_connected(monkeypatch):
    monkeypatch.setattr(web_dashboard, "_nc", None)
    assert await get_halt() is None


async def test_get_halt_returns_none_when_key_absent(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_STATE, fake_kv)
    assert await get_halt() is None


async def test_get_halt_returns_the_record_when_halted(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_STATE, fake_kv)
    await fake_kv.put("halt", json.dumps({"halted": True, "reason": "x"}).encode())
    halt = await get_halt()
    assert halt == {"halted": True, "reason": "x"}


async def test_get_halt_returns_none_when_stored_but_not_halted(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_STATE, fake_kv)
    await fake_kv.put("halt", json.dumps({"halted": False}).encode())
    assert await get_halt() is None


async def test_get_tier_status_returns_none_when_not_connected(monkeypatch):
    monkeypatch.setattr(web_dashboard, "_nc", None)
    assert await get_tier_status() is None


async def test_get_tier_status_returns_none_when_key_absent(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_STATE, fake_kv)
    assert await get_tier_status() is None


async def test_get_tier_status_returns_the_published_payload(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_STATE, fake_kv)
    payload = {"streaks": {"0": 3, "1": 0, "2": 0}, "active_tier": 0, "top_tier": 2, "streak_target": 10}
    await fake_kv.put("tier-status", json.dumps(payload).encode())
    assert await get_tier_status() == payload
