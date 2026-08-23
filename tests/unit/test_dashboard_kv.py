"""get_halt()/get_tier_status() -- web_dashboard.py's KV reads had zero test
coverage despite needing nothing new: FakeKV already reproduces the get/put
interface both functions call, the same fixture proven on agent_shell.py's
KV_OLLAMA_SLOTS/KV_INFLIGHT buckets. Seeding _kv_cache directly (same trick
test_ollama_slots.py/test_packet_claim.py use) skips the real NATS connect
entirely -- _nc only needs to be truthy to pass these functions' own
connectivity guard, it's never actually touched once the cache is warm."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from src import web_dashboard
from src.web_dashboard import (
    KV_CANDIDATE_EVALUATIONS,
    KV_SKILL_CANDIDATES,
    KV_SKILLS,
    KV_STATE,
    get_candidate_evaluations,
    get_candidate_evaluation,
    get_halt,
    get_tier_status,
    promote_candidate,
    get_state,
)


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


async def test_get_candidate_evaluation_reads_report(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_CANDIDATE_EVALUATIONS, fake_kv)
    report = {"candidate_id": "candidate-1", "recommendation": "recommend_promotion"}
    await fake_kv.put("candidate-1", json.dumps(report).encode())

    assert await get_candidate_evaluation("candidate-1") == report


async def test_get_candidate_evaluations_lists_stored_reports(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_CANDIDATE_EVALUATIONS, fake_kv)
    await fake_kv.put("candidate-b", json.dumps({"recommendation": "reject_regression"}).encode())
    await fake_kv.put("candidate-a", json.dumps({"recommendation": "recommend_promotion"}).encode())

    assert await get_candidate_evaluations() == [
        {"candidate_id": "candidate-a", "recommendation": "recommend_promotion"},
        {"candidate_id": "candidate-b", "recommendation": "reject_regression"},
    ]


async def test_promote_candidate_requires_promotion_recommendation(monkeypatch, fake_kv):
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_SKILL_CANDIDATES, fake_kv)
    monkeypatch.setitem(web_dashboard._kv_cache, KV_CANDIDATE_EVALUATIONS, fake_kv)
    monkeypatch.setitem(web_dashboard._kv_cache, KV_SKILLS, fake_kv)
    await fake_kv.put("candidate-1", json.dumps({
        "target_role": "executor", "content": "candidate skill",
    }).encode())
    await fake_kv.put("candidate-1", json.dumps({
        "recommendation": "reject_regression",
    }).encode())

    try:
        await promote_candidate("candidate-1")
    except ValueError as exc:
        assert "not recommended" in str(exc)
    else:
        raise AssertionError("promotion unexpectedly succeeded")


async def test_promote_candidate_writes_approved_content_to_active_skill_bucket(monkeypatch, fake_kv):
    candidates = type(fake_kv)()
    reports = type(fake_kv)()
    active_skills = type(fake_kv)()
    monkeypatch.setattr(web_dashboard, "_nc", object())
    monkeypatch.setitem(web_dashboard._kv_cache, KV_SKILL_CANDIDATES, candidates)
    monkeypatch.setitem(web_dashboard._kv_cache, KV_CANDIDATE_EVALUATIONS, reports)
    monkeypatch.setitem(web_dashboard._kv_cache, KV_SKILLS, active_skills)
    await candidates.put("candidate-1", json.dumps({
        "target_role": "executor", "content": "candidate skill",
    }).encode())
    await reports.put("candidate-1", json.dumps({
        "recommendation": "recommend_promotion",
    }).encode())

    result = await promote_candidate("candidate-1")

    assert result["target_role"] == "executor"
    assert (await active_skills.get("executor")).value == b"candidate skill"
    promoted_report = json.loads((await reports.get("candidate-1")).value.decode())
    assert promoted_report["promotion"]["revision"] == result["revision"]
    assert promoted_report["promotion"]["target_role"] == "executor"


async def test_state_includes_candidate_evaluation_reports(monkeypatch):
    reports = [{"candidate_id": "candidate-1", "recommendation": "recommend_promotion"}]
    monkeypatch.setattr(web_dashboard, "get_candidate_evaluations", AsyncMock(return_value=reports))
    monkeypatch.setattr(web_dashboard, "get_halt", AsyncMock(return_value=None))
    monkeypatch.setattr(web_dashboard, "get_stream_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(web_dashboard, "get_tier_status", AsyncMock(return_value=None))

    response = await get_state()
    payload = json.loads(response.body)

    assert payload["candidate_evaluations"] == reports
