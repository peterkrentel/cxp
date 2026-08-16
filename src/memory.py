"""Memory and reputation store backed by a local JSON file."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

MEMORY_PATH = os.environ.get("CXP_MEMORY_PATH", "/data/memory.json")
LOCK_PATH = MEMORY_PATH + ".lock"


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
    # reputation[agent_id][capability] = AgentReputation — read cache, refreshed
    # from disk after every save(); the on-disk file is the shared truth.
    reputation: dict[str, dict[str, AgentReputation]] = field(default_factory=lambda: defaultdict(dict))
    # episodic: last N task summaries
    episodic: list[dict[str, Any]] = field(default_factory=list)
    # semantic: stable facts extracted by reflect agent
    semantic: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # this process's contribution since the last save() — merging deltas
    # (rather than writing the full in-memory snapshot) is what keeps
    # concurrent replicas sharing MEMORY_PATH from clobbering each other.
    _pending_rep: dict[tuple[str, str], list[int]] = field(default_factory=dict, repr=False)
    _pending_episodic: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _pending_semantic: list[str] = field(default_factory=list, repr=False)

    def record_success(self, agent_id: str, capability: str) -> None:
        self._bump(agent_id, capability, successes=1)

    def record_failure(self, agent_id: str, capability: str) -> None:
        self._bump(agent_id, capability, failures=1)

    def _bump(self, agent_id: str, capability: str, successes: int = 0, failures: int = 0) -> None:
        rep = self._get_or_create(agent_id, capability)
        rep.successes += successes
        rep.failures += failures
        delta = self._pending_rep.setdefault((agent_id, capability), [0, 0])
        delta[0] += successes
        delta[1] += failures

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
        self._pending_episodic.append(summary)
        if len(self.episodic) > 200:
            self.episodic = self.episodic[-200:]

    def add_semantic(self, fact: str) -> None:
        if fact not in self.semantic:
            self.semantic.append(fact)
            self._pending_semantic.append(fact)

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
        """Merge this process's pending deltas onto the current on-disk state
        under an flock, instead of overwriting with a stale in-memory
        snapshot — multiple agent replicas share MEMORY_PATH on one PVC."""
        async with self._lock:
            pending_rep, self._pending_rep = self._pending_rep, {}
            pending_episodic, self._pending_episodic = self._pending_episodic, []
            pending_semantic, self._pending_semantic = self._pending_semantic, []
            data = await asyncio.to_thread(
                self._locked_merge, pending_rep, pending_episodic, pending_semantic
            )
            self._apply_disk_state(data)

    def _locked_merge(
        self,
        pending_rep: dict[tuple[str, str], list[int]],
        pending_episodic: list[dict[str, Any]],
        pending_semantic: list[str],
    ) -> dict:
        os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
        with open(LOCK_PATH, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                data = self._read_disk()
                rep = data.setdefault("reputation", {})
                for (agent_id, capability), (succ, fail) in pending_rep.items():
                    counts = rep.setdefault(agent_id, {}).setdefault(capability, {"successes": 0, "failures": 0})
                    counts["successes"] += succ
                    counts["failures"] += fail
                episodic = data.setdefault("episodic", [])
                episodic.extend(pending_episodic)
                data["episodic"] = episodic[-200:]
                semantic = data.setdefault("semantic", [])
                for fact in pending_semantic:
                    if fact not in semantic:
                        semantic.append(fact)
                data["semantic"] = semantic
                with open(MEMORY_PATH, "w") as f:
                    json.dump(data, f, indent=2)
                return data
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    @staticmethod
    def _read_disk() -> dict:
        if not os.path.exists(MEMORY_PATH):
            return {"reputation": {}, "episodic": [], "semantic": []}
        try:
            with open(MEMORY_PATH) as f:
                return json.load(f)
        except Exception:
            return {"reputation": {}, "episodic": [], "semantic": []}

    def _apply_disk_state(self, data: dict) -> None:
        """Refresh the in-memory read cache from the merged on-disk truth."""
        reputation: dict[str, dict[str, AgentReputation]] = defaultdict(dict)
        for aid, caps in data.get("reputation", {}).items():
            for cap, vals in caps.items():
                reputation[aid][cap] = AgentReputation(aid, cap, vals.get("successes", 0), vals.get("failures", 0))
        self.reputation = reputation
        self.episodic = data.get("episodic", [])
        self.semantic = data.get("semantic", [])

    @classmethod
    def load(cls) -> "MemoryStore":
        store = cls()
        store._apply_disk_state(store._read_disk())
        return store


# module-level singleton
_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore.load()
    return _store
