"""Shared fixtures for the unit test suite.

These tests exercise AgentShell's control-plane logic (the ollama
semaphore, the packet idempotency fence, diagnostician's classification)
without a live NATS/JetStream server. FakeKV below reproduces just enough
of nats-py's KeyValue CAS semantics (create fails if the key exists,
update fails on a revision mismatch) for that logic to run unmodified
against it -- the same interface AgentShell._kv() hands back from a real
JetStream bucket.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agent_shell import AgentShell  # noqa: E402


class _Entry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class FakeKV:
    """In-memory stand-in for a nats.js.kv.KeyValue bucket."""

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._seq = 0

    async def get(self, key: str) -> _Entry:
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]

    async def put(self, key: str, value: bytes) -> int:
        self._seq += 1
        self._data[key] = _Entry(value, self._seq)
        return self._seq

    async def create(self, key: str, value: bytes) -> int:
        if key in self._data:
            raise RuntimeError(f"key already exists: {key}")
        return await self.put(key, value)

    async def update(self, key: str, value: bytes, last: int) -> int:
        entry = self._data.get(key)
        if entry is None or entry.revision != last:
            raise RuntimeError(f"wrong last revision for {key}")
        self._seq += 1
        self._data[key] = _Entry(value, self._seq)
        return self._seq

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class DummyAgent(AgentShell):
    """Minimal concrete AgentShell -- _execute is never called by these
    tests, only the shared control-plane helper methods."""

    async def _execute(self, packet):  # pragma: no cover - not exercised
        raise NotImplementedError


@pytest.fixture
def agent(monkeypatch):
    a = DummyAgent("test-agent-1", capabilities=["code"])
    # record_validation_failure/etc. call self._memory.save(), which by
    # default writes to /data/memory.json -- doesn't exist outside a pod.
    monkeypatch.setattr(a._memory, "save", _noop_async)
    return a


async def _noop_async(*args, **kwargs) -> None:
    return None


@pytest.fixture
def fake_kv():
    return FakeKV()
