"""Memory and reputation store backed by a local JSON file."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

MEMORY_PATH = os.environ.get("CXP_MEMORY_PATH", "/data/memory.json")


@dataclass
class AgentReputation:
    agent_id: str
    capability: str
    successes: int = 0
    failures: int = 0

    @property
    def score(self) -> float:
        total = self.successes + self.failures
        return self.successes / total if total else 0.5

    @property
    def total(self) -> int:
        return self.successes + self.failures


@dataclass
class MemoryStore:
    # reputation[agent_id][capability] = AgentReputation
    reputation: dict[str, dict[str, AgentReputation]] = field(default_factory=lambda: defaultdict(dict))
    # episodic: last N task summaries
    episodic: list[dict[str, Any]] = field(default_factory=list)
    # semantic: stable facts extracted by reflect agent
    semantic: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def record_success(self, agent_id: str, capability: str) -> None:
        rep = self._get_or_create(agent_id, capability)
        rep.successes += 1

    def record_failure(self, agent_id: str, capability: str) -> None:
        rep = self._get_or_create(agent_id, capability)
        rep.failures += 1

    def best_agent(self, capability: str, candidates: list[str]) -> str | None:
        """Return the highest-reputation agent for a capability from a candidate list."""
        scored = [
            (a, self.reputation.get(a, {}).get(capability, AgentReputation(a, capability)).score)
            for a in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0] if scored else None

    def add_episodic(self, summary: dict[str, Any]) -> None:
        self.episodic.append(summary)
        if len(self.episodic) > 200:
            self.episodic = self.episodic[-200:]

    def add_semantic(self, fact: str) -> None:
        if fact not in self.semantic:
            self.semantic.append(fact)

    def all_reputations(self) -> list[AgentReputation]:
        out = []
        for cap_map in self.reputation.values():
            out.extend(cap_map.values())
        return out

    def _get_or_create(self, agent_id: str, capability: str) -> AgentReputation:
        if capability not in self.reputation[agent_id]:
            self.reputation[agent_id][capability] = AgentReputation(agent_id, capability)
        return self.reputation[agent_id][capability]

    async def save(self) -> None:
        async with self._lock:
            os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
            data = {
                "reputation": {
                    aid: {cap: {"successes": r.successes, "failures": r.failures}
                          for cap, r in caps.items()}
                    for aid, caps in self.reputation.items()
                },
                "episodic": self.episodic[-50:],
                "semantic": self.semantic,
            }
            with open(MEMORY_PATH, "w") as f:
                json.dump(data, f, indent=2)

    @classmethod
    def load(cls) -> "MemoryStore":
        store = cls()
        if not os.path.exists(MEMORY_PATH):
            return store
        try:
            with open(MEMORY_PATH) as f:
                data = json.load(f)
            for aid, caps in data.get("reputation", {}).items():
                for cap, vals in caps.items():
                    rep = AgentReputation(aid, cap, vals["successes"], vals["failures"])
                    store.reputation[aid][cap] = rep
            store.episodic = data.get("episodic", [])
            store.semantic = data.get("semantic", [])
        except Exception:
            pass
        return store


# module-level singleton
_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore.load()
    return _store
