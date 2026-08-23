"""is_halted()/set_halt()/clear_halt() and get_skill_with_revision()/put_skill()
-- KV_STATE and KV_SKILLS logic that has run in production since early in
this project but, unlike the adjacent KV_OLLAMA_SLOTS and KV_INFLIGHT
buckets in this same file, was never covered by FakeKV -- despite needing
nothing new to test, since FakeKV already reproduces the get/put interface
these functions call."""

from __future__ import annotations

import json

from src.agent_shell import KV_SKILLS, KV_STATE


async def test_is_halted_returns_none_when_key_absent(agent, fake_kv):
    agent._kv_cache[KV_STATE] = fake_kv
    assert await agent.is_halted() is None


async def test_set_halt_then_is_halted_round_trips(agent, fake_kv):
    agent._kv_cache[KV_STATE] = fake_kv
    await agent.set_halt("something broke", task_id="t1")
    halt = await agent.is_halted()
    assert halt is not None
    assert halt["halted"] is True
    assert halt["reason"] == "something broke"
    assert halt["task_id"] == "t1"
    assert halt["agent"] == agent.agent_id


async def test_clear_halt_makes_is_halted_return_none_again(agent, fake_kv):
    agent._kv_cache[KV_STATE] = fake_kv
    await agent.set_halt("something broke")
    assert await agent.is_halted() is not None
    await agent.clear_halt()
    assert await agent.is_halted() is None


async def test_is_halted_treats_a_stored_but_not_halted_record_as_not_halted(agent, fake_kv):
    # exercises the same "halted: false" shape clear_halt() writes, without
    # going through clear_halt() itself, in case that ever changes
    agent._kv_cache[KV_STATE] = fake_kv
    await fake_kv.put("halt", json.dumps({"halted": False}).encode())
    assert await agent.is_halted() is None


async def test_get_skill_with_revision_returns_stored_content_and_revision(agent, fake_kv):
    agent._kv_cache[KV_SKILLS] = fake_kv
    rev = await agent.put_skill("example-skill", "skill file contents")
    content, revision = await agent.get_skill_with_revision("example-skill")
    assert content == "skill file contents"
    assert revision == rev


async def test_get_skill_with_revision_falls_back_to_file_when_key_absent(agent, fake_kv, tmp_path):
    agent._kv_cache[KV_SKILLS] = fake_kv
    fallback = tmp_path / "fallback.yaml"
    fallback.write_text("fallback content")
    content, revision = await agent.get_skill_with_revision("missing-skill", str(fallback))
    assert content == "fallback content"
    assert revision is None


async def test_get_skill_with_revision_returns_empty_when_absent_and_no_fallback(agent, fake_kv):
    agent._kv_cache[KV_SKILLS] = fake_kv
    content, revision = await agent.get_skill_with_revision("missing-skill")
    assert content == ""
    assert revision is None


async def test_put_skill_overwrite_is_visible_to_a_later_get(agent, fake_kv):
    agent._kv_cache[KV_SKILLS] = fake_kv
    await agent.put_skill("example-skill", "version one")
    await agent.put_skill("example-skill", "version two")
    content, _ = await agent.get_skill_with_revision("example-skill")
    assert content == "version two"


async def test_get_or_create_kv_creates_the_bucket_when_it_does_not_exist_yet():
    from nats.js.errors import NotFoundError

    from src.agent_shell import get_or_create_kv

    created = {}

    class FakeJS:
        async def key_value(self, bucket):
            if bucket not in created:
                raise NotFoundError()
            return created[bucket]

        async def create_key_value(self, bucket):
            created[bucket] = f"kv-{bucket}"
            return created[bucket]

    # Every hourly CronJob run must be able to read a candidate-evaluation
    # bucket that no reflect run has ever staged yet, instead of crashing
    # with an uncaught NotFoundError until the first candidate exists.
    kv = await get_or_create_kv(FakeJS(), "cxp-skill-candidates")

    assert kv == "kv-cxp-skill-candidates"


async def test_get_or_create_kv_recovers_from_a_lost_create_race():
    from nats.js.errors import BadRequestError, NotFoundError

    from src.agent_shell import get_or_create_kv

    class FakeJS:
        async def key_value(self, bucket):
            return f"kv-{bucket}"

        async def create_key_value(self, bucket):
            raise BadRequestError()

    kv = await get_or_create_kv(FakeJS(), "cxp-skill-candidates")

    assert kv == "kv-cxp-skill-candidates"
